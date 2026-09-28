"""
Put-credit-spread rule backtest with walk-forward (Phase 15).

    python scripts/backtest_pcs.py                              # SPY QQQ IWM DIA, 20y
    python scripts/backtest_pcs.py --tickers SPY --years 10 --dte 45

Simulates every rule set in the grid (DTE x short delta x width x profit
target x loss stop x breach close x time stop) once per ticker on synthetic Black-Scholes
prices over the real daily path, then walks a 5-year train / 1-year test
window forward. See `analytics/pcs_backtest.py` for the model and its limits.

Writes data/validation/:
    pcs_backtest_sweep.parquet     one row per ticker x rule set (full history)
    pcs_backtest_folds.parquet     one row per ticker x walk-forward fold
    pcs_backtest_summary.json      pooled verdict, grid shape, per-ticker results
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics import pcs_backtest as pb  # noqa: E402
from core.paths import load_config, validation_dir  # noqa: E402


def _floats(text: str | None):
    if not text:
        return None
    out = []
    for part in text.split(","):
        part = part.strip().lower()
        out.append(None if part in ("none", "hold", "off") else float(part))
    return tuple(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="PCS rule backtest with walk-forward")
    ap.add_argument("--tickers", default="SPY,QQQ,IWM,DIA")
    ap.add_argument("--years", type=int, default=20)
    ap.add_argument("--dte", help="comma list of entry DTEs (default 30,45)")
    ap.add_argument("--deltas", help="comma list, e.g. -0.15,-0.2")
    ap.add_argument("--widths", help="comma list of widths as a fraction of spot")
    ap.add_argument("--targets", help="comma list of profit targets in %%, 'none' = hold")
    ap.add_argument("--stops", help="comma list of loss-stop multiples, 'none' = no stop")
    ap.add_argument("--time-stops", help="comma list of time-stop DTEs, 'none' = off")
    ap.add_argument("--breach", choices=["both", "on", "off"], default="both",
                    help="close when the short strike is breached")
    args = ap.parse_args()

    vrp = float(load_config().get("backtest", {}).get("vol_risk_premium_multiplier", 1.15))
    base = pb.PCSParams(vol_risk_premium=vrp)
    axes = {"dte": _floats(args.dte), "short_delta": _floats(args.deltas),
            "width_pct": _floats(args.widths), "profit_target_pct": _floats(args.targets),
            "loss_stop_multiple": _floats(args.stops), "time_stop_dte": _floats(args.time_stops),
            "close_on_breach": {"both": None, "on": (True,), "off": (False,)}[args.breach]}
    for key in ("dte", "time_stop_dte"):
        if axes[key]:
            axes[key] = tuple(int(v) if v is not None else None for v in axes[key])
    params = pb.grid(base, **axes)
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    print(f"{len(params)} rule sets x {len(tickers)} tickers, {args.years}y")

    started = time.perf_counter()
    result = pb.across_universe(tickers, params, baseline=base, years=args.years)
    seconds = time.perf_counter() - started

    out = validation_dir()
    result["sweep"].to_parquet(out / "pcs_backtest_sweep.parquet", index=False)
    folds = result["folds"]
    if not folds.empty:
        folds.to_parquet(out / "pcs_backtest_folds.parquet", index=False)
    pooled = {**result["pooled"], "seconds": round(seconds, 1), "years": args.years,
              "tickers_requested": tickers,
              "per_ticker": result["per_ticker"].to_dict("records")}
    (out / "pcs_backtest_summary.json").write_text(json.dumps(pooled, indent=2, default=str),
                                                   encoding="utf-8")

    print(f"done in {seconds:.0f}s")
    if "verdict" in pooled:
        print(f"pooled: IS {pooled['mean_in_sample']:.1%}, OOS {pooled['mean_out_of_sample']:.1%}, "
              f"baseline OOS {pooled['mean_baseline_oos']:.1%} over {pooled['folds']} folds")
        print(pooled["verdict"])
    per = result["per_ticker"]
    if not per.empty:
        cols = [c for c in ("ticker", "n_folds", "mean_in_sample", "mean_out_of_sample",
                            "mean_baseline_oos", "beat_baseline_rate") if c in per]
        print(per[cols].to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    for axis, rows in pooled.get("shape", {}).items():
        print(f"\n{axis}:")
        print(pd.DataFrame(rows).to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
