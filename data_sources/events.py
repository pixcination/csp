"""
Events -- one table of everything dated that can hurt a short-premium trade,
and one function that says what a trade's window runs into (Phase 9).

    events(symbol, date, type, time_of_day, confirmed, amount, source,
           sources_disagree, note)          symbol "*" = market-wide

Types and where they come from:

    earnings       yfinance calendar (with bmo/amc time) merged with the
                   TastyTrade expected report date; `sources_disagree` when
                   they differ by more than `events.earnings_disagree_days`,
                   in which case the EARLIER date is kept (fail safe)
    ex_dividend    history from daily_bars_raw; the next one projected from
                   the cadence (confirmed = False)
    split          history from daily_bars_raw
    fomc/cpi/nfp   config/macro_calendar.yaml (hand-maintained, sourced)
    opex           third Friday of each month (prior session if a holiday)
    quad_witching  the March/June/September/December third Fridays

The table is derived: `build()` rewrites it from its sources every run.

`check(symbol, start, end, strategy)` applies `config.yaml -> event_policy`
and replaces the direct earnings gate `candidates.py` used through Phase 8.
The fail-safe for an unknown earnings date is kept -- but only for stocks.
Before Phase 9 it applied to every ticker, and since ETFs never report
earnings it rejected SPY, DIA, EFA, KRE, IBIT, EWZ and KWEB on every run.
"""
from __future__ import annotations

import datetime as dt
import functools
from dataclasses import dataclass, field

import duckdb
import pandas as pd

from core.market_calendar import ET, is_trading_day
from core.paths import config_dir, db_universe, db_universe_daily, load_config
from core.progress import BaseReporter, NullReporter

