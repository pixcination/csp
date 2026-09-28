"""
Daily bars, earnings dates and dividends from yfinance.

THREE JOBS
----------
**Daily history on two explicit price bases.** Finding F-10 established that
the same ticker produces different long-run returns depending on whether its
bars are dividend-adjusted. Phase 8 settled which basis each question needs:

    price  -- split-adjusted only: the price that actually traded.
              Strikes, levels, technicals, breach/touch probabilities, gaps,
              realized vol. Options settle on this price, and it DROPS by the
              dividend on the ex-date. A dividend-adjusted series erases those
              drops, which understated P(breach) for high-yield names (MO, T,
              KO, PBR...) whenever a window spanned an ex-date.
    total  -- split and dividend adjusted: what a holder earned.
              Long-run wheel / buy-and-hold performance comparisons only.

Only raw bars are stored (`daily_bars_raw`: yfinance with
`auto_adjust=False`, whose Close is split-adjusted, plus Yahoo's `adj_close`,
dividends and splits). The total-return basis is DERIVED locally from the
stored dividends by `load_daily(..., basis="total")`, and `adjustment_check`
cross-checks it against Yahoo's `adj_close`.

WHY NOT STORE YAHOO'S ADJUSTED SERIES (the bug this replaced)
------------------------------------------------------------
Phases 2-7 stored `auto_adjust=True` bars and re-pulled only the last ~5 days
on each run. Every new dividend makes Yahoo re-adjust the *entire* history,
but only the tail was re-stated, so the stored series became a mix of
adjustment vintages with an artificial step at each seam -- corrupting
returns, moving averages and every probability computed across the seam.
Now: any new dividend or split in the incremental window, or any disagreement
with stored history on the overlap, triggers a full re-pull of that ticker.

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
import numpy as np
import pandas as pd

from core.market_calendar import ET, previous_trading_day
from core.paths import db_universe_daily, reference_dir
from core.progress import BaseReporter, NullReporter

EARNINGS_FILE = "earnings.parquet"
DIVIDENDS_FILE = "dividends.parquet"

RAW_TABLE = "daily_bars_raw"
RAW_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    ticker VARCHAR, date DATE,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, adj_close DOUBLE,
    volume DOUBLE, dividends DOUBLE, splits DOUBLE,
    PRIMARY KEY (ticker, date)
)
"""
RAW_COLUMNS = ["ticker", "date", "open", "high", "low", "close", "adj_close",
               "volume", "dividends", "splits"]
BAR_COLUMNS = ["date", "open", "high", "low", "close", "volume"]
BASES = ("price", "total")

# Overlap closes that move by more than this mean Yahoo restated history
# (a late-posted split, a data correction) and the ticker must be re-pulled.
RESTATEMENT_TOLERANCE = 1e-3


def _yf():
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError(
            "yfinance is not installed. Run: pip install -r requirements.txt"
        ) from exc
    return yf


# --- Daily bars: sync ------------------------------------------------------

@dataclass
class DailySyncResult:
    ticker: str
    rows_added: int = 0
    first_date: str | None = None
    last_date: str | None = None
    up_to_date: bool = False
    full_repull: str | None = None      # why the whole history was re-pulled
    error: str | None = None


def _last_stored(con, ticker: str) -> dt.date | None:
    row = con.execute(f"SELECT max(date) FROM {RAW_TABLE} WHERE ticker = ?",
                      [ticker]).fetchone()
    return row[0] if row and row[0] else None


