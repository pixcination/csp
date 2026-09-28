r"""Tag logged positions with a data-quality flag (analytics/paper.add_flag).

    .venv\Scripts\python scripts\flag_positions.py pre_fix_bars --runs-before 2026-09-28T16:00
    .venv\Scripts\python scripts\flag_positions.py pre_fix_bars --ids 1 2 3

`--runs-before` picks every position whose source run STARTED before the given
local time (from data/runs/<id>/manifest.json). Idempotent; nothing is deleted.

Used once on 2026-09-28: the positions logged from that morning's runs were
priced before the Phase 18 partial-bar fix (daily sync had stored in-progress
RTH bars), so they carry `pre_fix_bars` and reports can set them aside.
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics import paper  # noqa: E402
from core.paths import runs_dir  # noqa: E402


def run_started(run_id: str) -> dt.datetime | None:
    try:
        manifest = json.loads((runs_dir() / run_id / "manifest.json").read_text(encoding="utf-8"))
        return dt.datetime.fromisoformat(manifest["started_at"])
    except (OSError, ValueError, KeyError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("flag")
    ap.add_argument("--runs-before", default=None, help="ISO local time")
    ap.add_argument("--ids", nargs="*", type=int, default=None)
    args = ap.parse_args()
    positions = paper.list_positions()
    if args.ids:
        ids = [i for i in args.ids if i in set(positions["id"].astype(int))]
    elif args.runs_before:
        cutoff = dt.datetime.fromisoformat(args.runs_before)
        ids = []
        for _, row in positions.iterrows():
            started = run_started(str(row["run_id"])) if row.get("run_id") else None
            if started is not None and started < cutoff:
                ids.append(int(row["id"]))
    else:
        ap.error("give --runs-before or --ids")
    changed = paper.add_flag(ids, args.flag)
    print(f"{args.flag}: {len(ids)} position(s) matched ({', '.join(f'#{i}' for i in sorted(ids))}); "
          f"{changed} newly flagged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
