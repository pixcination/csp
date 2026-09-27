"""
Daily bars, earnings dates and dividends from yfinance.

THREE JOBS
----------
**Total-return daily history.** Finding F-10: `universe_daily.duckdb` is built
from Massive 1-minute bars pulled with `adjusted=True`, which is *split*
adjusted only. yfinance `history()` is split *and dividend* adjusted. The same
ticker produces different long-run returns depending on which you read, and
the empirical move engine is about to depend on one of them.

The rule this module establishes: **total-return bars for probability work,
raw prices for strike selection.** You receive dividends while holding
assigned shares, so a wheel backtest that ignores them understates returns;
but a strike is a strike, and adjusting it retroactively would be nonsense.
Both are stored, explicitly labelled, in `data/universe_daily.duckdb`.

**Earnings calendar.** The single largest driver of assignment risk on a
5-14 DTE put, and the only one known in advance. Free yfinance data is
roughly 90% reliable -- good enough to gate on, with the caveat surfaced
rather than buried: a date that cannot be confirmed is treated as *present*
rather than absent, so an unknown fails safe.

**Dividend ex-dates.** Early assignment on short calls clusters around
ex-dividend when remaining extrinsic is below the dividend. Needed by the
covered-call side in Phase 4.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import duckdb
import pandas as pd

from core.market_calendar import ET, previous_trading_day
from core.paths import db_universe_daily, reference_dir
from core.progress import BaseReporter, NullReporter

EARNINGS_FILE = "earnings.parquet"
DIVIDENDS_FILE = "dividends.parquet"

DAILY_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_bars_tr (
    ticker VARCHAR, date DATE,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE,
    dividends DOUBLE, splits DOUBLE,
    PRIMARY KEY (ticker, date)
)
"""


def _yf():
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError(
            "yfinance is not installed. Run: pip install -r requirements.txt"
        ) from exc
    return yf


# --- Daily bars ------------------------------------------------------------

@dataclass
class DailySyncResult:
    ticker: str
    rows_added: int = 0
    first_date: str | None = None
    last_date: str | None = None
    up_to_date: bool = False
    error: str | None = None


def _last_stored(con, ticker: str) -> dt.date | None:
    row = con.execute("SELECT max(date) FROM daily_bars_tr WHERE ticker = ?",
                       [ticker]).fetchone()
    return row[0] if row and row[0] else None


def sync_daily(tickers: list[str], reporter: BaseReporter | None = None,
                force_full: bool = False) -> list[DailySyncResult]:
    """Incremental total-return daily bars for the universe.

    yfinance has no per-minute cap worth pacing around at this scale, so this
    stage runs inline in the pipeline -- unlike the Massive archive sync.
    """
    yf = _yf()
    reporter = reporter or NullReporter()
    results: list[DailySyncResult] = []
    watermark = previous_trading_day(dt.datetime.now(ET).date() + dt.timedelta(days=1))

    con = duckdb.connect(str(db_universe_daily()))
    con.execute(DAILY_SCHEMA)
    try:
        with reporter.stage("daily", "Daily bars (total return)", total=len(tickers)):
            for ticker in tickers:
                res = DailySyncResult(ticker=ticker)
                try:
                    last = None if force_full else _last_stored(con, ticker)
                    if last and last >= watermark:
                        res.up_to_date = True
                        reporter.advance(1, note=f"{ticker} current")
                        results.append(res)
                        continue

                    if last:
                        start = (last - dt.timedelta(days=5)).isoformat()
                        frame = yf.Ticker(ticker).history(start=start, actions=True,
                                                           auto_adjust=True)
                    else:
                        frame = yf.Ticker(ticker).history(period="max", actions=True,
                                                           auto_adjust=True)

                    if frame is None or frame.empty:
                        res.error = "no data returned"
                        reporter.advance(1, note=f"{ticker} empty")
                        results.append(res)
                        continue

                    frame = frame.reset_index()
                    frame.columns = [str(c).strip().lower().replace(" ", "_")
                                      for c in frame.columns]
                    frame["date"] = pd.to_datetime(frame["date"]).dt.tz_localize(None).dt.date
                    frame["ticker"] = ticker
                    for column in ("dividends", "stock_splits"):
                        if column not in frame.columns:
                            frame[column] = 0.0
                    frame = frame.rename(columns={"stock_splits": "splits"})
                    keep = ["ticker", "date", "open", "high", "low", "close",
                            "volume", "dividends", "splits"]
                    frame = frame[[c for c in keep if c in frame.columns]].dropna(
                        subset=["close"])

                    con.register("incoming", frame)
                    before = con.execute(
                        "SELECT count(*) FROM daily_bars_tr WHERE ticker = ?",
                        [ticker]).fetchone()[0]
                    # Upsert: the 5-day overlap re-states recent bars in case a
                    # split or dividend retroactively adjusted them.
                    con.execute(
                        "DELETE FROM daily_bars_tr WHERE ticker = ? AND date IN "
                        "(SELECT date FROM incoming)", [ticker])
                    con.execute("INSERT INTO daily_bars_tr SELECT * FROM incoming")
                    con.unregister("incoming")
                    after = con.execute(
                        "SELECT count(*) FROM daily_bars_tr WHERE ticker = ?",
                        [ticker]).fetchone()[0]

                    res.rows_added = max(after - before, 0)
                    res.first_date = str(frame["date"].min())
                    res.last_date = str(frame["date"].max())
                    reporter.advance(1, note=f"{ticker} +{res.rows_added}")
                except Exception as exc:
                    res.error = f"{type(exc).__name__}: {str(exc)[:120]}"
                    reporter.advance(1, note=f"{ticker} error")
                results.append(res)
    finally:
        con.close()
    return results


