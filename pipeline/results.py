"""
Persisted run results -- so a browser refresh does not lose the last run.

Before Phase 8 the Decisions, Wheel and Portfolio pages read only
`st.session_state["last_manifest"]`, which a refresh or restart empties, even
though every run already wrote `data/runs/<id>/manifest.json`. Now each run
also writes its tables beside the manifest:

    data/runs/<id>/manifest.json       what ran, stage results, warnings
    data/runs/<id>/candidates.parquet  EVERY evaluated strike, accepted and
                                       rejected, with `selected` (best per
                                       ticker within the run cap) and
                                       `proposed` (survived portfolio limits)
    data/runs/<id>/positions.parquet   open-position evaluations

and pages fall back to `latest_run()` when session state is empty.

Runs written before Phase 8 have only the manifest; for those, `candidates`
is rebuilt from the proposals embedded in it and `has_full_sheet` is False.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from core.paths import runs_dir

MANIFEST = "manifest.json"
CANDIDATES = "candidates.parquet"
POSITIONS = "positions.parquet"
KEY = ["ticker", "expiration", "strike"]


@dataclass
class RunResults:
    run_id: str
    folder: Path
    manifest: object                     # pipeline.run.RunManifest
    candidates: pd.DataFrame = field(default_factory=pd.DataFrame)
    positions: pd.DataFrame = field(default_factory=pd.DataFrame)
    has_full_sheet: bool = False

    @property
    def finished_at(self) -> str | None:
        return getattr(self.manifest, "finished_at", None)

    @property
    def label(self) -> str:
        finished = self.finished_at or "unfinished"
        try:
            finished = dt.datetime.fromisoformat(finished).strftime("%a %Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            pass
        return f"run {self.run_id} · finished {finished}"


# --- Writing ---------------------------------------------------------------

def _parquet_safe(frame: pd.DataFrame) -> pd.DataFrame:
    """Object columns holding lists/dicts/dates -> something Arrow accepts.

    Lists of strings (rejection reasons) are kept as lists; anything else
    non-scalar becomes JSON text rather than failing the whole write.
    """
    out = frame.copy()
    for column in out.columns:
        if out[column].dtype != object:
            continue
        sample = out[column].dropna()
        if sample.empty:
            continue
        if sample.map(lambda v: isinstance(v, (list, tuple))
                      and all(isinstance(x, str) for x in v)).all():
            out[column] = out[column].map(lambda v: list(v) if isinstance(v, (list, tuple)) else [])
        elif sample.map(lambda v: isinstance(v, (dict, list, tuple, set))).any():
            out[column] = out[column].map(
                lambda v: None if v is None else json.dumps(v, default=str))
        elif sample.map(lambda v: isinstance(v, (dt.date, dt.datetime, pd.Timestamp))).all():
            out[column] = pd.to_datetime(out[column])
        else:
            out[column] = out[column].map(lambda v: None if v is None else str(v))
    return out


def annotate_sheet(full: pd.DataFrame, selected: pd.DataFrame,
                   proposed: pd.DataFrame) -> pd.DataFrame:
    """Flag which rows of the full sheet were selected and finally proposed."""
    if full.empty:
        return full
    out = full.copy()

    def keys(frame: pd.DataFrame) -> set:
        if frame is None or frame.empty or not set(KEY) <= set(frame.columns):
            return set()
        return {(str(t), str(pd.Timestamp(e).date()), float(k))
                for t, e, k in frame[KEY].itertuples(index=False)}

    row_keys = [(str(t), str(pd.Timestamp(e).date()), float(k))
                for t, e, k in out[KEY].itertuples(index=False)]
    chosen, final = keys(selected), keys(proposed)
    out["selected"] = [k in chosen for k in row_keys]
    out["proposed"] = [k in final for k in row_keys]
    return out


def write_tables(run_id: str, candidates: pd.DataFrame | None,
                 positions: list[dict] | pd.DataFrame | None) -> dict:
    """Write the run's tables. Returns {name: rows written}."""
    folder = runs_dir() / run_id
    folder.mkdir(parents=True, exist_ok=True)
    written = {}
    if candidates is not None:
        _parquet_safe(candidates).to_parquet(folder / CANDIDATES, index=False)
        written["candidates"] = len(candidates)
    if positions is not None:
        frame = positions if isinstance(positions, pd.DataFrame) else pd.DataFrame(positions)
        _parquet_safe(frame).to_parquet(folder / POSITIONS, index=False)
        written["positions"] = len(frame)
    return written


# --- Reading ---------------------------------------------------------------

def list_runs() -> list[str]:
    """Run ids with a manifest, newest first. Ids sort chronologically."""
    root = runs_dir()
    if not root.exists():
        return []
    return sorted((p.name for p in root.iterdir()
                   if p.is_dir() and (p / MANIFEST).exists()), reverse=True)


def load_run(run_id: str) -> RunResults | None:
    from pipeline.run import RunManifest

    folder = runs_dir() / run_id
    try:
        data = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
        known = RunManifest.__dataclass_fields__
        manifest = RunManifest(**{k: v for k, v in data.items() if k in known})
    except Exception:
        return None

    result = RunResults(run_id=run_id, folder=folder, manifest=manifest)
    analyse = (manifest.stages or {}).get("analyse", {}) or {}
    if (folder / CANDIDATES).exists():
        try:
            result.candidates = pd.read_parquet(folder / CANDIDATES)
            result.has_full_sheet = True
        except Exception:
            pass
    if not result.has_full_sheet and analyse.get("candidates"):
        result.candidates = pd.DataFrame(analyse["candidates"]).assign(
            accepted=True, selected=True, proposed=True)
    if (folder / POSITIONS).exists():
        try:
            result.positions = pd.read_parquet(folder / POSITIONS)
        except Exception:
            pass
    elif analyse.get("open_positions"):
        result.positions = pd.DataFrame(analyse["open_positions"])
    return result


def latest_run(finished_only: bool = True, with_analysis: bool = True) -> RunResults | None:
    """The newest run on disk, skipping ones that never finished (a crash or
    a run still in progress) unless `finished_only` is False, and -- since
    Phase 9's `--data-only` nightly job -- runs that analysed nothing, which
    would otherwise blank every results page the morning after."""
    for run_id in list_runs():
        result = load_run(run_id)
        if result is None:
            continue
        if finished_only and not result.finished_at:
            continue
        if with_analysis and "analyse" not in (result.manifest.stages or {}):
            continue
        return result
    return None
