"""
Option-chain capture -- the fix for finding F-02.

TWO CHANGES FROM scripts/04_stage3_chain_scan.py
------------------------------------------------
**Session stamping.** Every snapshot now records the session block it was
taken in (`2026-08-21_rth_14`, `2026-08-21_closed`, ...) rather than just
today's date. Captures from Friday's close through Monday's open collapse
into one block, so studying over a weekend overwrites a single file instead
of manufacturing three "independent" IV observations of one stale quote.
`core.market_calendar.is_ingestable_for_iv_history` then keeps non-RTH marks
out of the IV-rank sample entirely.

**Staleness instead of once-a-day.** The old `already_scanned_today()` guard
skipped a symbol if any file existed for the date, which meant a 9:45am scan
blocked a 2:00pm refresh -- exactly backwards during a live session. Capture
is now driven by age: re-pull during RTH if the newest snapshot is older than
`chain_capture.rth_refresh_minutes`, and outside RTH keep one snapshot per
session block.

DTE WINDOW
----------
21 days for the whole universe, 60 for any ticker carrying an open position.
The entry window is still 5-10 DTE, but you cannot rank a roll you did not
download, and roll targets sit one to three weeks beyond the leg you are
defending.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from core.market_calendar import SessionState, classify, session_block
from core.paths import chains_dir, load_config
from core.progress import BaseReporter, NullReporter
from data_sources import tastytrade_client as tt

STRIKE_FIELDS = ("last", "bid", "ask", "mark", "volume", "open_interest",
                  "iv", "delta", "gamma", "theta", "vega", "rho")


# --- dxFeed pacing ---------------------------------------------------------

class SubscriptionLimiter:
    """Rolling-window limiter for dxFeed subscription changes.

    TastyTrade's dxFeed allows 25,000 subscriptions per session and 10,000
    subscription changes per minute. A single symbol restricted to 21 DTE is
    small; the aggregate across a 61-symbol sweep is what needs pacing.
    """

    def __init__(self, budget_per_min: int = 8000):
        self.budget = budget_per_min
        self.events: deque[tuple[float, int]] = deque()

    def _trim(self, now: float) -> int:
        while self.events and now - self.events[0][0] > 60.0:
            self.events.popleft()
        return sum(count for _, count in self.events)

    def wait(self, upcoming: int, reporter: BaseReporter | None = None) -> None:
        now = time.monotonic()
        used = self._trim(now)
        while used + upcoming > self.budget and self.events:
            oldest = self.events[0][0]
            sleep_for = 60.0 - (now - oldest) + 0.25
            if sleep_for <= 0:
                break
            if reporter:
                reporter.log(f"dxFeed budget reached, pausing {sleep_for:.0f}s")
            time.sleep(sleep_for)
            now = time.monotonic()
            used = self._trim(now)

    def record(self, count: int) -> None:
        self.events.append((time.monotonic(), count))


# --- Snapshot identity -----------------------------------------------------

@dataclass(frozen=True)
class SnapshotRef:
    ticker: str
    block: str
    chain_path: Path
    underlying_path: Path
    captured_at: dt.datetime
    session_state: str
    rows: int = 0

    @property
    def is_rth(self) -> bool:
        return self.session_state == SessionState.RTH.value


def _paths(ticker: str, block: str) -> tuple[Path, Path]:
    folder = chains_dir() / block
    return (folder / f"{ticker}_chain.parquet",
            folder / f"{ticker}_underlying.parquet")


def existing_snapshot(ticker: str, block: str | None = None) -> SnapshotRef | None:
    block = block or session_block()
    chain_path, under_path = _paths(ticker, block)
    if not (chain_path.exists() and under_path.exists()):
        return None
    stat = chain_path.stat()
    return SnapshotRef(
        ticker=ticker, block=block, chain_path=chain_path,
        underlying_path=under_path,
        captured_at=dt.datetime.fromtimestamp(stat.st_mtime),
        session_state=block.split("_", 1)[1].split("_")[0]
        if "_" in block else "unknown")


def needs_capture(ticker: str, now: dt.datetime | None = None) -> tuple[bool, str]:
    """Should this ticker be re-pulled right now? Returns (yes, reason).

    During RTH a snapshot goes stale on a clock. Outside RTH the underlying
    marks are not changing, so one capture per session block is all the
    information that exists.
    """
    cfg = load_config().get("chain_capture", {})
    info = classify(now)
    block = session_block(now)
    snapshot = existing_snapshot(ticker, block)

    if snapshot is None:
        return True, "no snapshot for this session block"

    if info.state is SessionState.RTH:
        max_age = dt.timedelta(minutes=cfg.get("rth_refresh_minutes", 20))
        age = dt.datetime.now() - snapshot.captured_at
        if age > max_age:
            return True, f"snapshot is {age.total_seconds() / 60:.0f} min old during RTH"
        return False, f"captured {age.total_seconds() / 60:.0f} min ago"

    return False, f"one snapshot per closed session ({block})"


# --- Capture ---------------------------------------------------------------

@dataclass
class CaptureResult:
    ticker: str
    ok: bool = False
    rows: int = 0
    expirations: int = 0
    subscriptions: int = 0
    skipped: bool = False
    reason: str = ""
    error: str | None = None
    ref: SnapshotRef | None = None


def _rows_for_chain(ttc, chain: dict, tokens: str) -> tuple[list[dict], list[str], dict]:
    items = chain.get("data", {}).get("items", [])
    flat = []
    for item in items:
        flat.extend(item.get("expirations", []))
    chosen = set(ttc.select_expirations(flat, tokens))
    if not chosen:
        return [], [], {}

    rows: list[dict] = []
    symbols: list[str] = []
    index: dict[str, tuple[str, dict]] = {}
    for item in items:
        for exp in item.get("expirations", []):
            date_str = exp.get("expiration-date")
            if date_str not in chosen:
                continue
            for strike in exp.get("strikes", []):
                call_sym, put_sym = strike.get("call"), strike.get("put")
                row = ttc.empty_strike_row(date_str, strike.get("strike-price", 0),
                                            call_sym, put_sym)
                rows.append(row)
                for sym, side in ((call_sym, "call"), (put_sym, "put")):
                    if sym:
                        symbols.append(sym)
                        index[sym] = (side, row)
    return rows, symbols, index


def capture(ticker: str, dte_max: int | None = None, dte_min: int = 0,
             limiter: SubscriptionLimiter | None = None,
             force: bool = False,
             reporter: BaseReporter | None = None,
             now: dt.datetime | None = None) -> CaptureResult:
    """Pull one ticker's chain and underlying, stamped with the session block.

    REST supplies bid/ask/mark/last; DXLink streaming supplies open interest,
    IV and the Greeks -- REST alone does not populate those. A stream failure
    degrades to REST-only data with a recorded warning rather than losing the
    snapshot entirely.
    """
    result = CaptureResult(ticker=ticker)
    cfg = load_config().get("chain_capture", {})
    dte_max = dte_max if dte_max is not None else cfg.get("dte_max_universe", 21)

    if not force:
        wanted, reason = needs_capture(ticker, now)
        if not wanted:
            result.skipped, result.reason = True, reason
            result.ref = existing_snapshot(ticker, session_block(now))
            result.ok = True
            return result

    block = session_block(now)
    info = classify(now)
    captured_at = dt.datetime.now()

    try:
        ttc = tt.common()
        tt.authenticate()
        chain = ttc.fetch_equity_chain(ticker)
        rows, symbols, index = _rows_for_chain(ttc, chain, tt.dte_token(dte_min, dte_max))
        if not rows:
            result.reason = f"no expirations within {dte_min}-{dte_max} DTE"
            result.ok = True
            result.skipped = True
            return result

        # REST first: bid/ask/mark/last for every contract.
        for sym, info_dict in ttc.fetch_rest_market_data(
                symbols, kind="equity-option").items():
            if sym in index:
                side, row = index[sym]
                ttc.apply_rest(row, side, info_dict)

        # DXLink second: open interest, IV, Greeks.
        dx_symbols, dx_to_tasty = ttc.build_equity_streamer_symbols(symbols)
        result.subscriptions = len(dx_symbols)
        if limiter:
            limiter.wait(len(dx_symbols), reporter)
        collect = 8 + 0.5 * (len(dx_symbols) / 1000.0)
        try:
            quote_token, dxlink_url = ttc.fetch_quote_token()
            stream = asyncio.run(ttc.stream_market_events_once(
                dxlink_url, quote_token, dx_symbols, collect))
        except Exception as exc:
            stream = {}
            if reporter:
                reporter.log(f"{ticker}: stream failed ({str(exc)[:60]}); REST only")
        if limiter:
            limiter.record(len(dx_symbols))

        for dx_sym, payload in stream.items():
            tasty_sym = dx_to_tasty.get(dx_sym)
            if tasty_sym and tasty_sym in index:
                side, row = index[tasty_sym]
                ttc.apply_stream(row, side, payload)

        # Underlying, captured in the same pass so spot and chain agree.
        under = ttc.fetch_rest_market_data([ticker], kind="equity").get(ticker, {})
        under_row = ttc.underlying_row_from_quote(under, "Equity") if under else {
            "symbol": ticker}

        frame = pd.DataFrame(rows)
        frame["capture_block"] = block
        frame["captured_at"] = captured_at
        frame["session_state"] = info.state.value
        frame["greeks_from_stream"] = bool(stream)

        under_frame = pd.DataFrame([{
            **under_row,
            "capture_block": block,
            "captured_at": captured_at,
            "session_state": info.state.value,
        }])

        chain_path, under_path = _paths(ticker, block)
        chain_path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(chain_path, index=False)
        under_frame.to_parquet(under_path, index=False)

        result.ok = True
        result.rows = len(frame)
        result.expirations = frame["expiration"].nunique() if "expiration" in frame else 0
        result.ref = SnapshotRef(ticker, block, chain_path, under_path,
                                  captured_at, info.state.value, len(frame))
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def capture_universe(tickers: list[str], with_positions: set[str] | None = None,
                      force: bool = False,
                      reporter: BaseReporter | None = None) -> list[CaptureResult]:
    """Capture the whole universe, widening the window where a position is open."""
    cfg = load_config().get("chain_capture", {})
    universe_dte = cfg.get("dte_max_universe", 21)
    position_dte = cfg.get("dte_max_with_position", 60)
    with_positions = with_positions or set()

    reporter = reporter or NullReporter()
    limiter = SubscriptionLimiter(
        load_config().get("stage3_subs_per_minute_budget", 8000))
    results: list[CaptureResult] = []

    with reporter.stage("chains", "Option chains", total=len(tickers)):
        for ticker in tickers:
            dte_max = position_dte if ticker in with_positions else universe_dte
            res = capture(ticker, dte_max=dte_max, limiter=limiter,
                           force=force, reporter=reporter)
            results.append(res)
            if res.error:
                note = f"{ticker} FAILED"
            elif res.skipped:
                note = f"{ticker} {res.reason}"
            else:
                note = f"{ticker} {res.rows} strikes / {res.expirations} exp"
            reporter.advance(1, note=note)
    return results


# --- Reading ---------------------------------------------------------------

def list_blocks() -> list[str]:
    root = chains_dir()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def latest_block_for(ticker: str, rth_only: bool = False) -> str | None:
    for block in reversed(list_blocks()):
        if rth_only and "_rth_" not in block:
            continue
        if _paths(ticker, block)[0].exists():
            return block
    return None


def load_chain(ticker: str, block: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load a snapshot. Defaults to the most recent one for that ticker."""
    block = block or latest_block_for(ticker)
    if block is None:
        return pd.DataFrame(), pd.DataFrame()
    chain_path, under_path = _paths(ticker, block)
    if not chain_path.exists():
        return pd.DataFrame(), pd.DataFrame()
    chain = pd.read_parquet(chain_path)
    under = pd.read_parquet(under_path) if under_path.exists() else pd.DataFrame()
    return chain, under


def spot_from_underlying(under: pd.DataFrame) -> float | None:
    if under.empty:
        return None
    for column in ("mark", "last", "bid"):
        if column in under.columns:
            value = under[column].iloc[0]
            if pd.notna(value) and float(value) > 0:
                return float(value)
    return None


def iv_history_blocks() -> list[str]:
    """Blocks eligible to become IV-rank observations: regular session only."""
    from core.market_calendar import is_ingestable_for_iv_history
    return [b for b in list_blocks() if is_ingestable_for_iv_history(b)]
