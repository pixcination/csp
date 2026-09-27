"""
Data freshness -- how old each cache is, against thresholds in config.yaml.

One place that knows where every cache's "last updated" lives, so preflight
and the app's status views report the same ages. Added in Phase 8 after a
read of the tree found daily bars five weeks stale and the 1-minute cache
three months stale while preflight printed OK for both: it checked that the
files existed, not what they held.

Ages are measured from what the data contains (last bar date) wherever that
is cheap, and from file modification time otherwise -- a file rewritten today
with last month's data is not fresh.

Nothing here raises. A cache that cannot be read is reported as such.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from core.market_calendar import (ET, is_trading_day, previous_trading_day,
                                  trading_days_between)
from core.paths import (chains_dir, db_1m_cache, db_technicals, db_universe,
                        db_universe_daily, load_config, reference_dir)

OK, WARN, MISSING = "ok", "warn", "missing"

DEFAULTS = {
    "daily_bars_trading_days": 1,
    "cache_1m_days": 30,
    "earnings_days": 7,
    "dividends_days": 35,
    "reference_days": 3,
    "chains_days": 7,
    "market_metrics_days": 3,
    "events_days": 3,
    "technicals_sessions": 1,
}


@dataclass
class Freshness:
    key: str
    label: str
    status: str                 # ok | warn | missing
    last: str | None            # last bar / file time, as text
    age: str                    # human-readable age
    detail: str = ""


def thresholds() -> dict:
    return {**DEFAULTS, **(load_config().get("freshness") or {})}


def last_completed_session(now: dt.datetime | None = None) -> dt.date:
    """The most recent trading day whose daily bar is final."""
    now = now or dt.datetime.now(ET)
    today = now.date()
    if is_trading_day(today) and (now.hour, now.minute) >= (16, 15):
        return today
    return previous_trading_day(today)


def _file_age_days(path: Path) -> float:
    modified = dt.datetime.fromtimestamp(path.stat().st_mtime)
    return (dt.datetime.now() - modified).total_seconds() / 86400


def _daily_bars(limits: dict) -> Freshness:
    import duckdb
    path = db_universe_daily()
    label = "Daily bars"
    if not path.exists():
        return Freshness("daily", label, MISSING, None, "--", "run pipeline/run.py")
    try:
        con = duckdb.connect(str(path), read_only=True)
        try:
            row = con.execute("SELECT ticker, max(date) AS last FROM daily_bars_raw "
                              "GROUP BY ticker ORDER BY last LIMIT 1").fetchone()
        finally:
            con.close()
    except Exception as exc:
        return Freshness("daily", label, MISSING, None, "--",
                         f"daily_bars_raw unreadable ({str(exc)[:60]})")
    if not row:
        return Freshness("daily", label, MISSING, None, "--", "daily_bars_raw is empty")
    ticker, last = row
    target = last_completed_session()
    behind = trading_days_between(last, target)
    status = WARN if behind > limits["daily_bars_trading_days"] else OK
    detail = (f"stalest ticker {ticker}" if behind == 0 else
              f"stalest ticker {ticker}; last completed session {target}. "
              f"Run: python pipeline/run.py")
    return Freshness("daily", label, status, str(last),
                     f"{behind} session(s) behind", detail)


def _cache_1m(limits: dict) -> Freshness:
    import duckdb
    path = db_1m_cache()
    label = "1-minute cache"
    if not path.exists():
        return Freshness("cache_1m", label, MISSING, None, "--",
                         "gaps and intraday RV fall back to daily data")
    try:
        con = duckdb.connect(str(path), read_only=True)
        try:
            last = con.execute("SELECT max(datetime) FROM bars_1m").fetchone()[0]
        finally:
            con.close()
    except Exception as exc:
        return Freshness("cache_1m", label, MISSING, None, "--", str(exc)[:80])
    if last is None:
        return Freshness("cache_1m", label, MISSING, None, "--", "bars_1m is empty")
    days = (dt.datetime.now() - last).days
    status = WARN if days > limits["cache_1m_days"] else OK
    return Freshness("cache_1m", label, status, str(last.date()), f"{days}d old",
                     "" if status == OK else
                     "used by gaps.py and intraday RV. Refresh: scripts/06_build_1m_cache.py")


def _file(key: str, label: str, path: Path, limit_days: float, hint: str) -> Freshness:
    if not path.exists():
        return Freshness(key, label, MISSING, None, "--", hint)
    days = _file_age_days(path)
    modified = dt.datetime.fromtimestamp(path.stat().st_mtime)
    status = WARN if days > limit_days else OK
    age = f"{days * 24:.0f}h old" if days < 2 else f"{days:.0f}d old"
    return Freshness(key, label, status, modified.strftime("%Y-%m-%d %H:%M"), age,
                     "" if status == OK else hint)


def _universe_table(key: str, label: str, query: str, limit_days: float,
                    hint: str) -> Freshness:
    """Age of a Phase 9 table in data/universe.duckdb, from its own stamp."""
    import duckdb
    path = db_universe()
    if not path.exists():
        return Freshness(key, label, MISSING, None, "--", hint)
    try:
        con = duckdb.connect(str(path), read_only=True)
        try:
            last = con.execute(query).fetchone()[0]
        finally:
            con.close()
    except Exception:
        last = None
    if last is None:
        return Freshness(key, label, MISSING, None, "--", hint)
    last = pd_timestamp(last)
    days = (dt.datetime.now() - last).total_seconds() / 86400
    status = WARN if days > limit_days else OK
    age = f"{days * 24:.0f}h old" if days < 2 else f"{days:.0f}d old"
    return Freshness(key, label, status, last.strftime("%Y-%m-%d %H:%M"), age,
                     "" if status == OK else hint)


def pd_timestamp(value) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value
    return dt.datetime.combine(value, dt.time())


def _technicals(limits: dict) -> Freshness:
    """Level study / indicator cache: stalest symbol's as_of vs the last
    completed session (Phase 10)."""
    import duckdb
    path = db_technicals()
    label = "Technicals / level study"
    hint = "run: python pipeline/run.py --data-only"
    if not path.exists():
        return Freshness("technicals", label, MISSING, None, "--", hint)
    try:
        con = duckdb.connect(str(path), read_only=True)
        try:
            row = con.execute("SELECT min(as_of) FROM (SELECT symbol, max(as_of) AS as_of "
                              "FROM indicator_latest GROUP BY symbol)").fetchone()
        finally:
            con.close()
    except Exception:
        row = None
    if not row or row[0] is None:
        return Freshness("technicals", label, MISSING, None, "--", hint)
    last = row[0]
    behind = trading_days_between(last, last_completed_session())
    status = WARN if behind > limits["technicals_sessions"] else OK
    return Freshness("technicals", label, status, str(last), f"{behind} session(s) behind",
                     "" if status == OK else hint)


def _chains(limits: dict) -> Freshness:
    root = chains_dir()
    blocks = sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []
    if not blocks:
        return Freshness("chains", "Chain snapshots", MISSING, None, "--",
                         "run pipeline/run.py during market hours")
    newest = max(blocks, key=lambda p: p.stat().st_mtime)
    fresh = _file("chains", "Chain snapshots", newest, limits["chains_days"],
                  "premium figures are from an old snapshot; re-run the pipeline")
    fresh.last = f"{newest.name} ({fresh.last})"
    return fresh


def report() -> list[Freshness]:
    """Every cache's age, in the order a run depends on them."""
    limits = thresholds()
    ref = reference_dir()
    items = [
        _daily_bars(limits),
        _file("earnings", "Earnings calendar", ref / "earnings.parquet",
              limits["earnings_days"], "the earnings gate reads this; re-run the pipeline"),
        _file("dividends", "Dividend ex-dates", ref / "dividends.parquet",
              limits["dividends_days"], "ex-dividend warnings read this; re-run the pipeline"),
        _file("rates", "Treasury rates", ref / "treasury_rates.parquet",
              limits["reference_days"], "reference refresh"),
        _file("vol_indices", "Volatility indices", ref / "vol_indices.parquet",
              limits["reference_days"], "reference refresh"),
        _universe_table("metrics", "Market metrics (TastyTrade)",
                        "SELECT max(fetched_at) FROM market_metrics",
                        limits["market_metrics_days"], "IV rank / percentile; re-run the pipeline"),
        _universe_table("events", "Events table", "SELECT max(built_at) FROM events",
                        limits["events_days"], "earnings / macro / OPEX gates; re-run the pipeline"),
        _technicals(limits),
        _chains(limits),
        _cache_1m(limits),
    ]
    return items
