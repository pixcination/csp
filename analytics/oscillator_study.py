"""
RSI extremes -- what has this stock done over the next 5/10/20 sessions
after RSI went below 30 or above 70, daily and weekly, compared with any
day at all? (Phase 10, roadmap §B.3.)

EPISODES, NOT DAYS
------------------
RSI stays under 30 for several days at a time. Counting every such day
would count one selloff five times and report a sample five times too
confident. An episode starts on the first day RSI crosses into the zone;
the forward return is measured from that day's close. `n` is the number of
episodes. The unconditional baseline uses every day (overlapping windows),
with its effective n reported the same way `moves.py` does.

Weekly RSI comes from the last COMPLETED week (indicators.with_weekly), so a
weekly episode is dated to the day its week closed -- never earlier.

Output per (symbol, timeframe, condition, horizon): n, mean / median /
p10 / p90 forward return, win rate (share positive), and the same for the
unconditional baseline, plus the median difference. Price basis. Cached in
data/technicals.duckdb -> oscillator_stats.
"""
from __future__ import annotations

import datetime as dt

import duckdb
import numpy as np
import pandas as pd

from core.paths import db_technicals, load_config

TABLE = "oscillator_stats"


def _cfg() -> dict:
    return load_config().get("oscillator_study", {}) or {}


def episode_starts(rsi: pd.Series, condition: str, threshold: float) -> np.ndarray:
    """Positions where RSI first enters the zone (below / above threshold)."""
    inside = (rsi < threshold) if condition == "below" else (rsi > threshold)
    inside = inside.fillna(False).to_numpy()
    before = np.concatenate([[False], inside[:-1]])
    return np.flatnonzero(inside & ~before)


def forward_returns(close: np.ndarray, starts: np.ndarray, horizon: int) -> np.ndarray:
    starts = starts[starts + horizon < len(close)]
    return close[starts + horizon] / close[starts] - 1.0


def _describe(values: np.ndarray) -> dict:
    if len(values) == 0:
        return {"n": 0, "mean": np.nan, "median": np.nan, "p10": np.nan,
                "p90": np.nan, "win_rate": np.nan}
    return {"n": int(len(values)), "mean": float(values.mean()),
            "median": float(np.median(values)), "p10": float(np.quantile(values, 0.1)),
            "p90": float(np.quantile(values, 0.9)), "win_rate": float((values > 0).mean())}


def study(frame: pd.DataFrame, symbol: str = "") -> pd.DataFrame:
    """`frame` = indicators.with_weekly(daily)."""
    cfg = _cfg()
    low, high = cfg.get("rsi_low", 30), cfg.get("rsi_high", 70)
    horizons = cfg.get("horizons", [5, 10, 20])
    years = cfg.get("lookback_years", 20)
    if frame is None or frame.empty:
        return pd.DataFrame()
    cutoff = pd.to_datetime(frame["date"]).max() - pd.DateOffset(years=years)
    frame = frame[pd.to_datetime(frame["date"]) >= cutoff].reset_index(drop=True)
    close = frame["close"].to_numpy(float)
    rows = []
    for timeframe, column in (("D", "rsi"), ("W", "w_rsi")):
        if column not in frame:
            continue
        rsi = frame[column]
        for horizon in horizons:
            base = forward_returns(close, np.arange(len(close)), horizon)
            b = _describe(base)
            for condition, threshold in (("below", low), ("above", high)):
                starts = episode_starts(rsi, condition, threshold)
                d = _describe(forward_returns(close, starts, horizon))
                rows.append({
                    "symbol": symbol, "timeframe": timeframe,
                    "condition": f"rsi {'<' if condition == 'below' else '>'} {threshold}",
                    "horizon": horizon, **d,
                    "base_n": b["n"], "base_effective_n": int(b["n"] / max(horizon, 1)),
                    "base_mean": b["mean"], "base_median": b["median"],
                    "base_win_rate": b["win_rate"],
                    "median_minus_base": d["median"] - b["median"] if d["n"] else np.nan,
                    "last_episode": (pd.Timestamp(frame["date"].iloc[starts[-1]]).date()
                                     if len(starts) else None),
                    "in_zone_now": bool((rsi.iloc[-1] < threshold) if condition == "below"
                                        else (rsi.iloc[-1] > threshold))
                    if pd.notna(rsi.iloc[-1]) else False,
                })
    out = pd.DataFrame(rows)
    out["as_of"] = pd.Timestamp(frame["date"].iloc[-1]).date()
    return out


def store(symbol: str, stats: pd.DataFrame) -> None:
    if stats is None or stats.empty:
        return
    stats = stats.assign(computed_at=dt.datetime.now())
    con = duckdb.connect(str(db_technicals()))
    try:
        con.register("incoming", stats)
        if TABLE not in {r[0] for r in con.execute("SHOW TABLES").fetchall()}:
            con.execute(f"CREATE TABLE {TABLE} AS SELECT * FROM incoming WHERE false")
        con.execute(f"DELETE FROM {TABLE} WHERE symbol = ?", [symbol])
        con.execute(f"INSERT INTO {TABLE} BY NAME SELECT * FROM incoming")
        con.unregister("incoming")
    finally:
        con.close()


def load_stats(symbol: str | None = None) -> pd.DataFrame:
    path = db_technicals()
    if not path.exists():
        return pd.DataFrame()
    con = duckdb.connect(str(path), read_only=True)
    try:
        if TABLE not in {r[0] for r in con.execute("SHOW TABLES").fetchall()}:
            return pd.DataFrame()
        query = f"SELECT * FROM {TABLE}" + (" WHERE symbol = ?" if symbol else "")
        return con.execute(query, [symbol] if symbol else []).fetchdf()
    finally:
        con.close()
