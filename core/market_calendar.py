"""
Market session awareness -- the fix for findings F-02 and F-12.

Two jobs:

1. **Classify the current moment.** The app must run at 9pm on a Saturday and
   still be useful, but it has to say plainly that it is showing Friday's
   closing marks. `classify()` returns everything the banner needs.

2. **Stamp every snapshot with the session it belongs to.** The old code wrote
   chains into `data/stage3_chains/<today>/` unconditionally, so a Friday,
   Saturday and Sunday scan produced three "independent" observations of one
   stale quote -- and IV rank is computed from exactly those observations.
   `session_block()` collapses everything from Friday's close to Monday's open
   into a single block key, so repeated weekend study runs overwrite one
   snapshot instead of manufacturing history.

The 2026-only literal holiday sets in `snapshot_loop.py` are replaced by
`pandas_market_calendars`, which carries the real NYSE schedule including
early closes for arbitrary years.
"""
from __future__ import annotations

import datetime as dt
import functools
from dataclasses import dataclass
from enum import Enum
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

REGULAR_OPEN = dt.time(9, 30)
REGULAR_CLOSE = dt.time(16, 0)
PREMARKET_OPEN = dt.time(4, 0)
AFTERHOURS_CLOSE = dt.time(20, 0)


class SessionState(str, Enum):
    RTH = "rth"                    # regular trading hours, quotes are live
    PREMARKET = "premarket"        # 04:00-09:30 ET, thin but real
    AFTERHOURS = "afterhours"      # 16:00-20:00 ET, thin but real
    CLOSED_OVERNIGHT = "overnight"  # weeknight between 20:00 and 04:00
    CLOSED_WEEKEND = "weekend"
    CLOSED_HOLIDAY = "holiday"

    @property
    def quotes_are_live(self) -> bool:
        return self in (SessionState.RTH, SessionState.PREMARKET, SessionState.AFTERHOURS)

    @property
    def is_regular(self) -> bool:
        return self is SessionState.RTH


@dataclass(frozen=True)
class SessionInfo:
    state: SessionState
    now_et: dt.datetime
    trading_day: dt.date | None       # the session date, if one is in progress
    last_close: dt.datetime | None    # most recent regular-session close
    next_open: dt.datetime | None     # next regular-session open
    early_close: bool = False

    @property
    def is_open(self) -> bool:
        return self.state.is_regular

    @property
    def staleness(self) -> dt.timedelta | None:
        """How old the newest available regular-session print is."""
        if self.state.is_regular or self.last_close is None:
            return dt.timedelta(0)
        return self.now_et - self.last_close

    @property
    def staleness_label(self) -> str:
        gap = self.staleness
        if gap is None:
            return "unknown"
        if gap <= dt.timedelta(minutes=1):
            return "live"
        hours = gap.total_seconds() / 3600
        if hours < 24:
            return f"{hours:.0f}h old"
        return f"{gap.days}d {int(hours % 24)}h old"

    def banner(self) -> tuple[str, str]:
        """(severity, message) for the Command Center header.

        Deliberately specific rather than a generic 'market is closed' -- the
        useful information is what the numbers can and cannot be trusted for.
        """
        if self.state is SessionState.RTH:
            note = " (early close today at 13:00 ET)" if self.early_close else ""
            return ("ok", f"Regular session open{note}. Quotes, Greeks and spreads are live.")

        close_txt = (self.last_close.strftime("%a %Y-%m-%d %H:%M ET")
                     if self.last_close else "unknown")
        open_txt = (self.next_open.strftime("%a %Y-%m-%d %H:%M ET")
                    if self.next_open else "unknown")
        common = (f"Marks are from the {close_txt} close ({self.staleness_label}). "
                  f"Next regular session opens {open_txt}. "
                  f"Directional and probability analysis is valid; premium and "
                  f"spread figures are not tradable estimates.")

        if self.state in (SessionState.PREMARKET, SessionState.AFTERHOURS):
            phase = "Pre-market" if self.state is SessionState.PREMARKET else "After-hours"
            return ("info", f"{phase} session. Option quotes are thin and spreads "
                            f"are much wider than they will be at the open. " + common)
        if self.state is SessionState.CLOSED_HOLIDAY:
            return ("warn", "Market holiday. " + common)
        if self.state is SessionState.CLOSED_WEEKEND:
            return ("warn", "Weekend. " + common)
        return ("warn", "Market closed. " + common)


# --- Calendar backend ------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _calendar():
    """NYSE schedule. Returns None if pandas_market_calendars is unavailable,
    in which case we degrade to a weekday-only approximation and say so."""
    try:
        import pandas_market_calendars as mcal
        return mcal.get_calendar("NYSE")
    except Exception:
        return None


@functools.lru_cache(maxsize=64)
def _schedule_for_year(year: int):
    cal = _calendar()
    if cal is None:
        return None
    sched = cal.schedule(start_date=f"{year}-01-01", end_date=f"{year}-12-31")
    # Index is the session date; market_open/market_close are tz-aware UTC.
    return sched


def calendar_available() -> bool:
    return _calendar() is not None


def _session_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime] | None:
    """(open, close) in ET for a trading day, or None if the market is shut."""
    sched = _schedule_for_year(day.year)
    if sched is None:
        # Degraded mode: weekdays only, standard hours, no holiday awareness.
        if day.weekday() >= 5:
            return None
        return (dt.datetime.combine(day, REGULAR_OPEN, ET),
                dt.datetime.combine(day, REGULAR_CLOSE, ET))
    import pandas as pd
    key = pd.Timestamp(day)
    if key not in sched.index:
        return None
    row = sched.loc[key]
    return (row["market_open"].tz_convert(ET).to_pydatetime(),
            row["market_close"].tz_convert(ET).to_pydatetime())


