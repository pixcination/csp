"""
Phase 8 one-time data migration. Safe to re-run.

    python scripts/migrate_phase8.py            # full migration
    python scripts/migrate_phase8.py --dry-run  # report only

1. Back up data/universe_daily.duckdb (once) to universe_daily.pre_phase8.duckdb.
2. Full re-pull of every universe ticker into `daily_bars_raw`
   (yfinance, auto_adjust=False).
3. Cross-check the locally derived total-return factor against Yahoo's
   adj_close for every ticker.
4. Drop the mixed-vintage `daily_bars_tr` table once every universe ticker
   is present in `daily_bars_raw` (the backup keeps it).
5. Copy any legacy Trade Log rows into the paper book.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb  # noqa: E402

from core.paths import db_universe_daily, load_universe  # noqa: E402
from core.progress import ConsoleReporter  # noqa: E402

# Beyond this the local and Yahoo adjustments disagree materially.
ADJUSTMENT_TOLERANCE = 0.02


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from analytics import paper
    from data_sources import yfinance_sync as ys

    db = db_universe_daily()
    backup = db.with_name("universe_daily.pre_phase8.duckdb")
    universe = load_universe()
    print(f"universe: {len(universe)} tickers; database: {db}")

    if args.dry_run:
        print(ys.daily_data_status())
        return 0

    if not backup.exists():
        shutil.copy2(db, backup)
        print(f"backup written: {backup}")
    else:
        print(f"backup already exists: {backup}")

    reporter = ConsoleReporter([("daily", "Daily bars")])
    results = ys.sync_daily(universe, reporter=reporter, force_full=True)
    failed = [r for r in results if r.error]
    for r in failed:
        print(f"  FAILED {r.ticker}: {r.error}")

    print("\nadjustment check (local factor vs Yahoo adj_close):")
    worst = []
    for ticker in universe:
        check = ys.adjustment_check(ticker)
        if check:
            worst.append(check)
    worst.sort(key=lambda c: -c["max_deviation"])
    for c in worst[:10]:
        flag = "  <-- check" if c["max_deviation"] > ADJUSTMENT_TOLERANCE else ""
        print(f"  {c['ticker']:6s} max deviation {c['max_deviation']:.4%} "
              f"at {c['worst_date']}{flag}")

    status = ys.daily_data_status()
    if status["universe_covered"] == len(universe) and not failed:
        con = duckdb.connect(str(db))
        try:
            con.execute("DROP TABLE IF EXISTS daily_bars_tr")
            con.execute("CHECKPOINT")
        finally:
            con.close()
        print("\ndropped daily_bars_tr (kept in the backup)")
    else:
        print(f"\nNOT dropping daily_bars_tr: {status['universe_covered']}/"
              f"{len(universe)} covered, {len(failed)} failed")

    copied = paper.migrate_legacy_trade_log()
    print(f"legacy trade log rows copied into the paper book: {copied}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
