"""
Massive.com historical archive sync -- the fix for finding F-07.

ROLE IN THE ARCHITECTURE
-----------------------
The free Massive tier is 5 REST requests per minute, end-of-day only. That
makes it categorically unsuitable for interactive use: refreshing 61 tickers
for one month costs 61 requests, which is 13 minutes of pure waiting before
any analysis starts.

So Massive is **not** on the activation button's path. It is a background
archiver whose only job is to keep the long 1-minute history complete, which
is what the volatility estimators, the empirical move engine and the backtest
read. Live pricing for the interactive run comes from TastyTrade's DXLink
stream instead (see data_sources/quotes.py), which is real-time, already
authenticated, and has no per-minute request cap because it is a websocket
subscription rather than a REST poll.

The division of labour:

    Massive     -> deep history, updated nightly, never blocks a decision
    TastyTrade  -> right now, on demand, drives every number you trade on

WHAT CHANGED FROM massive_downloader.py
---------------------------------------
* Incremental: reads the last stored bar per ticker and requests only from
  there forward, instead of re-downloading whole months from a hardcoded list.
* Merges rather than overwrites, so a partially-downloaded month is repaired
  rather than replaced, and an interrupted run resumes.
* Token-bucket pacing at the real documented limit, with retry/backoff on 429.
* No API key literal in the source (see core/env.py).
"""
from __future__ import annotations

import datetime as dt
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from core import env
from core.market_calendar import ET, is_trading_day, previous_trading_day
from core.paths import load_config, raw_1m_dir
from core.progress import BaseReporter, NullReporter

HEADER = "Datetime,Open,High,Low,Close,Volume"
MARKET_TZ = ET


# --- Rate limiting ---------------------------------------------------------

class TokenBucket:
    """Rolling-window limiter honouring N requests per 60 seconds.

    A fixed `sleep(13)` between calls (the old approach) wastes time whenever a
    request itself takes a while, and still bunches at the window boundary.
    This tracks actual send times and waits only as long as it must.
    """

    def __init__(self, per_minute: int = 5, safety: float = 0.9):
        self.capacity = max(1, int(per_minute * safety))
        self.sent: deque[float] = deque()

    def wait(self) -> float:
        now = time.monotonic()
        while self.sent and now - self.sent[0] > 60.0:
            self.sent.popleft()
        waited = 0.0
        if len(self.sent) >= self.capacity:
            sleep_for = 60.0 - (now - self.sent[0]) + 0.05
            if sleep_for > 0:
                time.sleep(sleep_for)
                waited = sleep_for
            now = time.monotonic()
            while self.sent and now - self.sent[0] > 60.0:
                self.sent.popleft()
        self.sent.append(time.monotonic())
        return waited

    def estimated_seconds(self, requests: int) -> float:
        """Wall-clock estimate for N requests, for the progress bar's ETA."""
        if requests <= self.capacity:
            return 0.0
        return (requests - self.capacity) * (60.0 / self.capacity)


# --- On-disk archive -------------------------------------------------------

def archive_root() -> Path:
    """The canonical intraday store: `data/raw_1m/` inside the project.

    Post-consolidation this is where 1-minute history lives and where new bars
    are written. The external `pricing_data_root` archive is read-only and
    exists only so `scripts/01` and `scripts/03` can re-screen a new universe
    out of the full ~1,050-ticker pool -- it is not required for the app to
    run, and nothing here writes to it.
    """
    return raw_1m_dir()


def month_file(ticker: str, year: int, month: int, root: Path | None = None) -> Path:
    base = root or archive_root()
    return base / ticker / f"{ticker}_{year:04d}-{month:02d}_1m.txt"


def _months_between(start: dt.date, end: dt.date) -> list[tuple[int, int]]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def read_month(path: Path) -> dict[str, str]:
    """Existing rows keyed by timestamp, so a re-download merges cleanly."""
    if not path.exists():
        return {}
    rows: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            line = line.rstrip("\n")
            if i == 0 and line.lower().startswith("datetime"):
                continue
            if not line:
                continue
            rows[line.split(",", 1)[0]] = line
    return rows


