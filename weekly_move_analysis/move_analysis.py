#!/usr/bin/env python3
"""
Weekly and 2-week move analysis, with Maximum Adverse Excursion.

    python weekly_move_analysis/move_analysis.py AAPL
    python weekly_move_analysis/move_analysis.py AAPL --horizons 5 7 10
    python weekly_move_analysis/move_analysis.py --all        # whole universe

WHAT CHANGED
------------
This was the original standalone script -- hardcoded `D:/csp/weekly_move_analysis`,
an `input()` prompt, and Friday-to-Friday windows read from a per-ticker CSV.
Its method was the most valuable unused idea in the project, so it has been
generalised into `analytics/moves.py` and this file is now a reporting front
end over that engine.

Three practical differences:

* **Portable.** Paths come from `core.paths`; nothing is hardcoded, so the
  project folder can move to another machine unchanged.
* **Every start date, not just Fridays.** ~250 observations per year per
  horizon instead of 52. The tails stop being noise. Overlap is accounted for
  in the reported effective sample size rather than glossed over.
* **One data source.** Reads the dividend-adjusted daily bars the rest of the
  pipeline uses, instead of a separate yfinance CSV that could disagree with
  it. Falls back to a local CSV if the database has not been built yet.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics import moves  # noqa: E402
from core.paths import load_config, load_universe, project_root  # noqa: E402


def data_dir() -> Path:
    """This module's own folder -- resolved from the file, never hardcoded."""
    return Path(__file__).resolve().parent


def load_bars(ticker: str) -> pd.DataFrame:
    """Prefer the pipeline's price-basis (traded, split-adjusted) bars; fall back to a local CSV."""
    try:
        from data_sources.yfinance_sync import load_daily
        frame = load_daily(ticker, basis="price")
        if not frame.empty:
            return frame.set_index("date")
    except Exception:
        pass

    csv = data_dir() / f"{ticker}.csv"
    if not csv.exists():
        raise SystemExit(
            f"No data for {ticker}.\n"
            f"  Either run the pipeline once to populate the daily database:\n"
            f"      python pipeline\\run.py --tickers {ticker}\n"
            f"  or drop {csv.name} in {data_dir()}")
    frame = pd.read_csv(csv, index_col=0)
    frame.index = pd.to_datetime(frame.index, utc=True, errors="coerce").tz_localize(None)
    return frame[frame.index.notna()].sort_index()


def render(stats: moves.MoveStats) -> None:
    print(f"\n{'=' * 78}")
    print(f"{stats.ticker} -- {stats.horizon_days} trading-day moves -- {stats.lookback_label}")
    print(f"{stats.n_observations:,} overlapping windows "
          f"(~{stats.effective_n} independent) | {stats.start_date} to {stats.end_date}")
    print('=' * 78)
    print(f"{'pct':>6} {'terminal':>11} {'MAE':>11} {'MFE':>11}")
    for p in (1, 5, 10, 25, 50, 75, 90, 95, 99):
        key = f"p{p}"
        print(f"{'p' + str(p):>6} "
              f"{stats.terminal_percentiles[key] * 100:>10.2f}% "
              f"{stats.mae_percentiles[key] * 100:>10.2f}% "
              f"{stats.mfe_percentiles[key] * 100:>10.2f}%")
    print(f"\nmedian terminal {stats.median_terminal:+.2%}   "
          f"downside semi-deviation {stats.downside_semi_deviation:.2%}")


def render_assignment_table(frame: pd.DataFrame, ticker: str, spot: float,
                             horizon: int) -> None:
    print(f"\n{ticker} -- assignment odds at {horizon} trading days, spot ${spot:,.2f}")
    print(f"{'OTM':>6} {'strike':>10} {'P(touch)':>10} {'P(assign)':>11} "
          f"{'E[loss]':>9} {'tail':>8}")
    for pct in (0.02, 0.03, 0.05, 0.07, 0.10):
        strike = spot * (1 - pct)
        probs = moves.breach_probabilities(frame, ticker, spot, strike, horizon,
                                            lookback_years=10, vol_conditioned=True)
        if probs is None:
            print(f"{pct:>5.0%} {strike:>10.2f}   insufficient history")
            continue
        print(f"{pct:>5.0%} {strike:>10.2f} {probs.prob_touch:>9.1%} "
              f"{probs.prob_terminal:>10.1%} "
              f"{probs.expected_loss_if_breached:>8.2%} "
              f"{probs.conditional_tail_loss:>7.2%}")


def analyse(ticker: str, horizons: list[int], lookback: int, export: bool) -> None:
    frame = load_bars(ticker)
    frame.columns = [str(c).lower() for c in frame.columns]
    print(f"\n{ticker}: {len(frame):,} sessions, "
          f"{frame.index[0].date()} to {frame.index[-1].date()}")

    sheets: dict[str, pd.DataFrame] = {}
    for horizon in horizons:
        stats = moves.compute_stats(frame, ticker, horizon, lookback_years=lookback)
        if stats is None:
            print(f"\n{ticker} @ {horizon}d: insufficient history")
            continue
        render(stats)
        if export:
            sheets[f"{horizon}d_windows"] = moves.build_windows(frame, horizon)

    spot = float(frame["close"].iloc[-1])
    render_assignment_table(frame, ticker, spot, horizons[0])

    recovery = moves.recovery_statistics(frame, drop_pct=0.05, horizon=horizons[0])
    if recovery.get("assignments"):
        print(f"\nIf assigned on a {horizons[0]}-day 5%-OTM put: "
              f"{recovery['assignment_rate']:.1%} of the time, median "
              f"{recovery['median_sessions_to_recover']:.0f} sessions to recover "
              f"the strike (p90 {recovery['p90_sessions_to_recover']:.0f}, "
              f"{recovery['unrecovered_rate']:.1%} still under water after a year)")

    if export and sheets:
        out = data_dir() / f"{ticker}_move_analysis.xlsx"
        try:
            with pd.ExcelWriter(out, engine="openpyxl") as writer:
                for name, sheet in sheets.items():
                    sheet.to_excel(writer, sheet_name=name[:31], index=False)
            print(f"\nWrote {out}")
        except Exception as exc:
            print(f"\nExcel export failed ({exc}); writing CSVs instead")
            for name, sheet in sheets.items():
                sheet.to_csv(data_dir() / f"{ticker}_{name}.csv", index=False)


def main() -> int:
    cfg = load_config().get("move_analysis", {})
    ap = argparse.ArgumentParser(description="Empirical move and MAE analysis.")
    ap.add_argument("ticker", nargs="?", help="ticker symbol")
    ap.add_argument("--all", action="store_true", help="run the whole universe")
    ap.add_argument("--horizons", nargs="+", type=int,
                     default=cfg.get("horizons_days", [5, 7, 10]),
                     help="horizons in TRADING days")
    ap.add_argument("--lookback", type=int, default=10,
                     help="years of history; 0 for all")
    ap.add_argument("--export", action="store_true", help="write an Excel workbook")
    args = ap.parse_args()

    if args.all:
        tickers = load_universe()
    elif args.ticker:
        tickers = [args.ticker.strip().upper()]
    else:
        ap.error("give a ticker, or --all")
        return 2

    for ticker in tickers:
        try:
            analyse(ticker, args.horizons, args.lookback, args.export)
        except SystemExit as exc:
            print(exc)
        except Exception as exc:
            print(f"{ticker}: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
