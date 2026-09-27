#!/usr/bin/env python3
"""
Fetch daily history for one ticker into this folder.

    python weekly_move_analysis/download_ticker_data.py AAPL

Mostly superseded: the pipeline's `data_sources/yfinance_sync.py` keeps
dividend-adjusted daily bars for the whole universe in
`data/universe_daily.duckdb`, and `move_analysis.py` reads that first. This
remains for the case where you want to look at a ticker that is not in the
universe at all.

Two fixes from the original: the output path is resolved from this file rather
than hardcoded to `D:/csp/...`, and it no longer pip-installs yfinance on every
single run -- that made startup slow and non-deterministic, and dependencies
belong in requirements.txt.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(description="Download daily history for a ticker.")
    ap.add_argument("ticker", help="ticker symbol")
    ap.add_argument("--period", default="max", help="yfinance period (default: max)")
    args = ap.parse_args()

    try:
        import yfinance as yf
    except ImportError:
        print("yfinance is not installed. Run:  pip install -r requirements.txt")
        return 1

    ticker = args.ticker.strip().upper()
    print(f"Downloading {ticker}...")
    frame = yf.Ticker(ticker).history(period=args.period, actions=True, auto_adjust=True)
    if frame is None or frame.empty:
        print(f"No data for {ticker}. Check the symbol.")
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{ticker}.csv"
    frame.to_csv(path)
    print(f"{len(frame):,} rows, {frame.index[0].date()} to {frame.index[-1].date()}")
    print(f"Wrote {path}")
    print(f"\nNext:  python weekly_move_analysis/move_analysis.py {ticker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