def write_month(path: Path, rows: dict[str, str]) -> int:
    """Write reverse-chronological, matching the archive's existing convention
    (scripts/01 and scripts/06 both sort explicitly on ingest)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows.values(), key=lambda r: r.split(",", 1)[0], reverse=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(HEADER + "\n")
        for row in ordered:
            fh.write(row + "\n")
    tmp.replace(path)
    return len(ordered)


def last_stored_bar(ticker: str, root: Path | None = None) -> dt.datetime | None:
    """Newest timestamp already on disk for this ticker.

    Files are reverse-chronological, so the first data line of the newest file
    is the answer -- no need to parse the whole archive.
    """
    base = (root or archive_root()) / ticker
    if not base.is_dir():
        return None
    files = sorted(base.glob(f"{ticker}_*_1m.txt"))
    for path in reversed(files):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                fh.readline()  # header
                first = fh.readline().strip()
            if first:
                return dt.datetime.strptime(first.split(",", 1)[0], "%Y-%m-%d %H:%M")
        except (OSError, ValueError):
            continue
    return None


# --- Sync ------------------------------------------------------------------

@dataclass
class TickerSyncResult:
    ticker: str
    months_requested: int = 0
    rows_added: int = 0
    up_to_date: bool = False
    error: str | None = None
    last_bar: dt.datetime | None = None


def _client():
    key = env.require("MASSIVE_API_KEY")
    try:
        from massive import RESTClient
    except ImportError as exc:
        raise RuntimeError(
            "The 'massive' package is not installed. Run:\n"
            "    pip install massive-api\n"
            "(see https://github.com/massive-com/client-python)"
        ) from exc
    return RESTClient(key)


def _agg_to_row(agg) -> tuple[str, str]:
    local = dt.datetime.fromtimestamp(agg.timestamp / 1000,
                                       tz=ZoneInfo("UTC")).astimezone(MARKET_TZ)
    stamp = local.strftime("%Y-%m-%d %H:%M")
    return stamp, (f"{stamp},{agg.open},{agg.high},{agg.low},"
                   f"{agg.close},{int(agg.volume)}")


def latest_complete_session(today: dt.date | None = None) -> dt.date:
    """The newest session Massive can be expected to have finalised.

    End-of-day data for a session is not available until after that session
    closes and settles, so the safe watermark is the previous trading day.
    """
    today = today or dt.datetime.now(ET).date()
    return previous_trading_day(today) if is_trading_day(today) else previous_trading_day(today + dt.timedelta(days=1))


def sync_ticker(client, ticker: str, bucket: TokenBucket,
                 root: Path | None = None,
                 through: dt.date | None = None,
                 max_backfill_months: int = 6,
                 reporter: BaseReporter | None = None) -> TickerSyncResult:
    """Bring one ticker's 1-minute archive up to the last complete session."""
    result = TickerSyncResult(ticker=ticker)
    root = root or archive_root()
    through = through or latest_complete_session()

    last = last_stored_bar(ticker, root)
    result.last_bar = last
    if last is None:
        start = dt.date(through.year, through.month, 1)
        for _ in range(max_backfill_months - 1):
            start = (start - dt.timedelta(days=1)).replace(day=1)
    else:
        if last.date() >= through:
            result.up_to_date = True
            return result
        start = last.date().replace(day=1)

    cfg = load_config().get("massive", {})
    retries = int(cfg.get("max_retries", 3))

    for year, month in _months_between(start, through):
        path = month_file(ticker, year, month, root)
        existing = read_month(path)
        first = dt.date(year, month, 1)
        last_of_month = (dt.date(year + (month == 12), (month % 12) + 1, 1)
                          - dt.timedelta(days=1))
        window_end = min(last_of_month, through)
        # Resume mid-month: only ask for days we do not already hold.
        if existing:
            newest = max(existing)
            resume = dt.datetime.strptime(newest, "%Y-%m-%d %H:%M").date()
            if resume >= window_end:
                continue
            window_start = max(first, resume)
        else:
            window_start = first

        fetched: dict[str, str] = {}
        for attempt in range(1, retries + 1):
            bucket.wait()
            result.months_requested += 1
            try:
                for agg in client.list_aggs(
                        ticker=ticker, multiplier=1, timespan="minute",
                        from_=window_start.isoformat(), to=window_end.isoformat(),
                        adjusted=True, sort="asc", limit=50000):
                    stamp, row = _agg_to_row(agg)
                    fetched[stamp] = row
                break
            except Exception as exc:
                message = str(exc)
                if attempt >= retries:
                    result.error = f"{year}-{month:02d}: {message[:160]}"
                    if reporter:
                        reporter.log(f"{ticker} {year}-{month:02d} failed: {message[:80]}")
                    break
                # Exponential backoff; a 429 means we mis-estimated the window.
                time.sleep(min(60.0, 5.0 * (2 ** (attempt - 1))))

        if not fetched:
            continue
        merged = {**existing, **fetched}
        added = len(merged) - len(existing)
        if added > 0 or len(fetched) != len(existing):
            write_month(path, merged)
        result.rows_added += max(added, 0)

    return result


def sync_universe(tickers: list[str], root: Path | None = None,
                   through: dt.date | None = None,
                   reporter: BaseReporter | None = None,
                   allow_interactive: bool = False) -> list[TickerSyncResult]:
    """Nightly archive refresh across the tradable universe.

    Refuses to run from an interactive pipeline unless explicitly permitted:
    at 5 requests per minute this stage is measured in tens of minutes, and
    silently blocking the activation button behind it is exactly the failure
    mode the architecture is designed to avoid.
    """
    cfg = load_config().get("massive", {})
    if not allow_interactive and not cfg.get("interactive", False):
        raise RuntimeError(
            "Massive sync is a background job, not an interactive stage. "
            "Free-tier pacing is 5 requests/minute -- a full universe refresh "
            "takes tens of minutes. Run scripts/sync_archive.py on a schedule, "
            "or pass allow_interactive=True if you really mean to wait."
        )

    reporter = reporter or NullReporter()
    bucket = TokenBucket(per_minute=int(cfg.get("requests_per_minute", 5)),
                          safety=float(cfg.get("safety_margin", 0.9)))
    client = _client()
    results: list[TickerSyncResult] = []

    with reporter.stage("massive", "Massive 1-minute archive", total=len(tickers)):
        for ticker in tickers:
            res = sync_ticker(client, ticker, bucket, root, through, reporter=reporter)
            results.append(res)
            note = ("current" if res.up_to_date
                    else f"+{res.rows_added:,} bars" if not res.error
                    else "error")
            reporter.advance(1, note=f"{ticker} {note}")
    return results


def archive_status(tickers: list[str], root: Path | None = None) -> list[dict]:
    """How stale is each ticker's archive? Read-only; no API calls.

    The preflight shows this so you know whether the analytics are running on
    current data before you trust a recommendation.
    """
    through = latest_complete_session()
    rows = []
    for ticker in tickers:
        last = last_stored_bar(ticker, root)
        gap = (through - last.date()).days if last else None
        rows.append({
            "ticker": ticker,
            "last_bar": last.isoformat(sep=" ") if last else None,
            "days_behind": gap,
            "current": bool(last and last.date() >= through),
        })
    return rows