def daily_data_status() -> dict:
    """What daily price data actually exists, and which table it is in.

    Added after a validation run reported "no history" for all 61 tickers and
    gave no clue why. The cause was that `daily_bars_tr` had never been built --
    but every caller swallowed the missing-table error and returned an empty
    frame, so the diagnosis looked like 61 separate data problems instead of
    one missing pipeline stage. Silent degradation is worse than a crash: it
    turns a two-minute fix into an afternoon.
    """
    import duckdb
    path = db_universe_daily()
    status = {
        "database": str(path), "exists": path.exists(),
        "total_return_rows": 0, "total_return_tickers": 0,
        "legacy_rows": 0, "legacy_tickers": 0,
        "source": "none", "adjusted": None, "action": None,
    }
    if not path.exists():
        status["action"] = ("No daily database at all. Run:  python pipeline/run.py")
        return status

    con = duckdb.connect(str(path), read_only=True)
    try:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        if "daily_bars_tr" in tables:
            row = con.execute("SELECT count(*), count(DISTINCT ticker) "
                               "FROM daily_bars_tr").fetchone()
            status["total_return_rows"], status["total_return_tickers"] = int(row[0]), int(row[1])
        if "daily_bars" in tables:
            row = con.execute("SELECT count(*), count(DISTINCT ticker) "
                               "FROM daily_bars").fetchone()
            status["legacy_rows"], status["legacy_tickers"] = int(row[0]), int(row[1])
    except Exception as exc:
        status["action"] = f"Could not read the database: {exc}"
        return status
    finally:
        con.close()

    # Coverage against the universe matters more than the row count. A table
    # holding two tickers reports 25,124 rows and looks healthy, while 59 names
    # silently fall through to the legacy split-adjusted table -- producing a
    # run where some tickers are scored on total return and others are not.
    try:
        from core.paths import load_universe
        universe = set(load_universe())
    except Exception:
        universe = set()

    if universe and status["total_return_tickers"]:
        con = duckdb.connect(str(path), read_only=True)
        try:
            covered = {r[0] for r in con.execute(
                "SELECT DISTINCT ticker FROM daily_bars_tr").fetchall()}
        finally:
            con.close()
        status["universe_covered"] = len(covered & universe)
        status["universe_size"] = len(universe)
        status["coverage"] = len(covered & universe) / len(universe)
        status["mixed_basis"] = 0 < status["coverage"] < 1.0
    else:
        status["universe_covered"] = status["total_return_tickers"]
        status["universe_size"] = len(universe)
        status["coverage"] = 0.0
        status["mixed_basis"] = False

    if status["total_return_rows"]:
        status["source"] = "daily_bars_tr"
        status["adjusted"] = "split and dividend"
        if status["mixed_basis"]:
            status["action"] = (
                f"MIXED PRICE BASIS: only {status['universe_covered']} of "
                f"{status['universe_size']} universe tickers are in the "
                f"total-return table. The rest fall back to the legacy "
                f"split-adjusted table, so results are not comparable across "
                f"tickers. Run  python pipeline/run.py  to complete the sync.")
    elif status["legacy_rows"]:
        status["source"] = "daily_bars"
        status["adjusted"] = "split only"
        status["action"] = (
            "Falling back to the legacy split-adjusted table built by scripts/01. "
            "Usable, but probability and backtest work should run on total-return "
            "bars -- run  python pipeline/run.py  to build them.")
    else:
        status["action"] = ("Daily database exists but holds no bars. Run:  "
                             "python pipeline/run.py")
    return status