TABLE = "events"
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    symbol VARCHAR, date DATE, type VARCHAR, time_of_day VARCHAR,
    confirmed BOOLEAN, amount DOUBLE, source VARCHAR,
    sources_disagree BOOLEAN, note VARCHAR, built_at TIMESTAMP
)
"""
COLUMNS = ["symbol", "date", "type", "time_of_day", "confirmed", "amount", "source",
           "sources_disagree", "note", "built_at"]
MARKET = "*"
MACRO_FILE = "macro_calendar.yaml"
ACTION_ORDER = {"ignore": 0, "warn": 1, "block": 2}


# --- Building --------------------------------------------------------------

def _row(symbol, date, type_, time_of_day="unknown", confirmed=True, amount=None,
         source="", disagree=False, note="") -> dict:
    return {"symbol": symbol, "date": date, "type": type_, "time_of_day": time_of_day,
            "confirmed": confirmed, "amount": amount, "source": source,
            "sources_disagree": disagree, "note": note}


def third_friday(year: int, month: int) -> dt.date:
    first = dt.date(year, month, 1)
    friday = first + dt.timedelta(days=(4 - first.weekday()) % 7)
    return friday + dt.timedelta(days=14)


def expiration_events(start: dt.date, end: dt.date) -> list[dict]:
    """Monthly OPEX and quad witching; a holiday Friday moves to the prior session."""
    rows = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        day = third_friday(year, month)
        probe = day
        for _ in range(4):
            if is_trading_day(probe):
                break
            probe -= dt.timedelta(days=1)
        quad = month in (3, 6, 9, 12)
        note = "quarterly stock/index futures and options expiry" if quad else "monthly options expiry"
        if probe != day:
            note += f" (moved from {day}: holiday)"
        rows.append(_row(MARKET, probe, "quad_witching" if quad else "opex", "close",
                         source="computed", note=note))
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return rows


def macro_events(path=None) -> list[dict]:
    import yaml
    path = path or config_dir() / MACRO_FILE
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = []
    for kind, block in data.items():
        if not isinstance(block, dict):
            continue
        default_time = block.get("time", "unknown")
        for entry in block.get("dates", []) or []:
            note = entry.get("note", "")
            if entry.get("sep"):
                note += " (with economic projections)"
            rows.append(_row(MARKET, pd.Timestamp(entry["date"]).date(), kind,
                             entry.get("time", default_time), source=MACRO_FILE,
                             note=note.strip()))
    return rows


def corporate_action_events(symbols: list[str]) -> list[dict]:
    """Dividend and split history from daily_bars_raw, plus the projected next
    ex-dividend date."""
    from data_sources.yfinance_sync import RAW_TABLE, next_ex_dividend
    rows = []
    path = db_universe_daily()
    if path.exists():
        con = duckdb.connect(str(path), read_only=True)
        try:
            frame = con.execute(
                f"SELECT ticker, date, dividends, splits FROM {RAW_TABLE} "
                f"WHERE dividends > 0 OR splits > 0").fetchdf()
            frame = frame[frame["ticker"].isin(symbols)]
        except Exception:
            frame = pd.DataFrame()
        finally:
            con.close()
        for r in frame.itertuples():
            date = pd.Timestamp(r.date).date()
            if r.dividends and r.dividends > 0:
                rows.append(_row(r.ticker, date, "ex_dividend", "open", amount=float(r.dividends),
                                 source="yfinance", note=f"${r.dividends:.4g}/share"))
            if r.splits and r.splits > 0:
                rows.append(_row(r.ticker, date, "split", "open", amount=float(r.splits),
                                 source="yfinance", note=f"{r.splits:g}-for-1"))
    for symbol in symbols:
        projected = next_ex_dividend(symbol)
        if projected:
            date, amount = projected
            rows.append(_row(symbol, date, "ex_dividend", "open", confirmed=False,
                             amount=amount, source="projection",
                             note=f"projected from the payment cadence (~${amount:.4g})"))
    return rows


def earnings_events(stocks: list[str], today: dt.date | None = None) -> list[dict]:
    """History from yfinance; the next date merged across yfinance and tasty."""
    from data_sources import tasty_metrics
    from data_sources.yfinance_sync import load_earnings

    today = today or dt.datetime.now(ET).date()
    tolerance = load_config().get("events", {}).get("earnings_disagree_days", 1)
    yf_frame = load_earnings()
    if not yf_frame.empty:
        yf_frame = yf_frame.copy()
        yf_frame["earnings_date"] = pd.to_datetime(yf_frame["earnings_date"]).dt.date
        if "time_of_day" not in yf_frame.columns:
            yf_frame["time_of_day"] = "unknown"
    tasty = tasty_metrics.latest(stocks)
    tasty = tasty.set_index("symbol") if not tasty.empty else pd.DataFrame()

    rows = []
    for symbol in stocks:
        own = (yf_frame[yf_frame["ticker"] == symbol].sort_values("earnings_date")
               if not yf_frame.empty else pd.DataFrame())
        for r in own.itertuples() if not own.empty else []:
            if r.earnings_date < today:
                rows.append(_row(symbol, r.earnings_date, "earnings", r.time_of_day,
                                 source="yfinance", note="reported"))
        yf_next = own[own["earnings_date"] >= today].head(1) if not own.empty else own
        yf_date = yf_next["earnings_date"].iloc[0] if len(yf_next) else None
        yf_time = yf_next["time_of_day"].iloc[0] if len(yf_next) else "unknown"

        tt_date, tt_estimated = None, None
        if not tasty.empty and symbol in tasty.index:
            value = tasty.loc[symbol, "earnings_date"]
            if pd.notna(value) and pd.Timestamp(value).date() >= today:
                tt_date = pd.Timestamp(value).date()
                tt_estimated = bool(tasty.loc[symbol, "earnings_estimated"]) \
                    if pd.notna(tasty.loc[symbol, "earnings_estimated"]) else None

        if yf_date is None and tt_date is None:
            continue
        disagree = (yf_date is not None and tt_date is not None
                    and abs((yf_date - tt_date).days) > tolerance)
        date = min(d for d in (yf_date, tt_date) if d is not None)
        sources = "+".join(n for n, d in (("yfinance", yf_date), ("tasty", tt_date)) if d)
        confirmed = tt_estimated is False if tt_date else False
        note = "next report"
        if disagree:
            note = (f"sources disagree: yfinance {yf_date}, tasty {tt_date} -- "
                    f"using the earlier")
        rows.append(_row(symbol, date, "earnings", yf_time if date == yf_date else "unknown",
                         confirmed=confirmed, source=sources, disagree=disagree, note=note))
    return rows


def build(symbols: list[str] | None = None, reporter: BaseReporter | None = None) -> dict:
    """Rewrite the events table from every source. Returns counts by type."""
    from data_sources import universe

    reporter = reporter or NullReporter()
    registry = universe.load(active_only=True)
    if symbols is not None:
        registry = registry[registry["symbol"].isin(symbols)]
    stocks = registry.loc[registry["asset_class"] == "stock", "symbol"].tolist()
    physical = registry.loc[registry["settlement"] != "cash", "symbol"].tolist()
    cfg = load_config().get("events", {})
    today = dt.datetime.now(ET).date()

    rows: list[dict] = []
    with reporter.stage("events", "Events calendar", total=4):
        rows += earnings_events(stocks, today)
        reporter.advance(1, note="earnings")
        rows += corporate_action_events(physical)
        reporter.advance(1, note="dividends and splits")
        rows += macro_events()
        reporter.advance(1, note="macro calendar")
        rows += expiration_events(
            dt.date(today.year - cfg.get("opex_years_back", 2), 1, 1),
            dt.date(today.year + cfg.get("opex_years_forward", 2), 12, 31))
        reporter.advance(1, note="expirations")

    frame = pd.DataFrame(rows, columns=[c for c in COLUMNS if c != "built_at"])
    frame["built_at"] = dt.datetime.now()
    con = duckdb.connect(str(db_universe()))
    try:
        con.execute(SCHEMA)
        con.execute(f"DELETE FROM {TABLE}")
        if not frame.empty:
            con.register("incoming", frame[COLUMNS])
            con.execute(f"INSERT INTO {TABLE} SELECT * FROM incoming")
            con.unregister("incoming")
    finally:
        con.close()
    load.cache_clear()
    return frame["type"].value_counts().to_dict() if not frame.empty else {}


# --- Reading ---------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def load() -> pd.DataFrame:
    """The whole events table (cached per process; `build()` clears it)."""
    path = db_universe()
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    con = duckdb.connect(str(path), read_only=True)
    try:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        if TABLE not in tables:
            return pd.DataFrame(columns=COLUMNS)
        frame = con.execute(f"SELECT * FROM {TABLE}").fetchdf()
    finally:
        con.close()
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    return frame


def upcoming(symbol: str | None = None, days: int = 45,
             today: dt.date | None = None) -> pd.DataFrame:
    today = today or dt.datetime.now(ET).date()
    frame = load()
    frame = frame[(frame["date"] >= today) & (frame["date"] <= today + dt.timedelta(days=days))]
    if symbol:
        frame = frame[frame["symbol"].isin([symbol, MARKET])]
    return frame.sort_values(["date", "symbol"]).reset_index(drop=True)


def earnings_health(symbols: list[str] | None = None,
                    today: dt.date | None = None) -> dict:
    """Share of active STOCKS with a forward earnings date. Below the
    configured minimum the unknown-date rule degrades from block to warn --
    see yfinance_sync.earnings_calendar_health for why that matters."""
    from data_sources import universe
    today = today or dt.datetime.now(ET).date()
    registry = universe.load(active_only=True)
    stocks = registry.loc[registry["asset_class"] == "stock", "symbol"]
    if symbols is not None:
        stocks = stocks[stocks.isin(symbols)]
    stocks = set(stocks)
    frame = load()
    forward = set(frame.loc[(frame["type"] == "earnings") & (frame["date"] >= today),
                            "symbol"]) & stocks
    coverage = len(forward) / len(stocks) if stocks else 1.0
    minimum = load_config().get("events", {}).get("earnings_health_min_coverage", 0.5)
    return {"healthy": coverage >= minimum, "stocks": len(stocks),
            "forward_covered": len(forward), "coverage": coverage,
            "missing": sorted(stocks - forward),
            "note": "" if coverage >= minimum else
            (f"only {coverage:.0%} of stocks have a forward earnings date; the "
             f"unknown-date rule is degraded to a warning")}


@dataclass
class EventHit:
    symbol: str
    date: dt.date
    type: str
    action: str
    confirmed: bool
    note: str

    def text(self) -> str:
        who = "market" if self.symbol == MARKET else self.symbol
        tag = "" if self.confirmed else " (unconfirmed)"
        return f"{self.type.replace('_', ' ')} {self.date} [{who}]{tag}: {self.note}"


@dataclass
class EventCheck:
    symbol: str
    start: dt.date
    end: dt.date
    strategy: str
    action: str = "ok"                   # ok | warn | block
    hits: list[EventHit] = field(default_factory=list)

    @property
    def blocks(self) -> bool:
        return self.action == "block"

    def texts(self, action: str) -> list[str]:
        return [h.text() for h in self.hits if h.action == action]

    @property
    def earnings_block(self) -> EventHit | None:
        return next((h for h in self.hits if h.type == "earnings" and h.action == "block"), None)


def _policy(strategy: str) -> dict:
    out = {}
    for kind, rule in (load_config().get("event_policy") or {}).items():
        rule = rule or {}
        if strategy in (rule.get("applies_to") or [strategy]):
            out[kind] = rule
    return out


def check(symbol: str, start: dt.date, end: dt.date, strategy: str = "csp",
          asset_class: str | None = None, calendar_healthy: bool = True) -> EventCheck:
    """What does a trade in `symbol` from `start` to `end` run into?

    Each event type's window is [start - days_before, end + days_after]. The
    result's `action` is the most severe action among the hits.
    """
    if asset_class is None:
        try:
            from data_sources import universe
            asset_class = universe.asset_class(symbol) or "stock"
        except Exception:
            asset_class = "stock"
    result = EventCheck(symbol, start, end, strategy)
    policy = _policy(strategy)
    frame = load()
    frame = frame[frame["symbol"].isin([symbol, MARKET])] if not frame.empty else frame

    for kind, rule in policy.items():
        action = rule.get("action", "ignore")
        if action == "ignore":
            continue
        classes = rule.get("asset_classes")
        if classes and asset_class not in classes:
            continue
        lo = start - dt.timedelta(days=int(rule.get("days_before", 0)))
        hi = end + dt.timedelta(days=int(rule.get("days_after", 0)))
        rows = frame[(frame["type"] == kind) & (frame["date"] >= lo) & (frame["date"] <= hi)] \
            if not frame.empty else frame
        for r in rows.itertuples():
            note = r.note or ""
            if kind == "earnings":
                note = (f"reports {r.date} ({r.time_of_day}), inside the trade -- the largest "
                        f"single source of assignment risk, and pre-print IV is what "
                        f"makes the yield look good") + (f"; {r.note}" if r.sources_disagree else "")
            result.hits.append(EventHit(r.symbol, r.date, kind, action,
                                        bool(r.confirmed), note))

        # Fail safe: a stock with no known forward earnings date.
        if kind == "earnings" and asset_class == "stock" and not any(
                h.type == "earnings" for h in result.hits):
            known = frame[(frame["type"] == "earnings") & (frame["symbol"] == symbol)
                          & (frame["date"] >= start)] if not frame.empty else frame
            if known.empty:
                fail_action = action if calendar_healthy else "warn"
                result.hits.append(EventHit(
                    symbol, start, "earnings", fail_action, False,
                    "no forward earnings date from yfinance or TastyTrade -- " +
                    ("treating as blocked until confirmed" if fail_action == "block"
                     else "NOT earnings-checked (calendar degraded)")))

    if result.hits:
        result.action = max((h.action for h in result.hits),
                            key=lambda a: ACTION_ORDER.get(a, 0))
        result.action = "ok" if result.action == "ignore" else result.action
    return result
