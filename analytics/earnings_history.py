"""
Earnings reaction history -- how far does this stock actually move on a
report, and does it habitually beat the move the options implied? (Phase 9)

For each past report in the events table (yfinance history, with bmo/amc
timing), on the price basis:

    reaction day   bmo -> the report date itself
                   amc -> the next session
                   during/unknown -> the report date, and the 2-session move
                   below covers either case
    gap            reaction-day open / previous close - 1
    close_to_close reaction-day close / previous close - 1
    two_session    close of the session AFTER the report date vs the close
                   BEFORE it -- the move a short put held through the print
                   experienced whichever side of the session it landed on
    vs ATR         |close_to_close| / ATR(14) on the prior day, in ATR units

Implied move: when a TastyTrade market-metrics snapshot exists from before
the report (stored daily since Phase 9), the implied move to the first
expiration after the report is spot x IV x sqrt(days/365), and `beat` says
whether the realised |close_to_close| exceeded it. Until snapshots have
accumulated across a report, `implied_move` is None -- reported, not guessed.
"""
from __future__ import annotations

import datetime as dt
import json
import math

import numpy as np
import pandas as pd


def atr(daily: pd.DataFrame, window: int = 14) -> pd.Series:
    high, low, close = (daily[c].astype(float) for c in ("high", "low", "close"))
    prev = close.shift(1)
    true_range = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()],
                           axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()


def reactions(symbol: str, daily: pd.DataFrame | None = None,
              report_dates: pd.DataFrame | None = None, last_n: int = 12) -> pd.DataFrame:
    """One row per past report: date, timing, gap, close-to-close, 2-session
    move, ATR multiple, implied move (when stored) and whether it was beaten."""
    if daily is None:
        from data_sources.yfinance_sync import load_daily
        daily = load_daily(symbol, basis="price")
    if report_dates is None:
        from data_sources import events
        frame = events.load()
        today = dt.date.today()
        report_dates = frame[(frame["symbol"] == symbol) & (frame["type"] == "earnings")
                             & (frame["date"] < today)][["date", "time_of_day"]]
    if daily is None or daily.empty or report_dates is None or report_dates.empty:
        return pd.DataFrame()

    bars = daily.sort_values("date").reset_index(drop=True).copy()
    bars["date"] = pd.to_datetime(bars["date"])
    bars["atr"] = atr(bars)
    dates = bars["date"].dt.date.tolist()
    index_of = {d: i for i, d in enumerate(dates)}

    rows = []
    for report in report_dates.sort_values("date").tail(last_n).itertuples():
        report_date, timing = report.date, (report.time_of_day or "unknown")
        # the first session on/after the report date
        i = next((index_of[d] for d in dates if d >= report_date), None)
        if i is None or i == 0:
            continue
        react = i + 1 if timing == "amc" else i
        if react >= len(bars):
            continue
        before = i - 1                         # last close before the report date
        prev_close = float(bars.loc[react - 1, "close"])
        row = {
            "symbol": symbol, "report_date": report_date, "time_of_day": timing,
            "reaction_date": dates[react],
            "gap": float(bars.loc[react, "open"]) / prev_close - 1.0,
            "close_to_close": float(bars.loc[react, "close"]) / prev_close - 1.0,
            "two_session": float(bars.loc[min(i + 1, len(bars) - 1), "close"])
            / float(bars.loc[before, "close"]) - 1.0,
        }
        prior_atr = bars.loc[react - 1, "atr"]
        row["atr_multiple"] = (abs(row["close_to_close"]) * prev_close / prior_atr
                               if prior_atr and prior_atr == prior_atr else np.nan)
        implied = implied_move(symbol, report_date, prev_close)
        row["implied_move"] = implied
        row["beat_implied"] = (abs(row["close_to_close"]) > implied
                               if implied is not None else None)
        rows.append(row)
    return pd.DataFrame(rows)


def implied_move(symbol: str, report_date: dt.date, spot: float) -> float | None:
    """Fractional implied move to the first expiration after the report, from
    the newest market-metrics snapshot taken BEFORE the report date."""
    try:
        from data_sources import tasty_metrics
        hist = tasty_metrics.history(symbol)
    except Exception:
        return None
    if hist is None or hist.empty:
        return None
    hist = hist[pd.to_datetime(hist["snapshot_date"]).dt.date < report_date]
    if hist.empty:
        return None
    snap = hist.iloc[-1]
    snap_date = pd.Timestamp(snap["snapshot_date"]).date()
    try:
        expirations = json.loads(snap["expirations_json"] or "[]")
    except (TypeError, ValueError):
        return None
    after = sorted((dt.date.fromisoformat(e["expiration"]), e["iv"]) for e in expirations
                   if e.get("iv") and dt.date.fromisoformat(e["expiration"]) >= report_date)
    if not after:
        return None
    expiry, iv = after[0]
    days = max((expiry - snap_date).days, 1)
    return float(iv) * math.sqrt(days / 365.0)


def summary(symbol: str, last_n: int = 12) -> dict:
    """Median/max absolute moves and, where known, how often the implied move
    was beaten."""
    frame = reactions(symbol, last_n=last_n)
    if frame.empty:
        return {"symbol": symbol, "n": 0}
    moves = frame["close_to_close"].abs()
    known = frame["beat_implied"].dropna()
    return {
        "symbol": symbol, "n": len(frame),
        "median_abs_move": float(moves.median()),
        "max_abs_move": float(moves.max()),
        "median_abs_gap": float(frame["gap"].abs().median()),
        "median_atr_multiple": float(frame["atr_multiple"].median()),
        "worst_down": float(frame["close_to_close"].min()),
        "implied_known": int(len(known)),
        "beat_rate": float(known.mean()) if len(known) else None,
    }
