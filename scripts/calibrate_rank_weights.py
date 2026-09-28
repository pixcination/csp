"""
Calibrate the underlying-ranking weights against outcomes (Phase 14).

    python scripts/calibrate_rank_weights.py              # 21 and 5 trading days
    python scripts/calibrate_rank_weights.py --horizon 21 --years 8

Method and limits: analytics/rank_calibration.py. Writes
data/validation/rank_calibration_ic_<h>d.csv (one row per component and
preset) and rank_calibration_<h>d.json (settings + suggested weights); the
Validation page reads them. It changes no config: adopting weights is Tom's
decision (docs/PHASE14_SUMMARY.md).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics import rank_calibration as rc  # noqa: E402
from core.paths import load_universe, validation_dir  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--horizon", type=int, action="append",
                        help="trading days to expiry (repeatable; default 21 and 5)")
    parser.add_argument("--years", type=int, default=8)
    parser.add_argument("--scope", default="all", help="load_universe scope (default all)")
    args = parser.parse_args()

    symbols = load_universe(args.scope)
    for horizon in args.horizon or [21, 5]:
        started = time.perf_counter()
        s = rc.Settings(horizon=horizon, years=args.years)
        result = rc.run(symbols, s)
        ic = result["ic"]
        if ic.empty:
            print(f"{horizon}d: no data")
            continue
        out = validation_dir()
        ic.to_csv(out / f"rank_calibration_ic_{horizon}d.csv", index=False)
        panel = result["panel"]
        meta = {"horizon_trading_days": horizon, "years": args.years,
                "symbols": int(panel["symbol"].nunique()), "entries": int(len(panel)),
                "dates": int(panel["date"].nunique()),
                "breach_rate": float(panel["breached"].mean()),
                "mean_outcome": float(panel["outcome"].mean()),
                "suggested_weights": result["suggested"],
                "untestable": rc.UNTESTABLE, "seconds": round(time.perf_counter() - started, 1)}
        (out / f"rank_calibration_{horizon}d.json").write_text(json.dumps(meta, indent=2),
                                                              encoding="utf-8")
        print(f"\n=== {horizon} trading days: {meta['symbols']} symbols, {meta['dates']} dates, "
              f"{meta['entries']} entries, breach {meta['breach_rate']:.1%} "
              f"({meta['seconds']}s)")
        print(ic.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
        print("suggested:", result["suggested"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
