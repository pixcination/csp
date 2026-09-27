"""
snapshot_loop.py
================
Periodic option-chain snapshot downloader.

Runs on a fixed 5-minute schedule. Equity snapshots cover 09:15-16:20 ET
Mon-Fri (capturing the 15-min pre-open and 20-min post-close windows
around regular trading hours). Futures snapshots cover the entire CME
session (Sun 18:00 ET through Fri 17:00 ET, minus the daily 17:00-18:00
maintenance pause).

Authenticates fresh each cycle to avoid access-token expiry issues, and
writes Parquet snapshots into a date-partitioned tree.

USAGE
-----
    python snapshot_loop.py                       # run the schedule
    python snapshot_loop.py --once                # one snapshot now, then exit
    python snapshot_loop.py --watchlist my.yaml   # custom watchlist

OUTPUT
------
D:\\tastytrade\\snapshots\\YYYY-MM-DD\\
    SPY_full_chain_HHMMSS.parquet
    QQQ_full_chain_HHMMSS.parquet
    ES_futures_options_HHMMSS.parquet
    ...
    _events.log

SCHEDULE
--------
Interval: 5 minutes, aligned to :00 :05 :10 :15 :20 ... boundaries.

Equity (SPY, QQQ, IWM, etc.):
    Mon-Fri 09:15-16:20 ET, excluding US market holidays.
    The 09:15 pre-open snapshot captures yesterday's close as a baseline.
    The 16:20 snapshot ensures the 16:15 cycle completes cleanly.
    On early-close days the window adjusts: pre-open 09:15-09:30 plus
    RTH to the early close (typically 13:00 ET) plus 20-min post-close.

Futures (ES, NQ, etc.):
    Sun 18:00 ET through Fri 17:00 ET, with 17:00-18:00 maintenance pause.

REQUIRED
--------
    pip install requests python-dotenv websockets pyyaml pyarrow
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
import traceback
from datetime import datetime, date, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

# Make script self-locating: prepend its own directory to sys.path so
# `import tastytrade_common` works no matter what the current working
# directory is. This matters for Task Scheduler, pythonw, or any other
# launch context where CWD isn't the script's folder.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import tastytrade_common as ttc


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ET = ZoneInfo("America/New_York")
SNAPSHOT_DIR = ttc.BASE_SAVE_DIR / "snapshots"

# Snapshot cadence. Fixed at 5 minutes, aligned to interval boundaries.
INTERVAL_MINUTES = 5

# Equity snapshot window: 15-min pre-open buffer + RTH + 20-min post-close
# buffer. The pre-open snapshot captures yesterday's close as a baseline;
# the post-close buffer ensures the 16:15 cycle has time to finish before
# the window ends.
EQUITY_OPEN_BUFFER  = dtime(9, 15)
EQUITY_CLOSE_BUFFER = dtime(16, 20)
EQUITY_RTH_OPEN     = dtime(9, 30)  # informational; used in early-close logic

# US equity market holidays for 2026 (NYSE / CBOE).
# Update annually. Source: nyse.com/markets/hours-calendars
US_MARKET_HOLIDAYS_2026 = {
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK Day
    date(2026, 2, 16),   # Presidents Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed - July 4 is Saturday)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
}

# Equity early-close days (1:00 PM ET close). Add as needed.
US_MARKET_EARLY_CLOSE_2026 = {
    date(2026, 7, 2):  dtime(13, 0),  # day before Independence Day
    date(2026, 11, 27): dtime(13, 0),  # day after Thanksgiving
    date(2026, 12, 24): dtime(13, 0),  # Christmas Eve
}

# Post-close buffer length, applied uniformly to both regular-close
# (16:00 -> 16:20) and early-close (13:00 -> 13:20) days.
POST_CLOSE_BUFFER_MINUTES = 20


# ---------------------------------------------------------------------------
# Market-hours logic
# ---------------------------------------------------------------------------
def equity_market_open(now_et: datetime) -> bool:
    """True if the equity snapshot window is active at the given ET time.

    Schedule: Mon-Fri, 09:15 ET through 16:20 ET, excluding US market
    holidays. This brackets the official 09:30-16:00 RTH with a 15-min
    pre-open buffer (capturing yesterday's close as a baseline) and a
    20-min post-close buffer (ensuring the 16:15 snapshot has time to
    complete cleanly).

    Early-close days (typically 13:00 ET) get the same window structure:
    pre-open buffer at 09:15, RTH to the early close, then a 20-min
    post-close buffer (e.g., 13:00 -> 13:20).

    Outside RTH (so 09:15-09:30 and 16:00-16:20 on regular days,
    plus the early-close post-window), Tastytrade returns the most
    recent regular-session close for bid/ask/mark, with Greeks
    recomputed against any pre/post-market movement in the underlying.
    """
    d = now_et.date()
    if d in US_MARKET_HOLIDAYS_2026:
        return False
    if now_et.weekday() >= 5:  # Sat/Sun
        return False
    t = now_et.time()
    if t < EQUITY_OPEN_BUFFER:
        return False
    early_close = US_MARKET_EARLY_CLOSE_2026.get(d)
    if early_close:
        # Add a 20-min post-close buffer after the early close.
        buf_dt = (datetime.combine(d, early_close)
                   + timedelta(minutes=POST_CLOSE_BUFFER_MINUTES))
        return t < buf_dt.time()
    return t < EQUITY_CLOSE_BUFFER


def futures_market_open(now_et: datetime) -> bool:
    """True if CME futures session is active at the given ET timestamp.

    Sun 18:00 ET through Fri 17:00 ET, with a 17:00-18:00 ET pause
    each weekday.

    Holidays: futures sessions on US holidays are variable by product
    (often shortened). For simplicity we follow regular schedule. If a
    snapshot happens to come back stale on a holiday, that's fine; the
    file still gets written.
    """
    wd = now_et.weekday()  # Mon=0 ... Sun=6
    t = now_et.time()
    if wd == 5:  # Saturday: closed all day
        return False
    if wd == 6:  # Sunday: opens at 18:00 ET
        return t >= dtime(18, 0)
    if wd == 4:  # Friday: closes at 17:00 ET
        return t < dtime(17, 0)
    # Mon-Thu: open except 17:00-18:00 maintenance pause
    return not (dtime(17, 0) <= t < dtime(18, 0))


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
def next_interval_boundary(now: datetime, interval_minutes: int) -> datetime:
    """Return the next timestamp aligned to an interval boundary.

    Example: if now=10:07:23 and interval=5, returns 10:10:00.
    Boundaries are anchored to the top of the hour."""
    minutes_past = now.minute + now.second / 60.0 + now.microsecond / 60e6
    next_slot = int(minutes_past // interval_minutes + 1) * interval_minutes
    base = now.replace(second=0, microsecond=0, minute=0)
    return base + timedelta(minutes=next_slot)


# ---------------------------------------------------------------------------
# Watchlist (same format as batch.py)
# ---------------------------------------------------------------------------
def load_watchlist(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Watchlist not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    equities = data.get("equities") or []
    futures = data.get("futures") or []
    norm_eq, norm_fu = [], []
    for item in equities:
        if isinstance(item, dict) and item.get("symbol"):
            norm_eq.append({
                "symbol": item["symbol"].upper().strip(),
                "expirations": item.get("expirations", "under_60_dte"),
            })
    for item in futures:
        if isinstance(item, dict) and item.get("product"):
            norm_fu.append({
                "product": item["product"].upper().strip(),
                "expirations": item.get("expirations", "under_60_dte"),
            })
    return {"equities": norm_eq, "futures": norm_fu}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging() -> logging.Logger:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    today_dir = SNAPSHOT_DIR / date.today().isoformat()
    today_dir.mkdir(parents=True, exist_ok=True)
    log_path = today_dir / "_events.log"

    fmt = "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )
    return logging.getLogger("snapshot_loop")


# ---------------------------------------------------------------------------
# Parquet writer
# ---------------------------------------------------------------------------
def _arrow_schema() -> pa.Schema:
    """Same explicit schema as stream.py: string for IDs, float64 for
    every numeric field, so columns stay properly typed even when most
    values are missing."""
    fields = [
        pa.field("expiration",   pa.string()),
        pa.field("strike_price", pa.float64()),
        pa.field("call_symbol",  pa.string()),
        pa.field("put_symbol",   pa.string()),
    ]
    for side in ("call", "put"):
        for fld in ttc.STRIKE_FIELDS:
            fields.append(pa.field(f"{side}_{fld}", pa.float64()))
    return pa.schema(fields)


def _arrow_schema_underlying() -> pa.Schema:
    """Schema for the sibling underlying-quote Parquet file.

    One row per distinct underlying instrument referenced by the chain:
    - For an equity chain: exactly one row (the equity itself)
    - For a futures chain: one row per distinct futures contract
      referenced by chain expirations (typically 1-3)
    """
    return pa.schema([
        pa.field("symbol",          pa.string()),
        pa.field("instrument_type", pa.string()),
        pa.field("bid",             pa.float64()),
        pa.field("ask",             pa.float64()),
        pa.field("last",            pa.float64()),
        pa.field("mark",            pa.float64()),
        pa.field("bid_size",        pa.float64()),
        pa.field("ask_size",        pa.float64()),
        pa.field("day_low",         pa.float64()),
        pa.field("day_high",        pa.float64()),
        pa.field("prev_close",      pa.float64()),
    ])


def write_underlying_parquet(quote_rows: list[dict],
                              out_path: Path) -> int:
    """Write the underlying sibling Parquet file. Returns rows written."""
    if not quote_rows:
        return 0
    schema = _arrow_schema_underlying()
    table = pa.Table.from_pylist(quote_rows, schema=schema)
    pq.write_table(table, out_path, compression="snappy")
    return len(quote_rows)


def _clean_row(r: dict) -> dict:
    """Normalize a row to fit the arrow schema (NaN strings -> None,
    strikes as float)."""
    out = {
        "expiration":   r.get("expiration"),
        "strike_price": float(r.get("strike_price") or 0.0),
        "call_symbol":  r.get("call_symbol"),
        "put_symbol":   r.get("put_symbol"),
    }
    for side in ("call", "put"):
        for fld in ttc.STRIKE_FIELDS:
            v = r.get(f"{side}_{fld}")
            out[f"{side}_{fld}"] = ttc._normalize_numeric(v)
    return out


def write_parquet(rows: list[dict], out_path: Path,
                   schema: pa.Schema) -> int:
    if not rows:
        return 0
    cleaned = [_clean_row(r) for r in rows]
    table = pa.Table.from_pylist(cleaned, schema=schema)
    pq.write_table(table, out_path, compression="snappy")
    return len(cleaned)


# ---------------------------------------------------------------------------
# Per-symbol pulls (Parquet-only versions of batch.py logic)
# ---------------------------------------------------------------------------
def pull_equity(symbol: str, expirations_tokens,
                 out_dir: Path, ts: str,
                 log: logging.Logger) -> dict:
    """Pull one equity chain snapshot. Returns stats."""
    chain = ttc.fetch_equity_chain(symbol)
    items = chain.get("data", {}).get("items", [])
    flat_exps = []
    for item in items:
        flat_exps.extend(item.get("expirations", []))
    chosen_dates = set(ttc.select_expirations(flat_exps, expirations_tokens))
    if not chosen_dates:
        log.info(f"  {symbol}: no matching expirations")
        return {"symbol": symbol, "rows": 0, "skipped": True}

    rows: list[dict] = []
    tasty_symbols: list[str] = []
    strike_index: dict[str, tuple[str, dict]] = {}

    for item in items:
        for exp in item.get("expirations", []):
            ds = exp.get("expiration-date")
            if ds not in chosen_dates:
                continue
            for s in exp.get("strikes", []):
                call_sym = s.get("call")
                put_sym = s.get("put")
                row = ttc.empty_strike_row(
                    ds, s.get("strike-price", 0), call_sym, put_sym)
                rows.append(row)
                if call_sym:
                    tasty_symbols.append(call_sym)
                    strike_index[call_sym] = ("call", row)
                if put_sym:
                    tasty_symbols.append(put_sym)
                    strike_index[put_sym] = ("put", row)

    rest_quotes = ttc.fetch_rest_market_data(
        tasty_symbols, kind="equity-option")
    for sym, info in rest_quotes.items():
        if sym in strike_index:
            side, row = strike_index[sym]
            ttc.apply_rest(row, side, info)

    dx_symbols, dx_to_tasty = ttc.build_equity_streamer_symbols(tasty_symbols)

    # Scale stream collect window with subscription size
    collect = 8 + 0.5 * (len(dx_symbols) / 1000.0)
    quote_token, dxlink_url = ttc.fetch_quote_token()
    try:
        stream_results = asyncio.run(
            ttc.stream_market_events_once(
                dxlink_url, quote_token, dx_symbols, collect))
    except Exception as e:
        log.warning(f"  {symbol}: stream failed ({e}); using REST data only")
        stream_results = {}

    for dx_sym, payload in stream_results.items():
        tasty_sym = dx_to_tasty.get(dx_sym)
        if tasty_sym and tasty_sym in strike_index:
            side, row = strike_index[tasty_sym]
            ttc.apply_stream(row, side, payload)

    out_path = out_dir / f"{symbol}_full_chain_{ts}.parquet"
    n_written = write_parquet(rows, out_path, _arrow_schema())
    rows_w_delta = sum(1 for r in rows
                        if r.get("call_delta") is not None
                        or r.get("put_delta") is not None)
    rows_w_quote = sum(1 for r in rows
                        if r.get("call_bid") is not None
                        or r.get("put_bid") is not None)

    # Sibling file: the underlying ETF/stock bid/ask/last at snapshot time.
    # Pulled separately because it's a different instrument kind and a
    # tiny payload. Failure here doesn't invalidate the chain snapshot.
    u_path = out_dir / f"{symbol}_underlying_{ts}.parquet"
    u_rows: list[dict] = []
    try:
        u_quotes = ttc.fetch_rest_market_data([symbol], kind="equity")
        if symbol in u_quotes:
            u_rows.append(ttc.underlying_row_from_quote(
                u_quotes[symbol], instrument_type="equity"))
            write_underlying_parquet(u_rows, u_path)
    except Exception as e:
        log.warning(f"  {symbol}: underlying fetch failed ({e}); "
                    f"chain snapshot still written")

    u_status = "ok" if u_rows else "MISSING"
    log.info(f"  {symbol}: rows={n_written} w_quote={rows_w_quote} "
             f"w_delta={rows_w_delta} underlying={u_status} "
             f"-> {out_path.name}")
    return {"symbol": symbol, "rows": n_written,
            "rows_w_quote": rows_w_quote, "rows_w_delta": rows_w_delta,
            "underlying_ok": bool(u_rows)}


def pull_futures(product: str, expirations_tokens,
                  out_dir: Path, ts: str,
                  log: logging.Logger) -> dict:
    """Pull one futures-options chain snapshot. Returns stats."""
    chain = ttc.fetch_futures_chain(product)
    flat_exps = []
    for c in chain.get("data", {}).get("option-chains", []):
        flat_exps.extend(c.get("expirations", []))
    chosen_dates = set(ttc.select_expirations(flat_exps, expirations_tokens))
    if not chosen_dates:
        log.info(f"  {product}: no matching expirations")
        return {"symbol": product, "rows": 0, "skipped": True}

    rows: list[dict] = []
    tasty_symbols: list[str] = []
    strike_index: dict[str, tuple[str, dict]] = {}
    tasty_to_streamer: dict[str, str] = {}
    # Distinct underlying futures contract symbols referenced by this chain.
    # ES options near expiry reference ESM6; later options reference ESU6,
    # etc. We capture quotes for every distinct contract so IV can be
    # computed against the correct underlying for each expiration.
    underlying_contracts: set[str] = set()

    for c in chain.get("data", {}).get("option-chains", []):
        # The 'underlying-symbol' on the chain object itself, if present,
        # is a chain-wide fallback (e.g., '/ES').
        for exp in c.get("expirations", []):
            ds = exp.get("expiration-date") or exp.get("expiration_date")
            if ds not in chosen_dates:
                continue
            # Each expiration references a specific futures contract.
            u_sym = (exp.get("underlying-symbol")
                      or exp.get("underlying_symbol"))
            if u_sym:
                underlying_contracts.add(u_sym)
            for s in exp.get("strikes", []):
                call_sym = s.get("call")
                put_sym = s.get("put")
                call_stream = s.get("call-streamer-symbol")
                put_stream = s.get("put-streamer-symbol")
                row = ttc.empty_strike_row(
                    ds, s.get("strike-price", 0), call_sym, put_sym)
                rows.append(row)
                if call_sym:
                    tasty_symbols.append(call_sym)
                    strike_index[call_sym] = ("call", row)
                    if call_stream:
                        tasty_to_streamer[call_sym] = call_stream
                if put_sym:
                    tasty_symbols.append(put_sym)
                    strike_index[put_sym] = ("put", row)
                    if put_stream:
                        tasty_to_streamer[put_sym] = put_stream

    rest_quotes = ttc.fetch_rest_market_data(
        tasty_symbols, kind="future-option")
    for sym, info in rest_quotes.items():
        if sym in strike_index:
            side, row = strike_index[sym]
            ttc.apply_rest(row, side, info)
            ss = info.get("streamer-symbol")
            if ss and sym not in tasty_to_streamer:
                tasty_to_streamer[sym] = ss

    missing = [s for s in tasty_symbols if s not in tasty_to_streamer]
    if missing:
        found = ttc.lookup_futures_streamer_symbols(missing)
        tasty_to_streamer.update(found)

    streamer_to_tasty = {v: k for k, v in tasty_to_streamer.items()}
    dx_symbols = list(streamer_to_tasty.keys())

    collect = 12 + 0.5 * (len(dx_symbols) / 1000.0)
    quote_token, dxlink_url = ttc.fetch_quote_token()
    try:
        stream_results = asyncio.run(
            ttc.stream_market_events_once(
                dxlink_url, quote_token, dx_symbols, collect))
    except Exception as e:
        log.warning(f"  {product}: stream failed ({e}); using REST data only")
        stream_results = {}

    for dx_sym, payload in stream_results.items():
        tasty_sym = streamer_to_tasty.get(dx_sym)
        if tasty_sym and tasty_sym in strike_index:
            side, row = strike_index[tasty_sym]
            ttc.apply_stream(row, side, payload)

    out_path = out_dir / f"{product}_futures_options_{ts}.parquet"
    n_written = write_parquet(rows, out_path, _arrow_schema())
    rows_w_delta = sum(1 for r in rows
                        if r.get("call_delta") is not None
                        or r.get("put_delta") is not None)
    rows_w_quote = sum(1 for r in rows
                        if r.get("call_bid") is not None
                        or r.get("put_bid") is not None)

    # Sibling file: one row per distinct futures contract referenced by
    # the chain. /market-data/by-type accepts the contract symbols (e.g.,
    # '/ESM6', '/NQU6') under kind=future.
    u_path = out_dir / f"{product}_underlyings_{ts}.parquet"
    u_rows: list[dict] = []
    if underlying_contracts:
        try:
            u_quotes = ttc.fetch_rest_market_data(
                sorted(underlying_contracts), kind="future")
            for sym, info in u_quotes.items():
                u_rows.append(ttc.underlying_row_from_quote(
                    info, instrument_type="future"))
            if u_rows:
                write_underlying_parquet(u_rows, u_path)
        except Exception as e:
            log.warning(f"  {product}: underlyings fetch failed ({e}); "
                        f"chain snapshot still written")
    else:
        log.warning(f"  {product}: chain did not reference any underlying "
                    f"futures contracts (this is unexpected)")

    u_status = (f"{len(u_rows)}/{len(underlying_contracts)} contracts"
                if u_rows else "MISSING")
    log.info(f"  {product}: rows={n_written} w_quote={rows_w_quote} "
             f"w_delta={rows_w_delta} underlying={u_status} "
             f"-> {out_path.name}")
    return {"symbol": product, "rows": n_written,
            "rows_w_quote": rows_w_quote, "rows_w_delta": rows_w_delta,
            "underlying_count": len(u_rows),
            "underlying_expected": len(underlying_contracts)}


# ---------------------------------------------------------------------------
# One snapshot cycle
# ---------------------------------------------------------------------------
def do_snapshot(watchlist: dict, log: logging.Logger,
                 force: bool = False) -> None:
    """Run one full pass.

    If ``force`` is True (used by --once), skip the market-hours check
    and snapshot both equity and futures regardless. Otherwise pull each
    asset class only when its session is active. Each underlying is
    wrapped in try/except so one failure doesn't kill the rest.
    """
    now_et = datetime.now(ET)
    eq_open = force or equity_market_open(now_et)
    fu_open = force or futures_market_open(now_et)

    today = date.today()
    out_dir = SNAPSHOT_DIR / today.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")

    log.info(f"--- Snapshot at {ts} (ET {now_et.strftime('%H:%M:%S %a')}) "
             f"equity_session={eq_open} futures_session={fu_open} ---")

    if not eq_open and not fu_open:
        log.info("  No active session for either equity or futures; skipping.")
        return

    # Fresh auth per snapshot: forces a new OAuth grant each cycle, which
    # avoids the access-token-expiry issues that plagued long-running
    # processes. The refresh token is rotated and persisted automatically.
    try:
        ttc.get_access_token(force=True)
    except Exception as e:
        log.error(f"  AUTH FAILED: {e}")
        return

    if eq_open:
        for e in watchlist["equities"]:
            try:
                pull_equity(e["symbol"], e["expirations"], out_dir, ts, log)
            except Exception as exc:
                log.error(f"  ERROR pulling equity {e['symbol']}: {exc}")
                log.debug(traceback.format_exc())

    if fu_open:
        for f in watchlist["futures"]:
            try:
                pull_futures(f["product"], f["expirations"], out_dir, ts, log)
            except Exception as exc:
                log.error(f"  ERROR pulling futures {f['product']}: {exc}")
                log.debug(traceback.format_exc())


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
_shutdown = False

def _request_shutdown(*_a):
    global _shutdown
    _shutdown = True


def run_loop(watchlist: dict, log: logging.Logger) -> None:
    log.info(f"Starting snapshot loop: interval={INTERVAL_MINUTES}min")
    log.info(f"  watchlist: {len(watchlist['equities'])} equities, "
             f"{len(watchlist['futures'])} futures")

    signal.signal(signal.SIGINT, _request_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _request_shutdown)

    while not _shutdown:
        try:
            do_snapshot(watchlist, log)
        except Exception as e:
            log.exception(f"Snapshot crashed: {e}")

        if _shutdown:
            break

        now = datetime.now()
        next_run = next_interval_boundary(now, INTERVAL_MINUTES)
        sleep_s = (next_run - now).total_seconds()
        # If the snapshot took longer than the interval, skip to the
        # next clean boundary so we stay aligned.
        if sleep_s < 1:
            next_run = next_interval_boundary(
                now + timedelta(minutes=INTERVAL_MINUTES), INTERVAL_MINUTES)
            sleep_s = (next_run - now).total_seconds()
        log.info(f"  Next snapshot at {next_run.strftime('%H:%M:%S')} "
                 f"(in {sleep_s:.0f}s)")
        # Sleep in short increments so Ctrl-C is responsive.
        end_sleep = time.monotonic() + sleep_s
        while not _shutdown and time.monotonic() < end_sleep:
            time.sleep(min(1.0, end_sleep - time.monotonic()))

    log.info("Snapshot loop exited cleanly.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(
        description="Periodic Tastytrade option-chain snapshot downloader")
    p.add_argument("--watchlist", default="watchlist.yaml",
                   help="Path to watchlist YAML (default: watchlist.yaml)")
    p.add_argument("--once", action="store_true",
                   help="Run a single snapshot right now, ignoring "
                        "market hours, then exit. Useful for testing.")
    args = p.parse_args()

    ttc.ensure_dirs()
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    log = setup_logging()

    wl = load_watchlist(Path(args.watchlist))
    if not wl["equities"] and not wl["futures"]:
        log.error("Watchlist is empty.")
        return 1

    if args.once:
        log.info("--once flag set: running a single snapshot, ignoring "
                 "market hours")
        do_snapshot(wl, log, force=True)
        return 0

    try:
        run_loop(wl, log)
    except KeyboardInterrupt:
        log.info("Interrupted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