def _normalise(frame: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """yfinance `history(auto_adjust=False)` -> RAW_COLUMNS."""
    frame = frame.reset_index()
    frame.columns = [str(c).strip().lower().replace(" ", "_") for c in frame.columns]
    frame["date"] = pd.to_datetime(frame["date"]).dt.tz_localize(None).dt.date
    frame["ticker"] = ticker
    frame = frame.rename(columns={"stock_splits": "splits"})
    for column in ("dividends", "splits"):
        if column not in frame.columns:
            frame[column] = 0.0
    if "adj_close" not in frame.columns:
        frame["adj_close"] = frame["close"]
    frame[["dividends", "splits"]] = frame[["dividends", "splits"]].fillna(0.0)
    return frame[RAW_COLUMNS].dropna(subset=["close"]).reset_index(drop=True)


def _vendor_map(tickers: list[str]) -> dict[str, tuple[str, float]]:
    """{symbol: (yf_symbol, price_scale)} from the registry (Phase 9)."""
    try:
        from data_sources import universe
        return universe.vendor_map(tickers)
    except Exception:
        return {t: (t.replace(".", "-"), 1.0) for t in tickers}


def _scale(frame: pd.DataFrame, factor: float) -> pd.DataFrame:
    """Apply a registry price_scale (XSP = ^SPX x 0.1) to prices and dividends."""
    if factor == 1.0 or frame.empty:
        return frame
    frame = frame.copy()
    for column in ("open", "high", "low", "close", "adj_close", "dividends"):
        frame[column] = frame[column] * factor
    return frame


def _download(yf, yf_symbols: list[str], start: str | None = None,
              retries: int = 3, backoff: float = 2.0) -> dict[str, pd.DataFrame]:
    """Batch `yf.download`, split per symbol. Retries the whole call with
    exponential backoff, then each still-missing symbol once on its own.
    Returns {yf_symbol: yahoo-shaped frame}; a missing key means no data."""
    import time
    kwargs = {"start": start} if start else {"period": "max"}
    out: dict[str, pd.DataFrame] = {}
    pending = list(dict.fromkeys(yf_symbols))

    def split(data, wanted):
        if data is None or data.empty:
            return
        for symbol in wanted:
            try:
                part = data[symbol] if isinstance(data.columns, pd.MultiIndex) else data
            except KeyError:
                continue
            part = part.dropna(how="all")
            if not part.empty and "Close" in part and part["Close"].notna().any():
                out[symbol] = part

    for attempt in range(retries):
        if not pending:
            break
        try:
            split(yf.download(pending, auto_adjust=False, actions=True, group_by="ticker",
                              threads=True, progress=False, **kwargs), pending)
        except Exception:
            pass
        pending = [s for s in pending if s not in out]
        if pending and attempt < retries - 1:
            time.sleep(backoff * (2 ** attempt))
    # A batch can drop a symbol that loads fine alone (Yahoo rate limiting).
    for symbol in pending:
        try:
            split(yf.download([symbol], auto_adjust=False, actions=True, group_by="ticker",
                              progress=False, **kwargs), [symbol])
        except Exception:
            pass
    return out


def repull_reason(stored: pd.DataFrame, incoming: pd.DataFrame) -> str | None:
    """Does this incremental pull invalidate the stored history?

    `stored` and `incoming` are RAW_COLUMNS frames for one ticker. Returns a
    reason string when the ticker must be re-pulled in full, else None.

    * A new dividend changes Yahoo's `adj_close` for every earlier row.
    * A new split changes the split-adjusted Close for every earlier row.
    * An overlap row whose close or corporate actions differ from what is
      stored means Yahoo restated something -- trust neither side, re-pull.
    """
    if incoming.empty:
        return None
    last = stored["date"].max() if not stored.empty else None
    new = incoming[incoming["date"] > last] if last is not None else incoming
    if (new["splits"].fillna(0) > 0).any():
        return "new split"
    if (new["dividends"].fillna(0) > 0).any():
        return "new dividend"
    if stored.empty:
        return None
    overlap = incoming.merge(stored, on="date", suffixes=("", "_stored"))
    if overlap.empty:
        return None
    moved = (overlap["close"] / overlap["close_stored"] - 1.0).abs()
    if (moved > RESTATEMENT_TOLERANCE).any():
        return "history restated"
    for column in ("dividends", "splits"):
        if (overlap[column].fillna(0) - overlap[f"{column}_stored"].fillna(0)).abs().max() > 1e-9:
            return f"late-posted {column[:-1]}"
    return None


def _replace(con, ticker: str, frame: pd.DataFrame, whole_ticker: bool) -> None:
    con.register("incoming", frame)
    try:
        if whole_ticker:
            con.execute(f"DELETE FROM {RAW_TABLE} WHERE ticker = ?", [ticker])
        else:
            con.execute(f"DELETE FROM {RAW_TABLE} WHERE ticker = ? AND date IN "
                        f"(SELECT date FROM incoming)", [ticker])
        con.execute(f"INSERT INTO {RAW_TABLE} SELECT {', '.join(RAW_COLUMNS)} FROM incoming")
    finally:
        con.unregister("incoming")


def sync_daily(tickers: list[str], reporter: BaseReporter | None = None,
               force_full: bool = False, batch_size: int = 40) -> list[DailySyncResult]:
    """Incremental raw daily bars for `tickers` (canonical symbols).

    Phase 9: batched `yf.download` (threads, retry with backoff) using the
    registry's vendor mapping, so indices (^SPX...) and class shares (BRK-B)
    load, and one Yahoo series can serve two symbols (SPX and XSP).

    Per ticker: current -> skipped; otherwise pulled from the last stored
    date minus 7 days, compared on the overlap (`repull_reason`), and re-pulled
    in full when anything invalidates the stored history.
    """
    yf = _yf()
    reporter = reporter or NullReporter()
    results = {t: DailySyncResult(ticker=t) for t in tickers}
    mapping = _vendor_map(tickers)
    watermark = previous_trading_day(dt.datetime.now(ET).date() + dt.timedelta(days=1))

    con = duckdb.connect(str(db_universe_daily()))
    con.execute(RAW_SCHEMA)
    try:
        with reporter.stage("daily", "Daily bars", total=len(tickers)):
            last = {} if force_full else {t: _last_stored(con, t) for t in tickers}
            full, incremental = [], []
            for ticker in tickers:
                stored = last.get(ticker)
                if stored and stored >= watermark:
                    results[ticker].up_to_date = True
                    reporter.advance(1, note=f"{ticker} current")
                elif stored:
                    incremental.append(ticker)
                else:
                    results[ticker].full_repull = "forced" if force_full else "initial"
                    full.append(ticker)

            # Incremental: one download from the earliest start, sliced per ticker.
            for chunk in _chunks(incremental, batch_size):
                start = (min(last[t] for t in chunk) - dt.timedelta(days=7)).isoformat()
                pulled = _download(yf, [mapping[t][0] for t in chunk], start)
                for ticker in chunk:
                    yf_symbol, scale = mapping[ticker]
                    if yf_symbol not in pulled:
                        results[ticker].error = "no data returned"
                        reporter.advance(1, note=f"{ticker} empty")
                        continue
                    own_start = last[ticker] - dt.timedelta(days=7)
                    frame = _scale(_normalise(pulled[yf_symbol], ticker), scale)
                    frame = frame[frame["date"] >= own_start]
                    stored = con.execute(
                        f"SELECT date, close, dividends, splits FROM {RAW_TABLE} "
                        f"WHERE ticker = ? AND date >= ?", [ticker, own_start]).fetchdf()
                    stored["date"] = pd.to_datetime(stored["date"]).dt.date
                    reason = repull_reason(stored, frame)
                    if reason:
                        results[ticker].full_repull = reason
                        full.append(ticker)
                        continue
                    _store(con, ticker, frame, results[ticker], whole_ticker=False)
                    reporter.advance(1, note=f"{ticker} +{results[ticker].rows_added}")

            for chunk in _chunks(full, batch_size):
                pulled = _download(yf, [mapping[t][0] for t in chunk])
                for ticker in chunk:
                    yf_symbol, scale = mapping[ticker]
                    res = results[ticker]
                    if yf_symbol not in pulled:
                        res.error = "no data returned"
                        reporter.advance(1, note=f"{ticker} empty")
                        continue
                    frame = _scale(_normalise(pulled[yf_symbol], ticker), scale)
                    _store(con, ticker, frame, res, whole_ticker=True)
                    note = f"{ticker} +{res.rows_added}"
                    if res.full_repull not in ("initial", "forced"):
                        note += f" (full re-pull: {res.full_repull})"
                    reporter.advance(1, note=note)
    finally:
        con.close()
    return [results[t] for t in tickers]


def _chunks(items: list, size: int):
    for i in range(0, len(items), max(size, 1)):
        yield items[i:i + size]


def _store(con, ticker: str, frame: pd.DataFrame, res: DailySyncResult,
           whole_ticker: bool) -> None:
    try:
        before = con.execute(f"SELECT count(*) FROM {RAW_TABLE} WHERE ticker = ?",
                             [ticker]).fetchone()[0]
        _replace(con, ticker, frame, whole_ticker=whole_ticker)
        after = con.execute(f"SELECT count(*) FROM {RAW_TABLE} WHERE ticker = ?",
                            [ticker]).fetchone()[0]
        res.rows_added = max(after - before, 0)
        res.first_date = str(frame["date"].min())
        res.last_date = str(frame["date"].max())
    except Exception as exc:
        res.error = f"{type(exc).__name__}: {str(exc)[:120]}"


# --- Daily bars: read ------------------------------------------------------

def total_return_factor(frame: pd.DataFrame) -> pd.Series:
    """Backward dividend-adjustment factor for a date-sorted raw frame.

    For an ex-date d paying D, every bar before d is scaled by
    (1 - D / close[d-1]) -- the CRSP / Yahoo convention. The factor for a row
    is the product over all ex-dates AFTER it, so the latest row is 1.0 and
    the series is anchored at the last returned date.
    """
    close = frame["close"].astype(float).reset_index(drop=True)
    dividends = frame["dividends"].fillna(0.0).astype(float).reset_index(drop=True)
    prev_close = close.shift(1)
    step = pd.Series(1.0, index=close.index)
    mask = (dividends > 0) & (prev_close > 0)
    step[mask] = 1.0 - dividends[mask] / prev_close[mask]
    factor = step[::-1].cumprod()[::-1].shift(-1, fill_value=1.0)
    factor.index = frame.index
    return factor


def load_daily(ticker: str, start=None, end=None, basis: str = "price",
               allow_fallback: bool = True, with_dividends: bool = False) -> pd.DataFrame:
    """Daily bars on an explicit price basis -- see the module docstring.

    `basis="price"` (default): split-adjusted traded prices. Use for anything
    a strike, level or probability is computed from.
    `basis="total"`: dividend-adjusted, derived locally from stored dividends.
    Use only for long-run holder-return comparisons.
    `with_dividends`: keep the per-share cash `dividends` column (0 on most
    days, the amount on each ex-date) -- the price basis plus explicit
    dividend credits is the right model for anything that holds shares
    (Phase 15: the wheel backtest). The legacy fallbacks carry none.

    Falls back to the legacy tables when `daily_bars_raw` has no rows for the
    ticker, because 25 MB of usable history should not sit unread while every
    downstream module reports "no history". The basis actually delivered is
    always on `frame.attrs["price_basis"]`: "price", "total", or
    "split_adjusted_legacy" / "total_return_legacy" for a fallback.
    """
    if basis not in BASES:
        raise ValueError(f"basis must be one of {BASES}, not {basis!r}")
    path = db_universe_daily()
    if not path.exists():
        return _empty_daily("missing")

    con = duckdb.connect(str(path), read_only=True)
    try:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        where, params = " WHERE ticker = ?", [ticker]
        if start:
            where += " AND date >= ?"
            params.append(pd.Timestamp(start).date())
        if end:
            where += " AND date <= ?"
            params.append(pd.Timestamp(end).date())

        if RAW_TABLE in tables:
            frame = con.execute(
                f"SELECT date, open, high, low, close, volume, dividends FROM {RAW_TABLE}"
                f"{where} ORDER BY date", params).fetchdf()
            if not frame.empty:
                frame["date"] = pd.to_datetime(frame["date"])
                if basis == "total":
                    factor = total_return_factor(frame)
                    for column in ("open", "high", "low", "close"):
                        frame[column] = frame[column] * factor
                frame = frame[BAR_COLUMNS + (["dividends"] if with_dividends else [])]
                frame.attrs["price_basis"] = basis
                return frame

        if not allow_fallback:
            return _empty_daily("empty")
        # daily_bars (built from the split-adjusted 1-minute archive) is a
        # price basis; daily_bars_tr (pre-Phase-8) is a mixed-vintage total one.
        order = (("daily_bars", "split_adjusted_legacy"),
                 ("daily_bars_tr", "total_return_legacy"))
        if basis == "total":
            order = order[::-1]
        for table, label in order:
            if table not in tables:
                continue
            frame = con.execute(f"SELECT date, open, high, low, close, volume "
                                f"FROM {table}{where} ORDER BY date", params).fetchdf()
            if not frame.empty:
                frame["date"] = pd.to_datetime(frame["date"])
                frame["volume"] = frame["volume"].astype(float)
                frame.attrs["price_basis"] = label
                return frame
    except Exception:
        pass
    finally:
        con.close()
    return _empty_daily("empty")


def load_daily_total_return(ticker: str, start=None, end=None,
                            allow_fallback: bool = True) -> pd.DataFrame:
    """Dividend-adjusted daily bars. Thin wrapper over `load_daily`, kept for
    the long-run performance callers and for compatibility."""
    return load_daily(ticker, start=start, end=end, basis="total",
                      allow_fallback=allow_fallback)


def _empty_daily(reason: str) -> pd.DataFrame:
    frame = pd.DataFrame(columns=BAR_COLUMNS)
    frame.attrs["price_basis"] = reason
    return frame


def adjustment_check(ticker: str) -> dict | None:
    """Locally derived total-return factor vs Yahoo's own `adj_close`.

    The ratio (close x factor) / adj_close should be constant through history.
    Drift means a dividend is missing or misdated in the stored actions, or
    Yahoo's adjustment disagrees with ours -- either way the total basis for
    this ticker should not be trusted until it is re-pulled.
    """
    path = db_universe_daily()
    if not path.exists():
        return None
    con = duckdb.connect(str(path), read_only=True)
    try:
        frame = con.execute(f"SELECT date, close, adj_close, dividends FROM {RAW_TABLE} "
                            f"WHERE ticker = ? ORDER BY date", [ticker]).fetchdf()
    except Exception:
        return None
    finally:
        con.close()
    frame = frame[(frame["adj_close"] > 0) & (frame["close"] > 0)]
    if frame.empty:
        return None
    ratio = frame["close"] * total_return_factor(frame) / frame["adj_close"]
    ratio = ratio / ratio.iloc[-1]
    deviation = (ratio - 1.0).abs()
    worst = int(np.argmax(deviation.to_numpy()))
    return {"ticker": ticker, "rows": len(frame),
            "max_deviation": float(deviation.iloc[worst]),
            "worst_date": str(pd.Timestamp(frame["date"].iloc[worst]).date())}


def daily_data_status() -> dict:
    """What daily price data actually exists, and which table it is in.

    Added after a validation run reported "no history" for all 61 tickers and
    gave no clue why. The cause was that the bars table had never been built
    -- but every caller swallowed the missing-table error and returned an
    empty frame, so the diagnosis looked like 61 separate data problems
    instead of one missing pipeline stage. Silent degradation is worse than a
    crash: it turns a two-minute fix into an afternoon.
    """
    path = db_universe_daily()
    status = {
        "database": str(path), "exists": path.exists(),
        "raw_rows": 0, "raw_tickers": 0,
        "legacy_rows": 0, "legacy_tickers": 0,
        "legacy_tr_present": False,
        "last_date": None, "stalest_ticker": None, "stalest_date": None,
        "source": "none", "action": None,
        "universe_covered": 0, "universe_size": 0, "coverage": 0.0,
        "mixed_basis": False,
    }
    if not path.exists():
        status["action"] = "No daily database at all. Run:  python pipeline/run.py"
        return status

    con = duckdb.connect(str(path), read_only=True)
    try:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        status["legacy_tr_present"] = "daily_bars_tr" in tables
        covered: set[str] = set()
        if RAW_TABLE in tables:
            row = con.execute(f"SELECT count(*), count(DISTINCT ticker), max(date) "
                              f"FROM {RAW_TABLE}").fetchone()
            status["raw_rows"], status["raw_tickers"] = int(row[0]), int(row[1])
            status["last_date"] = row[2]
            per_ticker = con.execute(f"SELECT ticker, max(date) AS last FROM {RAW_TABLE} "
                                     f"GROUP BY ticker ORDER BY last LIMIT 1").fetchone()
            if per_ticker:
                status["stalest_ticker"], status["stalest_date"] = per_ticker
            covered = {r[0] for r in con.execute(
                f"SELECT DISTINCT ticker FROM {RAW_TABLE}").fetchall()}
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
    # holding two tickers looks healthy by row count, while 59 names silently
    # fall through to a legacy table on a different basis.
    try:
        from core.paths import load_universe
        universe = set(load_universe())
    except Exception:
        universe = set()
    if universe:
        status["universe_covered"] = len(covered & universe)
        status["universe_size"] = len(universe)
        status["coverage"] = len(covered & universe) / len(universe)
        status["mixed_basis"] = 0 < status["coverage"] < 1.0

    if status["raw_rows"]:
        status["source"] = RAW_TABLE
        if status["mixed_basis"]:
            missing = sorted(universe - covered)
            status["action"] = (
                f"MIXED PRICE BASIS: {len(missing)} of {status['universe_size']} "
                f"universe tickers are not in {RAW_TABLE} ({', '.join(missing[:8])}) "
                f"and fall back to a legacy table. Run  python pipeline/run.py  "
                f"to complete the sync.")
    elif status["legacy_rows"]:
        status["source"] = "daily_bars"
        status["action"] = ("Falling back to the legacy split-adjusted table built by "
                            "scripts/01. Run  python pipeline/run.py  to build "
                            f"{RAW_TABLE}.")
    else:
        status["action"] = "Daily database exists but holds no bars. Run:  python pipeline/run.py"
    return status


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
    mapping = _vendor_map(tickers)
    reporter = reporter or NullReporter()
    rows = []
    with reporter.stage("earnings", "Earnings calendar", total=len(tickers)):
        for ticker in tickers:
            try:
                frame = yf.Ticker(mapping[ticker][0]).get_earnings_dates(limit=8)
                if frame is not None and not frame.empty:
                    for stamp, row in frame.iterrows():
                        rows.append({
                            "ticker": ticker,
                            "time_of_day": earnings_time_of_day(stamp),
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
        _merge_into(reference_dir() / EARNINGS_FILE, frame, "ticker", tickers)
    return frame


def earnings_time_of_day(stamp) -> str:
    """bmo / amc / during / unknown from a yfinance earnings timestamp (ET).

    yfinance reports midnight when the time is not known, so 00:00 is
    "unknown" rather than "before the open"."""
    try:
        stamp = pd.Timestamp(stamp)
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert("America/New_York")
    except Exception:
        return "unknown"
    minutes = stamp.hour * 60 + stamp.minute
    if minutes == 0:
        return "unknown"
    if minutes < 9 * 60 + 30:
        return "bmo"
    if minutes >= 16 * 60:
        return "amc"
    return "during"


def _merge_into(path, fresh: pd.DataFrame, key: str, requested: list[str]) -> None:
    """Replace the rows for the requested symbols; keep everyone else's.

    Until Phase 9 both reference files were overwritten with only the
    symbols just pulled, so a `--tickers` subset run (or a one-symbol refresh)
    silently deleted every other stock's earnings dates -- and the fail-safe
    earnings rule then blocked all of them as "unknown"."""
    if path.exists():
        try:
            existing = pd.read_parquet(path)
            existing = existing[~existing[key].isin(requested)]
            fresh = pd.concat([existing, fresh], ignore_index=True)
        except Exception:
            pass
    fresh.to_parquet(path, index=False)


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
    mapping = _vendor_map(tickers)
    reporter = reporter or NullReporter()
    rows = []
    with reporter.stage("dividends", "Dividend ex-dates", total=len(tickers)):
        for ticker in tickers:
            try:
                series = yf.Ticker(mapping[ticker][0]).dividends
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
        _merge_into(reference_dir() / DIVIDENDS_FILE, frame, "symbol", tickers)
    return frame


def write_dividends_from_raw(per_ticker: int = 12) -> pd.DataFrame:
    """Rebuild dividends.parquet from the dividends stored in daily_bars_raw.

    Phase 8: nothing called `sync_dividends`, so the file (and with it the
    covered-call ex-dividend warning) froze in June. The raw daily sync now
    carries every dividend anyway, so this derives the file with no extra
    network calls and runs after every daily stage. Same schema as before.
    """
    path = db_universe_daily()
    if not path.exists():
        return pd.DataFrame()
    con = duckdb.connect(str(path), read_only=True)
    try:
        frame = con.execute(
            f"SELECT ticker AS symbol, date AS ex_date, dividends AS amount FROM ("
            f"  SELECT *, row_number() OVER (PARTITION BY ticker ORDER BY date DESC) AS k"
            f"  FROM {RAW_TABLE} WHERE dividends > 0) WHERE k <= ? "
            f"ORDER BY symbol, ex_date", [per_ticker]).fetchdf()
    except Exception:
        return pd.DataFrame()
    finally:
        con.close()
    if not frame.empty:
        frame["ex_date"] = pd.to_datetime(frame["ex_date"]).dt.date
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
