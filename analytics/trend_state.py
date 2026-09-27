"""
Trend state -- uptrend / range / downtrend, per day (Phase 10).

    uptrend    EMA21 > EMA50 > EMA200, EMA50 rising over `slope_days`,
               and ADX >= adx_min
    downtrend  EMA21 < EMA50 < EMA200, EMA50 falling, ADX >= adx_min
    range      everything else, including a correctly ordered stack with
               ADX below adx_min (ordered but not trending)

Deliberately simple and fully specified: it is an input to ranking (Phase
11) and a conditioning variable for the technical probability model (Phase
13), and a classifier nobody can restate from memory cannot be sanity
checked against a chart. Thresholds in config.yaml -> trend_state.

Returns a series for every day so the Phase 13 model can condition on the
state *at each historical start date*, not just today's.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.paths import load_config

STATES = ("uptrend", "range", "downtrend")


def classify(frame: pd.DataFrame) -> pd.Series:
    """`frame` from `indicators.compute` (needs ema_21/50/200 and adx)."""
    cfg = load_config().get("trend_state", {}) or {}
    adx_min = cfg.get("adx_min", 20)
    slope_days = cfg.get("slope_days", 10)
    e21, e50, e200 = frame["ema_21"], frame["ema_50"], frame["ema_200"]
    slope = e50 - e50.shift(slope_days)
    trending = frame["adx"] >= adx_min
    up = (e21 > e50) & (e50 > e200) & (slope > 0) & trending
    down = (e21 < e50) & (e50 < e200) & (slope < 0) & trending
    state = np.where(up, "uptrend", np.where(down, "downtrend", "range"))
    known = e200.notna() & frame["adx"].notna() & slope.notna()
    return pd.Series(np.where(known, state, None), index=frame.index, name="trend_state")


def current(symbol: str) -> dict:
    """Today's state with the numbers behind it."""
    from analytics import indicators
    frame = indicators.for_symbol(symbol)
    if frame.empty:
        return {"symbol": symbol, "state": None}
    states = classify(frame)
    row = frame.iloc[-1]
    # How long has it been in this state?
    run = 0
    for value in reversed(states.tolist()):
        if value != states.iloc[-1]:
            break
        run += 1
    return {"symbol": symbol, "state": states.iloc[-1], "days_in_state": run,
            "ema_21": row["ema_21"], "ema_50": row["ema_50"], "ema_200": row["ema_200"],
            "adx": row["adx"], "as_of": row["date"]}
