"""
Run and chain retention (Phase 18, review C.6).

    data/runs/<id>/manifest.json   kept forever
    data/runs/<id>/prob_*.parquet  the largest run artefacts: removed after
                                   `retention.prob_tables_days` UNLESS a paper
                                   position references the run (its Trade
                                   Detail must keep working)
    data/runs/<id>/other tables    kept (candidates, positions, underlyings,
                                   strategies are small)
    data/chains/<block>/           after `retention.chain_blocks_days`, thinned
                                   to one block per day (the latest RTH block,
                                   else the day's last); `data/chain_archive/`
                                   is never touched

`plan()` lists what would go; `apply(plan)` deletes it. Nothing runs
automatically: `scripts/prune.py` prints the plan and needs `--apply`.
"""
from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

from core.paths import chains_dir, load_config, runs_dir

PROB_TABLES = ("prob_curves.parquet", "prob_metrics.parquet", "prob_policies.parquet")


def _referenced_runs() -> set[str]:
    try:
        from analytics import paper
        frame = paper._query("SELECT DISTINCT run_id FROM paper_positions WHERE run_id IS NOT NULL")
        return set(frame["run_id"].astype(str))
    except Exception:
        return set()


def _run_date(run_id: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(run_id[:8], "%Y%m%d").date()
    except ValueError:
        return None


def _block_date(block: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(block[:10])
    except ValueError:
        return None


def plan(today: dt.date | None = None) -> dict:
    cfg = load_config().get("retention", {}) or {}
    today = today or dt.date.today()
    prob_cut = today - dt.timedelta(days=int(cfg.get("prob_tables_days", 60)))
    chain_cut = today - dt.timedelta(days=int(cfg.get("chain_blocks_days", 90)))
    keep = _referenced_runs()
    files: list[Path] = []
    for folder in sorted(p for p in runs_dir().iterdir() if p.is_dir()):
        day = _run_date(folder.name)
        if day is None or day >= prob_cut or folder.name in keep:
            continue
        files.extend(folder / name for name in PROB_TABLES if (folder / name).exists())
    by_day: dict[dt.date, list[str]] = {}
    for block in sorted(p.name for p in chains_dir().iterdir() if p.is_dir()):
        day = _block_date(block)
        if day is not None and day < chain_cut:
            by_day.setdefault(day, []).append(block)
    blocks: list[Path] = []
    for day, names in by_day.items():
        rth = [b for b in names if "_rth_" in b]
        keeper = (rth or names)[-1]
        blocks.extend(chains_dir() / b for b in names if b != keeper)
    size = sum(f.stat().st_size for f in files) + sum(
        f.stat().st_size for b in blocks for f in b.rglob("*") if f.is_file())
    return {"run_files": files, "chain_blocks": blocks, "bytes": size,
            "kept_referenced_runs": sorted(keep)}


def apply(planned: dict) -> dict:
    removed_files = removed_blocks = 0
    for path in planned["run_files"]:
        path.unlink(missing_ok=True)
        removed_files += 1
    for block in planned["chain_blocks"]:
        shutil.rmtree(block, ignore_errors=True)
        removed_blocks += 1
    return {"run_files": removed_files, "chain_blocks": removed_blocks,
            "bytes": planned["bytes"]}
