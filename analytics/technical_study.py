"""
Nightly technicals: indicators, trend state, level-respect and RSI studies
for every active symbol, cached in data/technicals.duckdb (Phase 10).

    indicator_latest   (symbol, as_of, name, value, text) -- the last row of
                       indicators.with_weekly + trend_state, long format so a
                       new indicator needs no schema change
    level_stats        analytics/level_respect.py
    oscillator_stats   analytics/oscillator_study.py

`run()` is the pipeline's `technicals` stage. It skips a symbol whose cached
stats are already as of its latest daily bar, so a second run on the same
day costs nothing.
"""
from __future__ import annotations

import datetime as dt

import duckdb
import numpy as np
import pandas as pd

from core.paths import db_technicals
from core.progress import BaseReporter, NullReporter

LATEST = "indicator_latest"


def _cached_as_of() -> dict[str, dt.date]:
    path = db_technicals()
    if not path.exists():
        return {}
    con = duckdb.connect(str(path), read_only=True)
    try:
        if LATEST not in {r[0] for r in con.execute("SHOW TABLES").fetchall()}:
            return {}
        return {s: d for s, d in con.execute(
            f"SELECT symbol, max(as_of) FROM {LATEST} GROUP BY symbol").fetchall()}
    finally:
        con.close()


def store_latest(symbol: str, frame: pd.DataFrame, state: str | None) -> None:
    last = frame.iloc[-1]
    as_of = pd.Timestamp(last["date"]).date()
    rows = [{"symbol": symbol, "as_of": as_of, "name": k, "value": float(v), "text": None}
            for k, v in last.items()
            if k != "date" and isinstance(v, (int, float, np.floating)) and np.isfinite(v)]
    rows.append({"symbol": symbol, "as_of": as_of, "name": "trend_state",
                 "value": None, "text": state})
    incoming = pd.DataFrame(rows)
    con = duckdb.connect(str(db_technicals()))
    try:
        con.execute(f"CREATE TABLE IF NOT EXISTS {LATEST} (symbol VARCHAR, as_of DATE, "
                    f"name VARCHAR, value DOUBLE, text VARCHAR)")
        con.register("incoming", incoming)
        con.execute(f"DELETE FROM {LATEST} WHERE symbol = ?", [symbol])
        con.execute(f"INSERT INTO {LATEST} SELECT symbol, as_of, name, value, text FROM incoming")
        con.unregister("incoming")
    finally:
        con.close()


def load_latest(symbol: str | None = None) -> pd.DataFrame:
    """Wide: one row per symbol, one column per indicator (+ trend_state)."""
    path = db_technicals()
    if not path.exists():
        return pd.DataFrame()
    con = duckdb.connect(str(path), read_only=True)
    try:
        if LATEST not in {r[0] for r in con.execute("SHOW TABLES").fetchall()}:
            return pd.DataFrame()
        query = f"SELECT * FROM {LATEST}" + (" WHERE symbol = ?" if symbol else "")
        long = con.execute(query, [symbol] if symbol else []).fetchdf()
    finally:
        con.close()
    if long.empty:
        return long
    wide = long.pivot_table(index=["symbol", "as_of"], columns="name", values="value",
                            aggfunc="first").reset_index()
    states = long[long["name"] == "trend_state"][["symbol", "text"]].rename(
        columns={"text": "trend_state"})
    return wide.merge(states, on="symbol", how="left")


def study_symbol(symbol: str) -> dict:
    from analytics import indicators, level_respect, oscillator_study, trend_state
    frame = indicators.for_symbol(symbol)
    if frame.empty or len(frame) < 260:
        return {"symbol": symbol, "skipped": "not enough history"}
    states = trend_state.classify(frame)
    store_latest(symbol, frame, states.iloc[-1])
    levels = level_respect.study(frame, symbol)
    level_respect.store(symbol, levels)
    osc = oscillator_study.study(frame, symbol)
    oscillator_study.store(symbol, osc)
    headline = levels[(levels["horizon"] == level_respect.Params.from_config().headline_horizon)
                      & (levels["slope_regime"] == "all")] if not levels.empty else levels
    strong = headline[(headline["status"] == "ok") & (headline["edge_ci_lo"] > 0)]
    return {"symbol": symbol, "trend_state": states.iloc[-1],
            "levels_ok": int((headline["status"] == "ok").sum()) if len(headline) else 0,
            "strong_levels": strong["level_id"].tolist()}


def run(symbols: list[str], reporter: BaseReporter | None = None,
        force: bool = False) -> dict:
    from data_sources.yfinance_sync import load_daily

    reporter = reporter or NullReporter()
    cached = {} if force else _cached_as_of()
    results, skipped, errors = [], 0, []
    with reporter.stage("technicals", "Technicals and level study", total=len(symbols)):
        for symbol in symbols:
            try:
                bars = load_daily(symbol, basis="price", start=dt.date.today() - dt.timedelta(days=10))
                last = pd.Timestamp(bars["date"].max()).date() if not bars.empty else None
                if last and cached.get(symbol) == last:
                    skipped += 1
                    reporter.advance(1, note=f"{symbol} current")
                    continue
                result = study_symbol(symbol)
                results.append(result)
                note = result.get("skipped") or (
                    f"{symbol} {result['trend_state']}, {len(result['strong_levels'])} strong")
                reporter.advance(1, note=note)
            except Exception as exc:
                errors.append(f"{symbol}: {type(exc).__name__}: {str(exc)[:100]}")
                reporter.advance(1, note=f"{symbol} error")
    return {"studied": len(results), "skipped_current": skipped, "errors": errors,
            "strong": {r["symbol"]: r["strong_levels"] for r in results
                       if r.get("strong_levels")}}