def load_daily_total_return(ticker: str, start=None, end=None,
                             allow_fallback: bool = True) -> pd.DataFrame:
    """Dividend-adjusted daily bars -- the basis for all probability work.

    Falls back to the legacy split-adjusted `daily_bars` table when the
    total-return table has not been built, because 25 MB of perfectly usable
    daily history should not sit unread while every downstream module reports
    "no history". The fallback is flagged on the returned frame via
    `frame.attrs["price_basis"]` so callers can say which basis they used.
    """
    import duckdb
    path = db_universe_daily()
    if not path.exists():
        return _empty_daily("missing")

    con = duckdb.connect(str(path), read_only=True)
    try:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        for table, basis in (("daily_bars_tr", "total_return"),
                              ("daily_bars", "split_adjusted")):
            if table not in tables:
                continue
            if table == "daily_bars" and not allow_fallback:
                continue
            query = (f"SELECT date, open, high, low, close, volume FROM {table} "
                     f"WHERE ticker = ?")
            params: list = [ticker]
            if start:
                query += " AND date >= ?"
                params.append(start)
            if end:
                query += " AND date <= ?"
                params.append(end)
            frame = con.execute(query + " ORDER BY date", params).fetchdf()
            if not frame.empty:
                frame["date"] = pd.to_datetime(frame["date"])
                frame.attrs["price_basis"] = basis
                return frame
    except Exception:
        pass
    finally:
        con.close()
    return _empty_daily("empty")


def _empty_daily(reason: str) -> pd.DataFrame:
    frame = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    frame.attrs["price_basis"] = reason
    return frame


# --- Earnings --------------------------------------------------------------

@dataclass(frozen=True)
class EarningsGuard:
    ticker: str
    next_date: dt.date | None
    confirmed: bool
    blocks_expiry: bool
    note: str


def sync_earnings(tickers: list[str],
                   reporter: BaseReporter | None = None) -> pd.DataFrame:
    """Refresh the earnings calendar for the universe.

    Stored rather than queried live because yfinance is slow per-ticker and
    the pipeline needs the whole universe at once.
    """
    yf = _yf()
    reporter = reporter or NullReporter()
    rows = []
    with reporter.stage("earnings", "Earnings calendar", total=len(tickers)):
        for ticker in tickers:
            try:
                frame = yf.Ticker(ticker).get_earnings_dates(limit=8)
                if frame is not None and not frame.empty:
                    for stamp, row in frame.iterrows():
                        rows.append({
                            "ticker": ticker,
                            "earnings_date": pd.Timestamp(stamp).tz_localize(None).date()
                            if pd.Timestamp(stamp).tzinfo else pd.Timestamp(stamp).date(),
                            "eps_estimate": row.get("EPS Estimate"),
                            "reported_eps": row.get("Reported EPS"),
                            "fetched_at": dt.datetime.now(),
                        })
                    reporter.advance(1, note=f"{ticker} {len(frame)} dates")
                else:
                    reporter.advance(1, note=f"{ticker} none")
            except Exception as exc:
                reporter.advance(1, note=f"{ticker} error")
                reporter.log(f"{ticker} earnings lookup failed: {str(exc)[:70]}")
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.drop_duplicates(subset=["ticker", "earnings_date"])
        frame.to_parquet(reference_dir() / EARNINGS_FILE, index=False)
    return frame


def load_earnings() -> pd.DataFrame:
    path = reference_dir() / EARNINGS_FILE
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_parquet(path)
    except Exception:
        return pd.DataFrame()


def earnings_calendar_health(universe: list[str] | None = None) -> dict:
    """Is the earnings calendar trustworthy enough to gate on?

    THE CASCADE THIS PREVENTS. `earnings_guard` fails safe: an unknown date
    blocks the trade, because a missing entry is far more often a stale
    calendar than a company that stopped reporting. That is right per ticker
    and catastrophic in aggregate -- if the calendar is empty, every candidate
    in the universe is blocked and the run produces nothing, with each
    individual rejection looking perfectly reasonable.

    So coverage is checked once. Below the threshold the calendar is declared
    unhealthy, the gate degrades to a loud warning, and the run still produces
    recommendations that say plainly which ones could not be earnings-checked.
    A tool that silently returns zero is worse than one that returns something
    caveated.
    """
    from core.paths import load_universe

    universe = universe or load_universe()
    frame = load_earnings()
    if frame.empty:
        return {"healthy": False, "covered": 0, "universe": len(universe),
                "coverage": 0.0, "forward_covered": 0,
                "note": "earnings.parquet is missing or empty"}

    today = dt.datetime.now(ET).date()
    covered = set(frame["ticker"].unique())
    forward = set(frame.loc[frame["earnings_date"] >= today, "ticker"].unique())
    coverage = len(forward & set(universe)) / max(len(universe), 1)

    return {
        "healthy": coverage >= 0.5,
        "covered": len(covered & set(universe)),
        "forward_covered": len(forward & set(universe)),
        "universe": len(universe),
        "coverage": coverage,
        "note": ("" if coverage >= 0.5 else
                 f"only {coverage:.0%} of the universe has a forward-looking "
                 f"earnings date. The gate is degraded to a warning -- blocking on "
                 f"an unknown date is correct for one ticker and useless when it "
                 f"blocks all of them. Re-run the earnings sync to restore it."),
    }


