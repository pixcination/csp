"""
TastyTrade market metrics -- IV rank, IV percentile, liquidity, expected
earnings, per-expiration IV (Phase 9).

Fixes the dead IV-rank component on day one: our own IV rank needs 10+
capture dates per ticker (`analytics/iv_history.py`), and after two months
there are three. TastyTrade computes it from a year of its own history. We
keep ours as a cross-check and store theirs daily, so our own history of
*their* number accumulates too.

ENDPOINT AND FIELDS -- verified live 2026-09-27 (GET /market-metrics?symbols=)
-------------------------------------------------------------------------------
Every numeric value arrives as a STRING. Units differ by field:

    implied-volatility-index            fraction (0.2375 = 23.75%)
    implied-volatility-index-rank       fraction 0-1; = the "tos" rank
    implied-volatility-index-rank-source  "tos"
    tos-implied-volatility-index-rank   fraction; same as the headline rank
    tw-implied-volatility-index-rank    fraction; tastyworks' own method --
                                        differs materially (AAPL 0.43 vs 0.25)
    implied-volatility-percentile       fraction 0-1
    implied-volatility-30-day           PERCENT (23.75)   <- different unit
    historical-volatility-30/60/90-day  PERCENT
    liquidity-rating                    int 0-5 (0 for XSP/RUT: rated illiquid)
    liquidity-rank, liquidity-value     fractions / raw
    beta, corr-spy-3month               fractions
    earnings{expected-report-date, estimated, visible, ...}  nested; None for
                                        indices; ETFs have visible=false
    dividend-ex-date                    the LAST ex-date
    dividend-next-date                  STALE (2022 dates seen) -- never used
    sector, industry, market-cap, lendability, borrow-rate
    option-expiration-implied-volatilities[]  expiration-date,
                                        option-chain-type, settlement-type
                                        (AM | PM; indices list both),
                                        implied-volatility

Symbols: indices by bare symbol (SPX, NDX, RUT, XSP); class shares with a
slash (BRK/B) -- the registry's tt_symbol.
"""
from __future__ import annotations

import datetime as dt
import json

import duckdb
import pandas as pd

from core.market_calendar import ET
from core.paths import db_universe, load_config
from core.progress import BaseReporter, NullReporter

