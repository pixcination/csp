"""
Cross-sectional parameter sweep -- the first step of Phase 5.

    python scripts/sweep_universe.py                    # whole universe
    python scripts/sweep_universe.py --tickers AAPL,F,T
    python scripts/sweep_universe.py --years 10 --min-cycles 40

WHY THIS AND NOT THE SINGLE-TICKER SWEEP
----------------------------------------
Running the grid on one name and taking the winner is how you fit noise. AAPL
over 2011-2026 says call delta 0.20 and put delta -0.25; that is one ticker
over one regime, and the top few cells in any 36-cell grid are usually within
each other's error bars.

What survives is what repeats. This runs the same grid across every ticker and
scores each parameter set on **how consistently it ranks well**, not on its
best result:

    median rank across tickers      lower is better; robust to one outlier name
    top-quartile hit rate           share of tickers where it landed in the top 25%
    median annualised return        the level, once consistency is established
    worst-ticker return             what it does on the name it suits least

A setting that ranks top-quartile on 45 of 61 names is a rule. A setting that
wins on three and is mediocre on the rest is a coincidence with good marketing.

The output is written to `output/sweep_universe.csv` (every ticker x cell) and
`output/sweep_consensus.csv` (the ranking above). Nothing in `config.yaml` is
changed automatically -- the consensus is evidence for a decision, not the
decision.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics.wheel_backtest import WheelParams, sweep  # noqa: E402
from core.paths import load_universe, output_dir  # noqa: E402
from core.progress import ConsoleReporter  # noqa: E402

PUT_DELTAS = (-0.15, -0.20, -0.25, -0.30)
PUT_DTES = (5, 7, 10)
CALL_DELTAS = (0.20, 0.25, 0.30)
METRIC = "annualised_on_capital_deployed"


def consensus(frame: pd.DataFrame, min_cycles: int) -> pd.DataFrame:
    """Score each parameter cell on consistency across tickers."""
    usable = frame[frame["n_cycles"] >= min_cycles].copy()
    if usable.empty:
        return pd.DataFrame()

    # Rank cells within each ticker: 1 is that ticker's best setting.
    usable["rank_in_ticker"] = usable.groupby("ticker")[METRIC].rank(
        ascending=False, method="min")
    cells = usable.groupby("ticker")["rank_in_ticker"].transform("max")
    usable["top_quartile"] = usable["rank_in_ticker"] <= (cells * 0.25)

    grouped = usable.groupby(["put_delta", "put_dte", "call_delta"])
    out = grouped.agg(
        tickers=("ticker", "nunique"),
        median_rank=("rank_in_ticker", "median"),
        top_quartile_rate=("top_quartile", "mean"),
        median_return=(METRIC, "median"),
        mean_return=(METRIC, "mean"),
        worst_ticker_return=(METRIC, "min"),
        best_ticker_return=(METRIC, "max"),
        median_assignment=("assignment_rate", "median"),
        median_cycle_days=("mean_cycle_days", "median"),
    ).reset_index()

    # Consistency first, level second. A cell that is reliably good beats one
    # that is occasionally spectacular -- the second kind does not survive
    # contact with a regime it has not seen.
    out = out.sort_values(["median_rank", "median_return"],
                          ascending=[True, False]).reset_index(drop=True)
    return out


def render(table: pd.DataFrame, current: WheelParams) -> None:
    if table.empty:
        print("\nNo cells met the minimum-cycle threshold.")
        return

    print(f"\n{'=' * 88}")
    print("  CONSENSUS -- ranked by median rank across tickers, then by median return")
    print('=' * 88)
    print(f"{'putΔ':>6} {'pDTE':>5} {'callΔ':>6} {'names':>6} {'med rank':>9} "
          f"{'top-25%':>8} {'med ret':>8} {'worst':>8} {'assign':>7} {'days':>6}")
    for row in table.head(10).itertuples():
        print(f"{row.put_delta:>6.2f} {row.put_dte:>5} {row.call_delta:>6.2f} "
              f"{row.tickers:>6} {row.median_rank:>9.1f} "
              f"{row.top_quartile_rate:>7.0%} {row.median_return:>7.1%} "
              f"{row.worst_ticker_return:>7.1%} {row.median_assignment:>6.0%} "
              f"{row.median_cycle_days:>5.0f}")

    print(f"\n{'-' * 88}")
    print("  MARGINAL EFFECT -- median return by each parameter, others averaged over")
    print('-' * 88)
    for column, label in (("put_delta", "put delta"), ("put_dte", "put DTE"),
                           ("call_delta", "call delta")):
        means = table.groupby(column)["median_return"].median()
        best = means.idxmax()
        rendered = "  ".join(
            (f"[{k}: {v:.1%}]" if k == best else f" {k}: {v:.1%} ")
            for k, v in means.items())
        print(f"  {label:<11} {rendered}")

    top = table.iloc[0]
    print(f"\n{'-' * 88}")
    print("  VERDICT")
    print('-' * 88)
    print(f"  Most consistent cell : put Δ {top.put_delta:.2f}, "
          f"{top.put_dte} DTE, call Δ {top.call_delta:.2f}")
    print(f"                         top-quartile on {top.top_quartile_rate:.0%} of "
          f"{int(top.tickers)} tickers, median {top.median_return:.1%} annualised")
    print(f"  Config currently says: put Δ {current.put_delta:.2f}, "
          f"{current.put_dte} DTE, call Δ {current.call_delta:.2f}")

    match = table[(table.put_delta == current.put_delta)
                  & (table.put_dte == current.put_dte)
                  & (table.call_delta == current.call_delta)]
    if not match.empty:
        row = match.iloc[0]
        position = int(match.index[0]) + 1
        print(f"                         which ranks #{position} of {len(table)} here "
              f"(median rank {row.median_rank:.1f}, {row.median_return:.1%})")
        gap = top.median_return - row.median_return
        if gap <= 0.005:
            print("\n  The current settings are within noise of the best cell. "
                  "Leave them alone.")
        else:
            print(f"\n  The best cell is {gap:.1%} better in median annualised terms. "
                  f"Worth changing\n  IF the margin holds up under walk-forward "
                  f"validation -- this is still in-sample.")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", default=None, help="comma-separated subset")
    ap.add_argument("--years", type=int, default=15, help="lookback (default 15)")
    ap.add_argument("--min-cycles", type=int, default=30,
                     help="drop ticker/cell combinations with fewer cycles")
    args = ap.parse_args()

    from data_sources.yfinance_sync import load_daily

    tickers = ([t.strip().upper() for t in args.tickers.split(",") if t.strip()]
               if args.tickers else load_universe())
    if not tickers:
        print("No universe. Run the pipeline, or pass --tickers.")
        return 1

    cells = len(PUT_DELTAS) * len(PUT_DTES) * len(CALL_DELTAS)
    print(f"Sweeping {cells} parameter cells across {len(tickers)} tickers "
          f"({cells * len(tickers):,} simulations, {args.years}y each).\n")

    reporter = ConsoleReporter([("sweep", "Cross-sectional sweep")])
    frames, skipped = [], []

    with reporter.stage("sweep", "Cross-sectional sweep", total=len(tickers) * cells):
        for ticker in tickers:
            daily = load_daily(ticker, basis="price", with_dividends=True)
            if daily.empty:
                skipped.append(ticker)
                reporter.advance(cells, note=f"{ticker} no daily history")
                continue
            cutoff = daily["date"].max() - pd.DateOffset(years=args.years)
            daily = daily[daily["date"] >= cutoff]
            try:
                grid = sweep(daily, ticker, PUT_DELTAS, PUT_DTES, CALL_DELTAS,
                              reporter=reporter)
                if not grid.empty:
                    frames.append(grid)
            except Exception as exc:
                skipped.append(ticker)
                reporter.log(f"{ticker}: {type(exc).__name__}: {exc}")

    if not frames:
        print("\nNo results. Has the daily database been populated? "
              "Run: python pipeline/run.py")
        return 1

    detail = pd.concat(frames, ignore_index=True)
    table = consensus(detail, args.min_cycles)

    detail_path = output_dir() / "sweep_universe.csv"
    consensus_path = output_dir() / "sweep_consensus.csv"
    detail.to_csv(detail_path, index=False)
    table.to_csv(consensus_path, index=False)

    render(table, WheelParams())

    if skipped:
        print(f"  Skipped {len(skipped)} ticker(s) with no usable history: "
              f"{', '.join(skipped[:12])}"
              + (" ..." if len(skipped) > 12 else ""))
    print(f"  Per-ticker detail : {detail_path}")
    print(f"  Consensus         : {consensus_path}")
    print("\n  NOTE: this is in-sample. Before changing config.yaml, re-run the top")
    print("  two or three cells under walk-forward validation -- fit on a rolling")
    print("  window, score on the period after it. A cell that wins in-sample and")
    print("  fails out-of-sample is the most common way to lose money confidently.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
