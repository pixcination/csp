"""
Weekly bars, resampled locally from daily -- and the no-lookahead rule.

Weekly bars are never downloaded separately (roadmap §B.2): a second vendor
series would need its own adjustment handling and could disagree with the
daily one. They are resampled from `load_daily` on whichever price basis the
caller asks for.

THE LOOKAHEAD TRAP
------------------
A weekly bar labelled Friday 2026-09-25 is built from Monday-Friday closes.
Joined naively onto a daily timeline, Wednesday 2026-09-23 would "see" that
Friday's close -- two days in the future. Any study of whether price respects
a weekly moving average is then contaminated, and it looks good precisely
because it cheats.

`weekly_on_daily` therefore attaches to each day the value of the last week
whose final session is on or before that day. On the final session itself
the week is complete at the close, so it becomes usable from that close. The
final session is taken from the exchange calendar (known in advance), never
from the data -- so a Good Friday week completes on Thursday without the
code peeking at whether Friday has a bar.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

WEEK_RULE = "W-FRI"


def weekly(daily: pd.DataFrame) -> pd.DataFrame:
    """Daily OHLCV -> weekly (W-FRI) bars.

    Columns: week_end (the Friday label), last_session (the actual last bar in
    the week), open, high, low, close, volume, sessions, complete. The final
    week is marked incomplete unless its last calendar session has traded.
    """
    if daily is None or daily.empty:
        return pd.DataFrame(columns=["week_end", "last_session", "open", "high", "low",
                                     "close", "volume", "sessions", "complete"])
    frame = daily.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.sort_values("date").set_index("date")
    grouped = frame.resample(WEEK_RULE)
    out = pd.DataFrame({
        "open": grouped["open"].first(),
        "high": grouped["high"].max(),
        "low": grouped["low"].min(),
        "close": grouped["close"].last(),
        "volume": grouped["volume"].sum() if "volume" in frame else 0.0,
        "sessions": grouped["close"].count(),
    })
    last_session = frame["close"].groupby(pd.Grouper(freq=WEEK_RULE)).apply(
        lambda s: s.index.max())
    out["last_session"] = last_session
    out = out[out["sessions"] > 0].reset_index().rename(columns={"date": "week_end"})

    out["scheduled_last_session"] = _scheduled_last_sessions(out["week_end"])
    out["complete"] = out["last_session"] >= out["scheduled_last_session"]
    if daily.attrs.get("price_basis"):
        out.attrs["price_basis"] = daily.attrs["price_basis"]
    return out[["week_end", "last_session", "scheduled_last_session", "open", "high",
                "low", "close", "volume", "sessions", "complete"]]


def _scheduled_last_sessions(week_ends: pd.Series) -> pd.Series:
    """Vectorised `week_last_session`: one exchange-calendar query for the
    whole range instead of one lookup per week (98 years of SPX: 5 s -> <1 s)."""
    week_ends = pd.to_datetime(week_ends)
    try:
        from core.market_calendar import _calendar
        cal = _calendar()
        sessions = pd.Series(pd.to_datetime(cal.valid_days(
            start_date=week_ends.min() - pd.Timedelta(days=6),
            end_date=week_ends.max())).tz_localize(None))
        last = sessions.groupby(sessions.dt.to_period(WEEK_RULE)).max()
        last.index = last.index.end_time.normalize()
        mapped = week_ends.map(last)
        missing = mapped.isna()
        if missing.any():
            mapped[missing] = week_ends[missing].map(
                lambda d: pd.Timestamp(week_last_session(d.date())))
        return pd.to_datetime(mapped).reset_index(drop=True)
    except Exception:
        return pd.to_datetime([week_last_session(d.date()) for d in week_ends])


def week_last_session(friday: dt.date) -> dt.date:
    """The last scheduled trading day of the week ending `friday` (exchange
    calendar; falls back to Friday if the calendar is unavailable)."""
    try:
        from core.market_calendar import is_trading_day
        probe = friday
        for _ in range(5):
            if is_trading_day(probe):
                return probe
            probe -= dt.timedelta(days=1)
    except Exception:
        pass
    return friday


def weekly_positions(daily: pd.DataFrame, weekly_frame: pd.DataFrame) -> np.ndarray:
    """For each daily row, the positional index into `weekly_frame` of the
    last week whose scheduled last session is <= that day (-1 = none yet).

    The alignment is computed once and reused for every weekly column --
    `indicators.with_weekly` attaches ~40 of them."""
    available = pd.to_datetime(weekly_frame["scheduled_last_session"]).to_numpy(
        dtype="datetime64[ns]")
    days = pd.to_datetime(daily["date"]).to_numpy(dtype="datetime64[ns]")
    order = np.argsort(available, kind="stable")
    found = np.searchsorted(available[order], days, side="right") - 1
    return np.where(found >= 0, order[np.clip(found, 0, None)], -1)


def weekly_on_daily(daily: pd.DataFrame, weekly_values: pd.Series | None = None,
                    weekly_frame: pd.DataFrame | None = None,
                    positions: np.ndarray | None = None) -> pd.Series:
    """Align a weekly series onto daily dates with no lookahead.

    `weekly_values` is indexed like `weekly(daily)` rows (positional) or by
    week_end; each daily date gets the value of the most recent week whose
    scheduled last session is <= that date. Defaults to the weekly close.
    """
    wk = weekly_frame if weekly_frame is not None else weekly(daily)
    if weekly_values is None:
        values = wk["close"].to_numpy(dtype=float)
    elif isinstance(weekly_values.index, pd.DatetimeIndex):
        values = weekly_values.reindex(pd.to_datetime(wk["week_end"])).to_numpy(dtype=float)
    else:
        values = weekly_values.to_numpy(dtype=float)
    pos = positions if positions is not None else weekly_positions(daily, wk)
    out = np.where(pos >= 0, values[np.clip(pos, 0, None)], np.nan)
    return pd.Series(out, index=pd.to_datetime(daily["date"]).to_numpy(), name="weekly")


def last_completed_week(daily: pd.DataFrame, as_of: dt.date) -> pd.Series | None:
    """The weekly bar that was complete at `as_of`'s close, or None."""
    wk = weekly(daily[pd.to_datetime(daily["date"]) <= pd.Timestamp(as_of)])
    wk = wk[wk["scheduled_last_session"] <= pd.Timestamp(as_of)]
    return None if wk.empty else wk.iloc[-1]


def load_weekly(symbol: str, basis: str = "price") -> pd.DataFrame:
    """Weekly bars for a registry symbol, resampled from `load_daily`."""
    from data_sources.yfinance_sync import load_daily
    return weekly(load_daily(symbol, basis=basis))
