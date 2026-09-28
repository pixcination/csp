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
Without a scan request: 21 days for the whole universe, 60 for any ticker
carrying an open position. You cannot rank a roll you did not download, and
roll targets sit one to three weeks beyond the leg you are defending.

With a scan request (Phase 11): [request dte_min, request dte_max +
chain_capture.roll_buffer_days], and [0, max(60, that)] for a ticker with an
open position -- and only for the top-N ranked names (`capture_targets`).

STRIKE FILTER (Phase 11)
------------------------
A 30-60 DTE pull of every strike does not fit the dxFeed budget: SPX alone
has ~500 strikes per expiration and two option roots. Before subscribing,
each expiration keeps only

    puts   spot + put_window_em  x EM   (default -3 .. +0.5 EM)
    calls  spot + call_window_em x EM   (default -0.5 .. +1.5 EM)

with EM = spot x IV x sqrt(DTE/365) for THAT expiration, and each window no
narrower than +/- min_window_pct of spot. Calls are kept near the money
because skew, the implied forward and covered calls read them. IV is the
ranking's IV for the request DTE, else the TastyTrade IV index, else 30-day
RV; with none of them the chain is not filtered. If a symbol still exceeds
max_subscriptions_per_symbol, the expirations furthest from the request's
reference DTE are dropped first (recorded on the result).

SPEC WIDENING (Phase 17, review decision 4)
-------------------------------------------
A PMCC's long call (~0.80 delta, ~90 DTE) sits below the call window. For a
request that recommends specs (or names one), each spec in
`chain_capture.widen_for_specs` adds, per ticker whose entry TREND holds
(PMCC: uptrend), a call band covering its delta-selected legs' delta
+/- `widen_delta_pad`, only at expirations inside that leg's role window
(target +/- tolerance). Strikes come from Black-Scholes at the window IV:
K = S exp(sigma^2 t / 2 - N^-1(delta) sigma sqrt(t)). The extra
subscriptions are counted per ticker in the manifest (`extra_subscriptions`).