def earnings_guard(ticker: str, expiration: dt.date,
                    today: dt.date | None = None,
                    calendar_healthy: bool = True) -> EarningsGuard:
    """Is there an unreported earnings date between now and expiration?

    **Unknown fails safe.** If the calendar has no forward date for a ticker,
    this reports `blocks_expiry=True` with `confirmed=False` rather than
    waving the trade through. A missing date is much more often a stale
    calendar than a company that has stopped reporting, and the cost of
    skipping a good trade is far below the cost of selling a put through an
    earnings print by accident.
    """
    today = today or dt.datetime.now(ET).date()
    # When the calendar as a whole is unhealthy, an unknown date is evidence
    # about the calendar, not about the ticker -- so it warns instead of blocks.
    unknown_blocks = calendar_healthy

    frame = load_earnings()
    if frame.empty:
        return EarningsGuard(ticker, None, False, unknown_blocks,
                             "no earnings calendar on disk -- run the reference "
                             "refresh" + (" ; treating as blocked until confirmed"
                                          if unknown_blocks else
                                          " ; NOT earnings-checked"))

    rows = frame[frame["ticker"] == ticker]
    if rows.empty:
        return EarningsGuard(ticker, None, False, unknown_blocks,
                             "no earnings dates for this ticker"
                             + (" -- treating as blocked until confirmed"
                                if unknown_blocks else " -- NOT earnings-checked"))

    upcoming = sorted(d for d in rows["earnings_date"] if d >= today)
    if not upcoming:
        stale = max(rows["earnings_date"])
        return EarningsGuard(ticker, None, False, unknown_blocks,
                             f"calendar ends {stale} with nothing forward-looking"
                             + (" -- refresh it; treating as blocked"
                                if unknown_blocks else " -- NOT earnings-checked"))

    next_date = upcoming[0]
    blocks = next_date <= expiration
    if blocks:
        note = (f"reports {next_date}, before the {expiration} expiration -- "
                f"the largest single source of assignment risk on a weekly put, "
                f"and elevated pre-print IV is exactly what makes the yield look good")
    else:
        note = f"next report {next_date}, safely after the {expiration} expiration"
    return EarningsGuard(ticker, next_date, True, blocks, note)


# --- Dividends -------------------------------------------------------------

def sync_dividends(tickers: list[str],
                    reporter: BaseReporter | None = None) -> pd.DataFrame:
    """Ex-dividend dates and amounts, for the early-assignment warning."""
    yf = _yf()
    reporter = reporter or NullReporter()
    rows = []
    with reporter.stage("dividends", "Dividend ex-dates", total=len(tickers)):
        for ticker in tickers:
            try:
                series = yf.Ticker(ticker).dividends
                if series is not None and len(series):
                    recent = series.tail(12)
                    for stamp, amount in recent.items():
                        stamp = pd.Timestamp(stamp)
                        rows.append({
                            "symbol": ticker,
                            "ex_date": (stamp.tz_localize(None).date()
                                        if stamp.tzinfo else stamp.date()),
                            "amount": float(amount),
                        })
                reporter.advance(1, note=ticker)
            except Exception:
                reporter.advance(1, note=f"{ticker} error")
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.drop_duplicates(subset=["symbol", "ex_date"])
        frame.to_parquet(reference_dir() / DIVIDENDS_FILE, index=False)
    return frame


def next_ex_dividend(ticker: str, today: dt.date | None = None) -> tuple[dt.date, float] | None:
    """Estimate the next ex-date by extrapolating the observed cadence.

    yfinance reports historical ex-dates, not announced future ones, so this
    is a projection. Good enough to raise a flag near a likely ex-date; not
    good enough to trade a dividend capture on.
    """
    today = today or dt.datetime.now(ET).date()
    path = reference_dir() / DIVIDENDS_FILE
    if not path.exists():
        return None
    try:
        frame = pd.read_parquet(path)
    except Exception:
        return None
    rows = frame[frame["symbol"] == ticker].sort_values("ex_date")
    if len(rows) < 3:
        return None
    dates = list(rows["ex_date"])
    gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    typical = sorted(gaps)[len(gaps) // 2]
    projected = dates[-1]
    for _ in range(8):
        projected = projected + dt.timedelta(days=typical)
        if projected >= today:
            return projected, float(rows["amount"].iloc[-1])
    return None