TABLE = "market_metrics"
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    symbol VARCHAR, snapshot_date DATE, fetched_at TIMESTAMP,
    iv_index DOUBLE, iv_index_15d DOUBLE, iv_index_5d_change DOUBLE,
    ivr DOUBLE, ivr_source VARCHAR, ivr_tos DOUBLE, ivr_tw DOUBLE, ivp DOUBLE,
    iv_30d DOUBLE, hv_30d DOUBLE, hv_60d DOUBLE, hv_90d DOUBLE,
    iv_updated_at TIMESTAMP,
    liquidity_rating INTEGER, liquidity_rank DOUBLE, liquidity_value DOUBLE,
    beta DOUBLE, corr_spy_3m DOUBLE,
    earnings_date DATE, earnings_estimated BOOLEAN, earnings_visible BOOLEAN,
    dividend_ex_date DATE, dividend_yield DOUBLE,
    sector VARCHAR, industry VARCHAR, market_cap DOUBLE, borrow_rate DOUBLE,
    n_expirations INTEGER, weeklies BOOLEAN, settlement_times VARCHAR,
    expirations_json VARCHAR,
    PRIMARY KEY (symbol, snapshot_date)
)
"""

#: Expirations inside this many calendar days needed to call a name "weekly".
WEEKLY_WINDOW_DAYS = 35
WEEKLY_MIN_EXPIRATIONS = 3


def _num(value, scale: float = 1.0):
    try:
        return None if value in (None, "") else float(value) * scale
    except (TypeError, ValueError):
        return None


def _date(value):
    try:
        return None if not value else dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _stamp(value):
    try:
        return None if not value else pd.Timestamp(value).tz_localize(None).to_pydatetime()
    except Exception:
        try:
            return pd.Timestamp(value).tz_convert(None).to_pydatetime()
        except Exception:
            return None


def parse(item: dict, symbol: str, today: dt.date) -> dict:
    """One /market-metrics item -> a row. `symbol` is the canonical symbol."""
    earnings = item.get("earnings") or {}
    expirations = item.get("option-expiration-implied-volatilities") or []
    future = [e for e in expirations
              if (_date(e.get("expiration-date")) or dt.date.min) >= today]
    soon = [e for e in future
            if (_date(e.get("expiration-date")) - today).days <= WEEKLY_WINDOW_DAYS]
    settlement = sorted({e.get("settlement-type") for e in future if e.get("settlement-type")})
    return {
        "symbol": symbol, "snapshot_date": today, "fetched_at": dt.datetime.now(),
        "iv_index": _num(item.get("implied-volatility-index")),
        "iv_index_15d": _num(item.get("implied-volatility-index-15-day")),
        "iv_index_5d_change": _num(item.get("implied-volatility-index-5-day-change")),
        "ivr": _num(item.get("implied-volatility-index-rank")),
        "ivr_source": item.get("implied-volatility-index-rank-source"),
        "ivr_tos": _num(item.get("tos-implied-volatility-index-rank")),
        "ivr_tw": _num(item.get("tw-implied-volatility-index-rank")),
        "ivp": _num(item.get("implied-volatility-percentile")),
        "iv_30d": _num(item.get("implied-volatility-30-day"), 0.01),
        "hv_30d": _num(item.get("historical-volatility-30-day"), 0.01),
        "hv_60d": _num(item.get("historical-volatility-60-day"), 0.01),
        "hv_90d": _num(item.get("historical-volatility-90-day"), 0.01),
        "iv_updated_at": _stamp(item.get("implied-volatility-updated-at")),
        "liquidity_rating": (int(item["liquidity-rating"])
                             if item.get("liquidity-rating") is not None else None),
        "liquidity_rank": _num(item.get("liquidity-rank")),
        "liquidity_value": _num(item.get("liquidity-value")),
        "beta": _num(item.get("beta")),
        "corr_spy_3m": _num(item.get("corr-spy-3month")),
        "earnings_date": _date(earnings.get("expected-report-date")),
        "earnings_estimated": (bool(earnings["estimated"])
                               if "estimated" in earnings else None),
        "earnings_visible": (bool(earnings["visible"]) if "visible" in earnings else None),
        "dividend_ex_date": _date(item.get("dividend-ex-date")),
        "dividend_yield": _num(item.get("dividend-yield")),
        "sector": item.get("sector"), "industry": item.get("industry"),
        "market_cap": _num(item.get("market-cap")),
        "borrow_rate": _num(item.get("borrow-rate")),
        "n_expirations": len(future),
        "weeklies": len(soon) >= WEEKLY_MIN_EXPIRATIONS,
        "settlement_times": "+".join(settlement) if settlement else None,
        "expirations_json": json.dumps([
            {"expiration": e.get("expiration-date"), "type": e.get("option-chain-type"),
             "settlement": e.get("settlement-type"),
             "iv": _num(e.get("implied-volatility"))} for e in future]),
    }


def _fetch(tt_symbols: list[str]) -> list[dict]:
    from data_sources import tastytrade_client as tt
    ttc = tt.common()
    tt.authenticate()
    response = ttc._authed_get(f"{ttc.BASE_URL}/market-metrics",
                               params={"symbols": ",".join(tt_symbols)})
    response.raise_for_status()
    return response.json().get("data", {}).get("items", [])


def _connect(read_only: bool = False):
    path = db_universe()
    if read_only and not path.exists():
        read_only = False
    con = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA)
    return con


def store(rows: list[dict]) -> int:
    if not rows:
        return 0
    frame = pd.DataFrame(rows)
    con = _connect()
    try:
        columns = [r[0] for r in con.execute(f"DESCRIBE {TABLE}").fetchall()]
        for column in columns:
            if column not in frame.columns:
                frame[column] = None
        con.register("incoming", frame[columns])
        con.execute(f"DELETE FROM {TABLE} WHERE (symbol, snapshot_date) IN "
                    f"(SELECT symbol, snapshot_date FROM incoming)")
        con.execute(f"INSERT INTO {TABLE} SELECT * FROM incoming")
        con.unregister("incoming")
    finally:
        con.close()
    return len(frame)


def sync(symbols: list[str], reporter: BaseReporter | None = None,
         force: bool = False, batch_size: int | None = None) -> dict:
    """Fetch and store today's metrics for `symbols` (canonical)."""
    from data_sources import universe

    reporter = reporter or NullReporter()
    batch_size = batch_size or load_config().get("market_metrics", {}).get("batch_size", 50)
    today = dt.datetime.now(ET).date()
    have = set() if force else set(latest(max_age_days=0)["symbol"])
    todo = [s for s in symbols if s not in have]
    tt_map = universe.tt_symbols(todo) if todo else {}
    back = {v.upper(): k for k, v in tt_map.items()}
    rows, missing, errors = [], [], []

    with reporter.stage("metrics", "Market metrics", total=max(len(todo), 1)):
        if not todo:
            reporter.skip("today's snapshot already stored")
        for i in range(0, len(todo), batch_size):
            chunk = todo[i:i + batch_size]
            try:
                items = _fetch([tt_map[s] for s in chunk])
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {str(exc)[:120]}")
                reporter.advance(len(chunk), note="batch failed")
                continue
            seen = set()
            for item in items:
                symbol = back.get(str(item.get("symbol", "")).upper())
                if symbol:
                    rows.append(parse(item, symbol, today))
                    seen.add(symbol)
            missing += [s for s in chunk if s not in seen]
            reporter.advance(len(chunk), note=f"{len(seen)}/{len(chunk)} returned")

    stored = store(rows)
    if rows:
        universe.apply_market_metrics(pd.DataFrame(rows))
    return {"stored": stored, "skipped_current": len(symbols) - len(todo),
            "missing": missing, "errors": errors}


def latest(symbols: list[str] | None = None, max_age_days: int | None = None) -> pd.DataFrame:
    """The newest snapshot per symbol (optionally no older than max_age_days)."""
    con = _connect(read_only=True)
    try:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        if TABLE not in tables:
            return pd.DataFrame(columns=["symbol", "snapshot_date"])
        frame = con.execute(
            f"SELECT * FROM {TABLE} QUALIFY row_number() OVER "
            f"(PARTITION BY symbol ORDER BY snapshot_date DESC) = 1").fetchdf()
    finally:
        con.close()
    if max_age_days is not None and not frame.empty:
        cutoff = dt.datetime.now(ET).date() - dt.timedelta(days=max_age_days)
        frame = frame[pd.to_datetime(frame["snapshot_date"]).dt.date >= cutoff]
    if symbols is not None:
        frame = frame[frame["symbol"].isin(symbols)]
    return frame.reset_index(drop=True)


def for_symbol(symbol: str) -> dict | None:
    frame = latest([symbol])
    return None if frame.empty else frame.iloc[0].to_dict()


def history(symbol: str) -> pd.DataFrame:
    con = _connect(read_only=True)
    try:
        return con.execute(f"SELECT * FROM {TABLE} WHERE symbol = ? ORDER BY snapshot_date",
                           [symbol]).fetchdf()
    except Exception:
        return pd.DataFrame()
    finally:
        con.close()