SYMBOLS
-------
The chain and the underlying quote are requested with the registry's
`tt_symbol` (BRK/B, not BRK.B -- the canonical form returns an empty chain).
Index chains (SPX, NDX, RUT, XSP) come from the same /option-chains endpoint,
verified 2026-09-27. SPX, NDX and RUT return two roots (SPXW/NDXP/RUTW
PM-settled weeklies and the AM-settled standard root), so a row carries
`root_symbol`, `settlement_type` and `expiration_type`; the monthly
expiration appears once per root. The underlying quote works for indices
with the equity market-data kind.
"""
from __future__ import annotations

import asyncio
import json
import datetime as dt
import math
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


def snapshot_dte_window(ref: SnapshotRef) -> tuple[int, int]:
    """The DTE window a snapshot was captured for. Snapshots before Phase 11
    did not record it; they were 0-21 (0-60 with a position), so 0-21 is
    assumed -- the conservative reading, which at worst re-pulls once."""
    try:
        under = pd.read_parquet(ref.underlying_path, columns=["dte_min", "dte_max"])
        lo, hi = under["dte_min"].iloc[0], under["dte_max"].iloc[0]
        if pd.notna(lo) and pd.notna(hi):
            return int(lo), int(hi)
    except Exception:
        pass
    default = load_config().get("chain_capture", {}).get("dte_max_universe", 21)
    return 0, int(default)


def snapshot_dropped(ref: SnapshotRef) -> list[dt.date]:
    """Expirations inside the recorded window that the subscription cap
    dropped when this snapshot was taken (Phase 12)."""
    try:
        under = pd.read_parquet(ref.underlying_path, columns=["expirations_dropped"])
        text = under["expirations_dropped"].iloc[0]
        return [dt.date.fromisoformat(d) for d in str(text).split(",") if d.strip()]             if pd.notna(text) else []
    except Exception:
        return []


def needs_capture(ticker: str, now: dt.datetime | None = None,
                  dte_window: tuple[int, int] | None = None) -> tuple[bool, str]:
    """Should this ticker be re-pulled right now? Returns (yes, reason).

    During RTH a snapshot goes stale on a clock. Outside RTH the underlying
    marks are not changing, so one capture per session block is all the
    information that exists -- unless it does not cover the DTE window now
    asked for (Phase 11: a 30-45 DTE request cannot use a 0-21 snapshot).
    """
    cfg = load_config().get("chain_capture", {})
    info = classify(now)
    block = session_block(now)
    snapshot = existing_snapshot(ticker, block)

    if snapshot is None:
        return True, "no snapshot for this session block"

    if dte_window is not None:
        have = snapshot_dte_window(snapshot)
        if dte_window[0] < have[0] or dte_window[1] > have[1]:
            return True, (f"snapshot covers {have[0]}-{have[1]} DTE, "
                          f"{dte_window[0]}-{dte_window[1]} requested")
        # The subscription cap may have dropped expirations inside the
        # recorded window (Phase 12: a weekly pull keeps the near ones).
        today = (now or dt.datetime.now()).date()
        missing = [d for d in snapshot_dropped(snapshot)
                   if dte_window[0] <= (d - today).days <= dte_window[1]]
        if missing:
            return True, (f"{len(missing)} requested expiration(s) were dropped by the "
                          f"subscription cap in this block's snapshot")

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
    dte_window: tuple[int, int] | None = None
    strikes_listed: int = 0
    filtered: bool = False
    expirations_dropped: list = field(default_factory=list)
    extra_subscriptions: int = 0


@dataclass
class StrikeWindow:
    """Per-expiration strike filter in expected-move units (Phase 11)."""
    spot: float | None = None
    iv: float | None = None
    put_em: tuple[float, float] = (-3.0, 0.5)
    call_em: tuple[float, float] = (-0.5, 1.5)
    min_pct: float = 0.03
    max_subscriptions: int | None = None
    reference_dte: float | None = None
    # Phase 17: extra call bands [(dte_lo, dte_hi, delta_lo, delta_hi)] (spec widening)
    extra_call_bands: tuple = ()

    @classmethod
    def from_config(cls, spot: float | None, iv: float | None,
                    reference_dte: float | None = None,
                    extra_call_bands=()) -> "StrikeWindow":
        cfg = load_config().get("chain_capture", {})
        return cls(spot=spot, iv=iv,
                   put_em=tuple(cfg.get("put_window_em", [-3.0, 0.5])),
                   call_em=tuple(cfg.get("call_window_em", [-0.5, 1.5])),
                   min_pct=float(cfg.get("min_window_pct", 0.03)),
                   max_subscriptions=cfg.get("max_subscriptions_per_symbol", 6000),
                   reference_dte=reference_dte,
                   extra_call_bands=tuple(tuple(b) for b in (extra_call_bands or ())))

    @property
    def active(self) -> bool:
        return bool(self.spot and self.iv and self.iv > 0)

    def bounds(self, side: str, dte: int) -> tuple[float, float]:
        """(low, high) strike bounds for one side at one expiration."""
        if not self.active:
            return (-float("inf"), float("inf"))
        em = self.spot * self.iv * math.sqrt(max(dte, 1) / 365.0)
        lo_k, hi_k = self.put_em if side == "put" else self.call_em
        floor = self.spot * self.min_pct
        lo = min(self.spot + lo_k * em, self.spot - floor)
        hi = max(self.spot + hi_k * em, self.spot + floor)
        return lo, hi

    def extra_bounds(self, side: str, dte: int) -> list[tuple[float, float]]:
        """Phase 17: extra strike intervals for `side` at `dte` (spec widening:
        call deltas -> strikes by Black-Scholes at the window IV)."""
        if side != "call" or not self.active or not self.extra_call_bands:
            return []
        from scipy.stats import norm
        t = max(dte, 1) / 365.0
        sig = self.iv * math.sqrt(t)
        out = []
        for dte_lo, dte_hi, d_lo, d_hi in self.extra_call_bands:
            if not dte_lo <= dte <= dte_hi:
                continue
            k = [self.spot * math.exp(0.5 * sig * sig - float(norm.ppf(d)) * sig)
                 for d in (d_hi, d_lo)]          # higher delta = lower strike
            out.append((min(k), max(k)))
        return out


def _rows_for_chain(ttc, chain: dict, tokens: str,
                    window: StrikeWindow | None = None,
                    today: dt.date | None = None,
                    only: set | None = None) -> tuple[list[dict], list[str], dict, dict]:
    """Chain rows inside the DTE token and the strike window.

    Returns (rows, option symbols to subscribe, symbol -> (side, row),
    info). Only sides inside the window are subscribed; a row with neither
    side inside is dropped.
    """
    items = chain.get("data", {}).get("items", [])
    flat = []
    for item in items:
        flat.extend(item.get("expirations", []))
    chosen = set(ttc.select_expirations(flat, tokens))
    if only is not None:
        chosen &= {str(d) for d in only}
    info = {"expirations_dropped": [], "strikes_listed": 0, "extra_subscriptions": 0}
    if not chosen:
        return [], [], {}, info

    window = window or StrikeWindow()
    today = today or dt.date.today()
    by_exp: dict[str, list[tuple[dict, list[tuple[str, str]]]]] = {}
    for item in items:
        root = item.get("root-symbol")
        for exp in item.get("expirations", []):
            date_str = exp.get("expiration-date")
            if date_str not in chosen:
                continue
            dte = (dt.date.fromisoformat(date_str) - today).days
            put_lo, put_hi = window.bounds("put", dte)
            call_lo, call_hi = window.bounds("call", dte)
            extra_calls = window.extra_bounds("call", dte)
            for strike in exp.get("strikes", []):
                info["strikes_listed"] += 1
                k = float(strike.get("strike-price", 0) or 0)
                sides = []
                if strike.get("put") and put_lo <= k <= put_hi:
                    sides.append(("put", strike["put"]))
                if strike.get("call") and call_lo <= k <= call_hi:
                    sides.append(("call", strike["call"]))
                elif strike.get("call") and any(a <= k <= b for a, b in extra_calls):
                    sides.append(("call", strike["call"]))
                    info["extra_subscriptions"] += 1
                if not sides:
                    continue
                row = ttc.empty_strike_row(date_str, strike.get("strike-price", 0),
                                           strike.get("call"), strike.get("put"))
                row["root_symbol"] = root
                row["settlement_type"] = exp.get("settlement-type")
                row["expiration_type"] = exp.get("expiration-type")
                by_exp.setdefault(date_str, []).append((row, sides))

    # Subscription cap: drop the expirations furthest from the request DTE.
    def count(dates) -> int:
        return sum(len(sides) for d in dates for _, sides in by_exp[d])
    dates = sorted(by_exp)
    cap = window.max_subscriptions
    if cap and count(dates) > cap and len(dates) > 1:
        ref = window.reference_dte if window.reference_dte is not None else 0.0
        by_distance = sorted(dates, key=lambda d: abs(
            (dt.date.fromisoformat(d) - today).days - ref))
        keep = list(by_distance)
        while len(keep) > 1 and count(keep) > cap:
            info["expirations_dropped"].append(keep.pop())
        dates = sorted(keep)

    rows: list[dict] = []
    symbols: list[str] = []
    index: dict[str, tuple[str, dict]] = {}
    for date_str in dates:
        for row, sides in by_exp[date_str]:
            rows.append(row)
            for side, sym in sides:
                symbols.append(sym)
                index[sym] = (side, row)
    return rows, symbols, index, info


def _tt_symbol(ticker: str) -> str:
    try:
        from data_sources import universe
        return universe.tt_symbols([ticker]).get(ticker, ticker)
    except Exception:
        return ticker


def fallback_iv(ticker: str) -> float | None:
    """IV for the strike window when the caller has none: the TastyTrade IV
    index, else 30-day close-to-close RV from the daily bars."""
    try:
        from data_sources import tasty_metrics
        iv = (tasty_metrics.for_symbol(ticker) or {}).get("iv_index")
        if iv and iv == iv and iv > 0:
            return float(iv)
    except Exception:
        pass
    try:
        import numpy as np
        from data_sources.yfinance_sync import load_daily
        bars = load_daily(ticker, basis="price", start=dt.date.today() - dt.timedelta(days=70))
        closes = bars["close"].astype(float).tail(31).to_numpy()
        if len(closes) > 20:
            return float(np.std(np.diff(np.log(closes)), ddof=1) * math.sqrt(252))
    except Exception:
        pass
    return None


def capture(ticker: str, dte_max: int | None = None, dte_min: int = 0,
             limiter: SubscriptionLimiter | None = None,
             force: bool = False,
             reporter: BaseReporter | None = None,
             now: dt.datetime | None = None,
             iv: float | None = None,
             reference_dte: float | None = None,
             strike_filter: bool = True,
             extra_call_bands=(),
             expirations: set | None = None) -> CaptureResult:
    """Pull one ticker's chain and underlying, stamped with the session block.

    REST supplies bid/ask/mark/last; DXLink streaming supplies open interest,
    IV and the Greeks -- REST alone does not populate those. A stream failure
    degrades to REST-only data with a recorded warning rather than losing the
    snapshot entirely.

    The underlying is quoted FIRST (Phase 11): its spot sets the strike
    window. `iv` sets the window's expected move (see `fallback_iv`);
    `strike_filter=False` pulls every strike, as before Phase 11.
    `extra_call_bands`: Phase 17 spec widening (see the module docstring);
    the DTE window is extended to cover them.
    `expirations` (Phase 18, the tracking update): refresh ONLY these
    expirations (dates) and merge them into this block's existing snapshot,
    whose other expirations and recorded window are kept. A 0-54 DTE pull
    of SPX exceeds the subscription cap, which drops the far expirations --
    exactly the ones a 45-DTE position lives in.
    """
    result = CaptureResult(ticker=ticker)
    cfg = load_config().get("chain_capture", {})
    dte_max = dte_max if dte_max is not None else cfg.get("dte_max_universe", 21)
    if extra_call_bands:
        dte_max = max(int(dte_max), max(int(b[1]) for b in extra_call_bands))

    if not force and expirations is None:
        wanted, reason = needs_capture(ticker, now, (dte_min, dte_max))
        if not wanted:
            result.skipped, result.reason = True, reason
            result.ref = existing_snapshot(ticker, session_block(now))
            if result.ref is not None:
                result.dte_window = snapshot_dte_window(result.ref)
            result.ok = True
            return result

    block = session_block(now)
    info = classify(now)
    captured_at = dt.datetime.now()

    # Never lose coverage within a block: a 30-59 DTE request re-pulling a
    # symbol captured 0-21 earlier in the same block pulls 0-59, not 30-59,
    # or a later --quick CSP run would find its weekly strikes gone.
    existing = existing_snapshot(ticker, block)
    recorded = (int(dte_min), int(dte_max))
    if existing is not None:
        have = snapshot_dte_window(existing)
        recorded = (min(dte_min, have[0]), max(dte_max, have[1]))
        if expirations is None:
            dte_min, dte_max = recorded
    if expirations is not None:
        today_ = (now or dt.datetime.now()).date()
        days = [(pd.Timestamp(d).date() - today_).days for d in expirations]
        dte_min, dte_max = max(min(days), 0), max(max(days), 0)
    result.dte_window = (int(dte_min), int(dte_max))

    try:
        ttc = tt.common()
        tt.authenticate()
        tt_symbol = _tt_symbol(ticker)

        # Underlying first: its spot sets the strike window.
        under = ttc.fetch_rest_market_data([tt_symbol], kind="equity").get(tt_symbol, {})
        under_row = ttc.underlying_row_from_quote(under, "Equity") if under else {
            "symbol": tt_symbol}
        spot = next((float(under_row[c]) for c in ("mark", "last", "bid")
                     if under_row.get(c) is not None and float(under_row[c]) > 0), None)
        window = StrikeWindow.from_config(
            spot, (iv or fallback_iv(ticker)) if strike_filter else None, reference_dte,
            extra_call_bands)

        chain = ttc.fetch_equity_chain(tt_symbol)
        rows, symbols, index, listing = _rows_for_chain(
            ttc, chain, tt.dte_token(dte_min, dte_max), window,
            (now or dt.datetime.now()).date(),
            {str(pd.Timestamp(d).date()) for d in expirations} if expirations else None)
        result.strikes_listed = listing["strikes_listed"]
        result.extra_subscriptions = listing.get("extra_subscriptions", 0)
        result.expirations_dropped = listing["expirations_dropped"]
        result.filtered = window.active
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

        frame = pd.DataFrame(rows)
        frame["capture_block"] = block
        frame["captured_at"] = captured_at
        frame["session_state"] = info.state.value
        frame["greeks_from_stream"] = bool(stream)

        under_frame = pd.DataFrame([{
            **under_row,
            "symbol": ticker,
            "tt_symbol": tt_symbol,
            "strike_window_iv": window.iv if window.active else None,
            "capture_block": block,
            "captured_at": captured_at,
            "session_state": info.state.value,
            "dte_min": int(dte_min),
            "dte_max": int(dte_max),
            "expirations_dropped": ",".join(listing["expirations_dropped"]),
            "extra_call_bands": json.dumps([list(b) for b in extra_call_bands])
            if extra_call_bands else None,
        }])

        chain_path, under_path = _paths(ticker, block)
        chain_path.parent.mkdir(parents=True, exist_ok=True)
        if expirations is not None:
            # Targeted refresh: replace these expirations, keep the rest of
            # the block's snapshot and its recorded DTE window.
            if existing is not None:
                old = pd.read_parquet(existing.chain_path)
                fresh = set(frame["expiration"].astype(str))
                old = old[~old["expiration"].astype(str).isin(fresh)]
                frame = pd.concat([old, frame], ignore_index=True)
            under_frame["dte_min"], under_frame["dte_max"] = recorded
            result.dte_window = recorded
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
            reporter.advance(1, note=_note(res))
    return results


def _note(res: CaptureResult) -> str:
    if res.error:
        return f"{res.ticker} FAILED"
    if res.skipped:
        return f"{res.ticker} {res.reason}"
    extra = f", {len(res.expirations_dropped)} exp over budget" if res.expirations_dropped else ""
    return (f"{res.ticker} {res.rows}/{res.strikes_listed} strikes / "
            f"{res.expirations} exp, {res.subscriptions} subs{extra}")


def spec_widening(request, tickers: list[str], trends: dict[str, str] | None = None
                  ) -> dict[str, list[tuple]]:
    """Phase 17: ticker -> extra call bands for the request's specs listed in
    `chain_capture.widen_for_specs`, only where the spec's entry trend holds
    (`trends`: ticker -> trend state; unknown trend = no widening)."""
    cfg = load_config().get("chain_capture", {}) or {}
    wanted = list(cfg.get("widen_for_specs") or [])
    if not wanted or not (request.recommend or request.specs):
        return {}
    from analytics import strategy_spec
    specs = strategy_spec.load_all()
    chosen = [specs[s] for s in wanted if s in specs
              and (request.recommend or s in (request.specs or []))]
    pad = float(cfg.get("widen_delta_pad", 0.12))
    out: dict[str, list[tuple]] = {}
    for spec in chosen:
        bands = []
        for leg in spec.legs:
            if leg.type != "call" or leg.selector != "delta":
                continue
            d = abs(float(leg.select["delta"]))
            exp = spec.expirations[leg.expiration]
            t, tol = int(exp["dte_target"]), int(exp.get("tolerance", 14))
            bands.append((max(t - tol, 0), t + tol, max(d - pad, 0.01), min(d + pad, 0.99)))
        allowed = spec.entry.get("trend")
        for ticker in tickers:
            if allowed and (trends or {}).get(ticker) not in allowed:
                continue
            out.setdefault(ticker, []).extend(bands)
    return out


def capture_targets(tickers: list[str], request, with_positions: set[str] | None = None,
                    ivs: dict[str, float] | None = None, force: bool = False,
                    reporter: BaseReporter | None = None,
                    widen: dict[str, list[tuple]] | None = None) -> list[CaptureResult]:
    """Phase 11: capture the ranked top N for a scan request.

    DTE window = the request's chain window (entry window + roll buffer);
    a ticker with an open position gets [0, max(dte_max_with_position, that)].
    `ivs` (symbol -> IV at the request DTE, from the ranking) sets each
    strike window.
    """
    cfg = load_config().get("chain_capture", {})
    position_dte = int(cfg.get("dte_max_with_position", 60))
    with_positions = with_positions or set()
    ivs = ivs or {}
    lo, hi = request.chain_dte_window()
    ref = request.reference_dte()

    reporter = reporter or NullReporter()
    limiter = SubscriptionLimiter(load_config().get("stage3_subs_per_minute_budget", 8000))
    results: list[CaptureResult] = []
    with reporter.stage("chains", "Option chains", total=len(tickers)):
        for ticker in tickers:
            window = (0, max(position_dte, hi)) if ticker in with_positions else (lo, hi)
            res = capture(ticker, dte_min=window[0], dte_max=window[1], limiter=limiter,
                          force=force, reporter=reporter, iv=ivs.get(ticker),
                          reference_dte=ref, extra_call_bands=(widen or {}).get(ticker, ()))
            results.append(res)
            reporter.advance(1, note=_note(res))
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