def is_trading_day(day: dt.date) -> bool:
    return _session_bounds(day) is not None


def previous_trading_day(day: dt.date) -> dt.date:
    probe = day - dt.timedelta(days=1)
    for _ in range(12):
        if is_trading_day(probe):
            return probe
        probe -= dt.timedelta(days=1)
    return probe


def next_trading_day(day: dt.date) -> dt.date:
    probe = day + dt.timedelta(days=1)
    for _ in range(12):
        if is_trading_day(probe):
            return probe
        probe += dt.timedelta(days=1)
    return probe


def trading_days_between(start: dt.date, end: dt.date) -> int:
    """Inclusive of `end`, exclusive of `start`. Used wherever theta decay or
    realized-vol scaling needs sessions rather than calendar days."""
    if end <= start:
        return 0
    sched = _schedule_for_year(start.year)
    if sched is None or start.year != end.year:
        # Cheap approximation across a year boundary or in degraded mode.
        days = 0
        probe = start + dt.timedelta(days=1)
        while probe <= end:
            if is_trading_day(probe):
                days += 1
            probe += dt.timedelta(days=1)
        return days
    import pandas as pd
    mask = (sched.index > pd.Timestamp(start)) & (sched.index <= pd.Timestamp(end))
    return int(mask.sum())


# --- Classification --------------------------------------------------------

def now_et() -> dt.datetime:
    return dt.datetime.now(ET)


def classify(when: dt.datetime | None = None) -> SessionInfo:
    """Classify a moment into a SessionState with the surrounding context."""
    moment = (when or now_et()).astimezone(ET)
    day = moment.date()
    bounds = _session_bounds(day)

    last_close = _most_recent_close(moment)
    next_open_dt = _next_open(moment)

    if bounds is None:
        state = (SessionState.CLOSED_WEEKEND if moment.weekday() >= 5
                 else SessionState.CLOSED_HOLIDAY)
        return SessionInfo(state, moment, None, last_close, next_open_dt)

    open_dt, close_dt = bounds
    early = close_dt.time() < REGULAR_CLOSE

    if open_dt <= moment < close_dt:
        return SessionInfo(SessionState.RTH, moment, day, last_close, next_open_dt, early)

    pre_start = dt.datetime.combine(day, PREMARKET_OPEN, ET)
    post_end = dt.datetime.combine(day, AFTERHOURS_CLOSE, ET)
    if pre_start <= moment < open_dt:
        return SessionInfo(SessionState.PREMARKET, moment, day, last_close, next_open_dt, early)
    if close_dt <= moment < post_end:
        return SessionInfo(SessionState.AFTERHOURS, moment, day, last_close, next_open_dt, early)
    return SessionInfo(SessionState.CLOSED_OVERNIGHT, moment, None, last_close, next_open_dt, early)


def _most_recent_close(moment: dt.datetime) -> dt.datetime | None:
    probe = moment.date()
    for _ in range(12):
        bounds = _session_bounds(probe)
        if bounds is not None and bounds[1] <= moment:
            return bounds[1]
        probe -= dt.timedelta(days=1)
    return None


def _next_open(moment: dt.datetime) -> dt.datetime | None:
    probe = moment.date()
    for _ in range(12):
        bounds = _session_bounds(probe)
        if bounds is not None and bounds[0] > moment:
            return bounds[0]
        probe += dt.timedelta(days=1)
    return None


# --- Snapshot identity -----------------------------------------------------

def session_block(when: dt.datetime | None = None) -> str:
    """A stable key naming the market state a snapshot belongs to.

    Every capture taken between Friday's close and Monday's open gets the same
    key, so weekend study runs overwrite one file instead of fabricating three
    IV observations. RTH captures are bucketed by the hour so intraday
    re-scans accumulate a usable intraday term structure without exploding.

    Examples:
        2026-08-21_rth_14      Friday, 2pm ET, live
        2026-08-21_post        Friday after-hours
        2026-08-21_closed      Friday night through Monday's open
        2026-08-24_pre         Monday pre-market
    """
    info = classify(when)
    state, moment = info.state, info.now_et
    if state is SessionState.RTH:
        return f"{info.trading_day.isoformat()}_rth_{moment.hour:02d}"
    if state is SessionState.PREMARKET:
        return f"{moment.date().isoformat()}_pre"
    if state is SessionState.AFTERHOURS:
        return f"{moment.date().isoformat()}_post"
    # Everything from one close to the next open shares the close's date.
    anchor = info.last_close.date() if info.last_close else moment.date()
    return f"{anchor.isoformat()}_closed"


def is_ingestable_for_iv_history(block: str) -> bool:
    """Only regular-session captures become IV-rank observations.

    Pre-market, after-hours and weekend marks are last-trade echoes with wide
    synthetic spreads; treating them as independent samples is what corrupted
    the metric in the first place.
    """
    return "_rth_" in block


if __name__ == "__main__":  # pragma: no cover
    info = classify()
    sev, msg = info.banner()
    print(f"now      : {info.now_et:%Y-%m-%d %H:%M:%S %Z}")
    print(f"state    : {info.state.value}")
    print(f"block    : {session_block()}")
    print(f"calendar : {'pandas_market_calendars' if calendar_available() else 'DEGRADED weekday-only'}")
    print(f"banner   : [{sev}] {msg}")
