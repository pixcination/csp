"""
tastytrade_common.py
====================
Shared helpers for the Tastytrade option-data tools:

    batch.py    - YAML-driven multi-symbol snapshot downloader
    stream.py   - Long-lived DXLink streaming feed with Parquet snapshots
    download_v2.py - Drop-in replacement for the original download.py

What lives here:
    * TLS-renegotiation-tolerant requests.Session
    * OAuth refresh-token grant WITH ROTATION PERSISTENCE
      (this is the fix for "fails after a few runs in the same shell")
    * Chain-structure loaders for equity and futures options
    * Equity OCC -> dxFeed streamer-symbol conversion
    * REST market-data fetcher (bid/ask/mark/last)
    * DXLink connection setup + subscription helpers
    * Shared strike-row schema and apply functions

Environment:
    .env file with:
        CLIENT_SECRET=...
        REFRESH_TOKEN=...

    REFRESH_TOKEN may be rewritten by this module when Tastytrade
    rotates it. Keep .env writable.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import threading
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any

import requests
import websockets
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.util.ssl_ import create_urllib3_context

load_dotenv()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = "https://api.tastyworks.com"
def _default_data_dir() -> Path:
    """Choose a sensible data directory per OS.

    Override with the TASTYTRADE_DATA_DIR environment variable if you
    want data somewhere else (e.g., an external drive). The directory
    is created on first use.

    Defaults:
      - Windows: D:\\tastytrade  (preserves prior behavior)
      - macOS / Linux: ~/tastytrade/data  (alongside the code if you put
        the project at ~/tastytrade/, kept separate otherwise)
    """
    override = os.getenv("TASTYTRADE_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":  # Windows
        return Path(r"D:\tastytrade")
    return Path.home() / "tastytrade" / "data"


BASE_SAVE_DIR = _default_data_dir()
STREAM_SAVE_DIR = BASE_SAVE_DIR / "stream"

CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN")

ENV_PATH = Path(".env")

STREAMER_BATCH_SIZE = 200


# ---------------------------------------------------------------------------
# TLS-tolerant session
# ---------------------------------------------------------------------------
# Tastytrade's nginx edge initiates TLS renegotiation mid-session, which
# Python's OpenSSL 3.x rejects by default. Allow legacy renegotiation
# and bolt on a Retry policy for transient SSL/conn errors.
_OP_LEGACY_SERVER_CONNECT             = 0x4
_OP_ALLOW_UNSAFE_LEGACY_RENEGOTIATION = 0x40000


def _make_renegotiation_tolerant_ssl_context():
    ctx = create_urllib3_context()
    ctx.options |= _OP_LEGACY_SERVER_CONNECT
    ctx.options |= _OP_ALLOW_UNSAFE_LEGACY_RENEGOTIATION
    return ctx


class _TLSRenegotiationAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = _make_renegotiation_tolerant_ssl_context()
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = _make_renegotiation_tolerant_ssl_context()
        return super().proxy_manager_for(*args, **kwargs)


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4, connect=4, read=2, backoff_factor=0.8,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = _TLSRenegotiationAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def make_dxlink_ssl_context() -> ssl.SSLContext:
    """SSL context for websockets.connect(). Same renegotiation tolerance."""
    ctx = ssl.create_default_context()
    ctx.options |= _OP_LEGACY_SERVER_CONNECT
    ctx.options |= _OP_ALLOW_UNSAFE_LEGACY_RENEGOTIATION
    return ctx


SESSION = make_session()


# ---------------------------------------------------------------------------
# OAuth with refresh-token rotation persistence
# ---------------------------------------------------------------------------
# This is the fix for "the script fails after a couple of runs in the same
# PowerShell window." Tastytrade rotates refresh_token on use; if you don't
# write the new one back to .env, the stored token goes stale and grants
# start failing. A fresh shell only "fixes" it incidentally (time passes,
# you regenerate elsewhere, etc.).
_TOKEN_LOCK = threading.Lock()
_ACCESS_TOKEN: str | None = None
_REFRESH_TOKEN_CURRENT: str | None = REFRESH_TOKEN


def _persist_refresh_token(new_rt: str) -> None:
    """Rewrite REFRESH_TOKEN=... in .env, preserving everything else."""
    global _REFRESH_TOKEN_CURRENT
    _REFRESH_TOKEN_CURRENT = new_rt
    try:
        if ENV_PATH.exists():
            lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
        else:
            lines = []
        out, found = [], False
        for line in lines:
            if line.startswith("REFRESH_TOKEN="):
                out.append(f"REFRESH_TOKEN={new_rt}")
                found = True
            else:
                out.append(line)
        if not found:
            out.append(f"REFRESH_TOKEN={new_rt}")
        ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"  [auth] rotated refresh token persisted to {ENV_PATH.resolve()}")
    except Exception as e:
        # Don't crash the run if we can't write the file; just warn loudly.
        print(f"  [auth] WARN: could not persist rotated refresh token: {e}")
        print(f"  [auth] WARN: copy this value into .env manually if you "
              f"want subsequent runs to keep working:\n    REFRESH_TOKEN={new_rt}")


def get_access_token(force: bool = False) -> str:
    """
    Fetch a Tastytrade access token using the refresh_token grant.
    Cached in-memory for the lifetime of the process.

    If Tastytrade returns a new refresh_token (rotation), persist it
    back to .env immediately so future invocations don't break.
    """
    global _ACCESS_TOKEN, _REFRESH_TOKEN_CURRENT
    with _TOKEN_LOCK:
        if _ACCESS_TOKEN and not force:
            return _ACCESS_TOKEN

        rt = _REFRESH_TOKEN_CURRENT or REFRESH_TOKEN
        if not rt:
            raise RuntimeError(
                "No REFRESH_TOKEN found. Set REFRESH_TOKEN in .env "
                "(and CLIENT_SECRET).")
        if not CLIENT_SECRET:
            raise RuntimeError("No CLIENT_SECRET found in .env.")

        resp = SESSION.post(
            f"{BASE_URL}/oauth/token",
            json={
                "grant_type": "refresh_token",
                "refresh_token": rt,
                "client_secret": CLIENT_SECRET,
            },
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"OAuth refresh failed: HTTP {resp.status_code} "
                f"{resp.text[:300]}")
        payload = resp.json()
        _ACCESS_TOKEN = payload["access_token"]

        new_rt = payload.get("refresh_token")
        if new_rt and new_rt != rt:
            _persist_refresh_token(new_rt)

        return _ACCESS_TOKEN


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {get_access_token()}",
            "Accept": "application/json"}


def fetch_quote_token() -> tuple[str, str]:
    """Get DXLink streamer credentials. Token is valid for 24h."""
    r = _authed_get(f"{BASE_URL}/api-quote-tokens")
    r.raise_for_status()
    d = r.json()["data"]
    return d["token"], d["dxlink-url"]


def _authed_get(url: str, params=None, timeout: int = 30):
    """
    Authenticated GET with auto-refresh on 401. The cached access token
    expires after ~15 minutes; long-running loops (like the snapshot loop)
    must handle that. On a 401 we force a token refresh and retry once.
    """
    r = SESSION.get(url, headers=auth_headers(), params=params,
                     timeout=timeout)
    if r.status_code == 401:
        # Stale access token; force a refresh and retry once.
        get_access_token(force=True)
        r = SESSION.get(url, headers=auth_headers(), params=params,
                         timeout=timeout)
    return r


# ---------------------------------------------------------------------------
# Chain loaders
# ---------------------------------------------------------------------------
def fetch_equity_chain(symbol: str) -> dict:
    r = _authed_get(f"{BASE_URL}/option-chains/{symbol}/nested")
    r.raise_for_status()
    return r.json()


def fetch_futures_chain(product_code: str) -> dict:
    r = _authed_get(f"{BASE_URL}/futures-option-chains/{product_code}/nested")
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Expiration selection (convenience tokens)
# ---------------------------------------------------------------------------
# Supported in YAML watchlists:
#   - "all"                  every expiration in the chain
#   - "YYYY-MM-DD"           one specific date
#   - "next_monthly"         next standard monthly (3rd Friday of a month)
#   - "next_weekly"          next non-monthly weekly
#   - "0dte"                 today's expiration if present
#   - "under_45_dte"         everything with DTE <= 45 (any integer works)
# A list mixes these freely, e.g. ["next_monthly", "next_weekly", "0dte"].

def _is_third_friday(d: date) -> bool:
    return d.weekday() == 4 and 15 <= d.day <= 21


def select_expirations(all_expirations: list[dict], tokens) -> list[str]:
    """
    all_expirations: list of dicts from the chain response, each with
        'expiration-date' (YYYY-MM-DD) and 'days-to-expiration'.
    tokens: str or list[str] of selection tokens.

    Returns deduped, sorted list of YYYY-MM-DD date strings.
    """
    if tokens is None:
        return []
    if isinstance(tokens, str):
        tokens = [tokens]

    by_date: dict[str, dict] = {}
    for exp in all_expirations:
        ds = exp.get("expiration-date") or exp.get("expiration_date")
        if ds:
            by_date[ds] = exp
    sorted_dates = sorted(by_date.keys())
    today = datetime.now().date()
    chosen: set[str] = set()

    for tok in tokens:
        tok = str(tok).strip().lower()

        if tok == "all":
            chosen.update(sorted_dates)
            continue

        if tok == "0dte":
            ds = today.isoformat()
            if ds in by_date:
                chosen.add(ds)
            continue

        if tok == "next_monthly":
            for ds in sorted_dates:
                d = date.fromisoformat(ds)
                if d >= today and _is_third_friday(d):
                    chosen.add(ds)
                    break
            continue

        if tok == "next_weekly":
            for ds in sorted_dates:
                d = date.fromisoformat(ds)
                if d >= today and not _is_third_friday(d):
                    chosen.add(ds)
                    break
            continue

        if tok.startswith("under_") and tok.endswith("_dte"):
            try:
                n = int(tok[len("under_"):-len("_dte")])
            except ValueError:
                print(f"  [exp] WARN: bad token {tok!r}, skipping")
                continue
            for ds in sorted_dates:
                d = date.fromisoformat(ds)
                if 0 <= (d - today).days <= n:
                    chosen.add(ds)
            continue

        # "between_M_N_dte" -- inclusive DTE range, e.g. "between_5_14_dte"
        # for wheel-strategy screening (5-14 DTE weekly/biweekly targets).
        # Added for Stage 3 chain liquidity scanning: unlike under_N_dte,
        # this excludes very-near-term expirations (0-4 DTE) that aren't
        # relevant to the target trade window, keeping subscription counts
        # (and therefore dxFeed rate-limit exposure) as small as possible
        # per symbol during a broad multi-symbol scan.
        if tok.startswith("between_") and tok.endswith("_dte"):
            try:
                lo_str, hi_str = tok[len("between_"):-len("_dte")].split("_")
                lo, hi = int(lo_str), int(hi_str)
            except ValueError:
                print(f"  [exp] WARN: bad token {tok!r}, skipping")
                continue
            for ds in sorted_dates:
                d = date.fromisoformat(ds)
                if lo <= (d - today).days <= hi:
                    chosen.add(ds)
            continue

        # Literal YYYY-MM-DD
        try:
            datetime.strptime(tok, "%Y-%m-%d")
            if tok in by_date:
                chosen.add(tok)
            else:
                print(f"  [exp] WARN: {tok} not in chain, skipping")
        except ValueError:
            print(f"  [exp] WARN: unrecognized expiration token {tok!r}, skipping")

    return sorted(chosen)


# ---------------------------------------------------------------------------
# Symbol conversion: equity OCC -> dxFeed streamer
# ---------------------------------------------------------------------------
def build_equity_streamer_symbols(option_symbols: list[str]):
    """
    'SPY   260522C00680000' -> '.SPY260522C680'
    Returns (dx_symbols, dx_to_tasty_dict)
    """
    out, sym_map = [], {}
    for s in option_symbols:
        try:
            parts = s.split(None, 1)
            if len(parts) != 2:
                continue
            root, rest = parts[0], parts[1]
            yymmdd = rest[:6]
            cp = rest[6]
            strike_int = int(rest[7:])
            strike = strike_int / 1000.0
            strike_str = f"{strike:g}"
            dx = f".{root}{yymmdd}{cp}{strike_str}"
            out.append(dx)
            sym_map[dx] = s
        except Exception as e:
            print(f"  WARN: couldn't convert symbol '{s}': {e}")
    return out, sym_map


# ---------------------------------------------------------------------------
# REST market data
# ---------------------------------------------------------------------------
def fetch_rest_market_data(symbols: list[str],
                            kind: str,
                            chunk_size: int = 90) -> dict:
    """
    Fetch market data for any instrument kind from /market-data/by-type.

    kind values supported by Tastytrade:
        "equity-option"   options on equities/ETFs
        "future-option"   options on futures
        "equity"          equities and ETFs (SPY, QQQ, AAPL, ...)
        "future"          futures contracts (/ESM6, /NQU6, ...)
        "cryptocurrency", "index"  also supported but unused here

    Returns dict[symbol] -> raw market-data item with fields like
    bid, ask, last, mark, bid-size, ask-size, etc.
    """
    quotes: dict[str, dict] = {}
    if not symbols:
        return quotes
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i + chunk_size]
        params = {kind: ",".join(chunk)}
        r = _authed_get(f"{BASE_URL}/market-data/by-type", params=params)
        if r.status_code != 200:
            print(f"  WARN: market-data/by-type {kind} returned "
                  f"{r.status_code} for batch {i}: {r.text[:200]}")
            continue
        body = r.json().get("data", {})
        items = body.get("items") if isinstance(body, dict) else body
        for it in items or []:
            sym = it.get("symbol")
            if sym:
                quotes[sym] = it
    return quotes


def fetch_underlying_quotes(equity_symbols: list[str],
                             futures_symbols: list[str]) -> dict[str, dict]:
    """Convenience wrapper: fetch equity + futures underlying quotes in a
    single dict keyed by symbol. Used by snapshot_loop to capture the
    contemporaneous underlying state alongside each chain snapshot."""
    out: dict[str, dict] = {}
    if equity_symbols:
        out.update(fetch_rest_market_data(equity_symbols, kind="equity"))
    if futures_symbols:
        out.update(fetch_rest_market_data(futures_symbols, kind="future"))
    return out


# ---------------------------------------------------------------------------
# Underlying row schema for sibling Parquet files
# ---------------------------------------------------------------------------
# Stored as a sibling file per snapshot (e.g., SPY_underlying_HHMMSS.parquet)
# rather than denormalized into every option row.
UNDERLYING_FIELDS = ("symbol", "instrument_type", "bid", "ask", "last",
                     "mark", "bid_size", "ask_size", "day_low", "day_high",
                     "prev_close")


def underlying_row_from_quote(quote: dict, instrument_type: str) -> dict:
    """Build a single row from a /market-data/by-type response item.

    instrument_type: 'equity' or 'future', stored verbatim in the row so
    downstream code can tell them apart on read.
    """
    bid  = _normalize_numeric(quote.get("bid"))
    ask  = _normalize_numeric(quote.get("ask"))
    last = _normalize_numeric(quote.get("last"))
    mark = _normalize_numeric(quote.get("mark"))
    # Compute mark from bid/ask if missing and both sides present.
    # Must use normalized values - raw quote may have 'NaN' strings.
    if mark is None and bid is not None and ask is not None:
        mark = (bid + ask) / 2.0
    return {
        "symbol":          quote.get("symbol"),
        "instrument_type": instrument_type,
        "bid":             bid,
        "ask":             ask,
        "last":            last,
        "mark":            mark,
        "bid_size":        _normalize_numeric(quote.get("bid-size")),
        "ask_size":        _normalize_numeric(quote.get("ask-size")),
        "day_low":         _normalize_numeric(quote.get("day-low-price")),
        "day_high":        _normalize_numeric(quote.get("day-high-price")),
        "prev_close":      _normalize_numeric(quote.get("prev-close")),
    }


def lookup_futures_streamer_symbols(missing: list[str]) -> dict[str, str]:
    """For futures option symbols missing streamer-symbol, look them up via
    /instruments/future-options. Returns tasty_symbol -> streamer_symbol."""
    found: dict[str, str] = {}
    if not missing:
        return found
    BATCH = 100
    for i in range(0, len(missing), BATCH):
        chunk = missing[i:i + BATCH]
        params = [("symbol[]", s) for s in chunk]
        r = _authed_get(f"{BASE_URL}/instruments/future-options",
                         params=params)
        if r.status_code != 200:
            print(f"    WARN: {r.status_code} on instruments lookup: "
                  f"{r.text[:200]}")
            continue
        for inst in r.json().get("data", {}).get("items", []):
            tsy = inst.get("symbol")
            ssy = inst.get("streamer-symbol")
            if tsy and ssy:
                found[tsy] = ssy
    return found


# ---------------------------------------------------------------------------
# Strike row schema (shared between equity and futures)
# ---------------------------------------------------------------------------
STRIKE_FIELDS = ("last", "bid", "ask", "mark", "volume", "open_interest",
                 "iv", "delta", "gamma", "theta", "vega", "rho")


def empty_strike_row(exp_date: str, strike_price, call_sym, put_sym) -> dict:
    row = {
        "expiration": exp_date,
        "strike_price": float(strike_price) if strike_price is not None else 0.0,
        "call_symbol": call_sym,
        "put_symbol": put_sym,
    }
    for side in ("call", "put"):
        for fld in STRIKE_FIELDS:
            row[f"{side}_{fld}"] = None
    return row


def _normalize_numeric(value):
    """dxFeed sends missing numeric values as the literal string 'NaN'
    (and rarely as 'Infinity'). These poison parquet writes because
    pyarrow ends up with mixed float/string columns. Convert any such
    sentinel to None so columns stay typed as float64."""
    if value is None:
        return None
    if isinstance(value, str):
        # Trim and check common dxFeed sentinels
        s = value.strip()
        if s in ("NaN", "nan", "Infinity", "-Infinity", "inf", "-inf", ""):
            return None
        # Otherwise try to coerce to float; if it's some other string,
        # drop it rather than poisoning the column.
        try:
            return float(s)
        except ValueError:
            return None
    return value


def apply_stream(row: dict, side: str, payload: dict) -> None:
    for src in STRIKE_FIELDS:
        if src == "mark":
            continue  # mark is REST-only
        if src in payload:
            v = _normalize_numeric(payload[src])
            if v is not None:
                row[f"{side}_{src}"] = v


def apply_rest(row: dict, side: str, info: dict) -> None:
    """Override of REST application that also normalizes 'NaN' strings."""
    row[f"{side}_bid"]  = _normalize_numeric(info.get("bid"))
    row[f"{side}_ask"]  = _normalize_numeric(info.get("ask"))
    row[f"{side}_last"] = _normalize_numeric(info.get("last"))
    row[f"{side}_mark"] = _normalize_numeric(info.get("mark"))


# ---------------------------------------------------------------------------
# DXLink protocol layer
# ---------------------------------------------------------------------------
FIELD_LAYOUTS = {
    "Quote":   ["eventType", "eventSymbol", "bidPrice", "askPrice",
                "bidSize", "askSize"],
    "Trade":   ["eventType", "eventSymbol", "price", "size", "dayVolume"],
    "Summary": ["eventType", "eventSymbol", "openInterest",
                "prevDayClosePrice", "dayOpenPrice", "dayHighPrice",
                "dayLowPrice"],
    "Greeks":  ["eventType", "eventSymbol", "volatility", "delta", "gamma",
                "theta", "vega", "rho", "price"],
}


async def _wait_for(ws, predicate, timeout: float = 10.0,
                     drain_into: list | None = None):
    """
    Read frames until predicate(msg) returns True, or until timeout.
    Frames that don't match are dropped (or appended to drain_into if
    you want to inspect them later). Returns the matching message.
    Raises RuntimeError on timeout or if the server sends an ERROR.
    """
    end = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = end - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise RuntimeError(
                f"DXLink handshake timed out waiting for expected frame")
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"DXLink handshake timed out waiting for expected frame")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if msg.get("type") == "ERROR":
            raise RuntimeError(
                f"DXLink ERROR during handshake: {msg.get('error')} "
                f"{msg.get('message')}")
        if drain_into is not None:
            drain_into.append(msg)
        if predicate(msg):
            return msg


async def dxlink_handshake(ws, quote_token: str,
                            aggregation_period: float = 0.1) -> None:
    """
    SETUP -> wait SETUP echo -> AUTH -> wait AUTH_STATE: AUTHORIZED ->
    CHANNEL_REQUEST -> wait CHANNEL_OPENED -> FEED_SETUP -> wait FEED_CONFIG.

    Waiting for each ack is critical. dxFeed's server rejects later
    frames with BAD_ACTION ("AUTH step missing", "Channel with id 3 not
    exists") if the previous step hasn't been processed yet. Firing
    them back-to-back works sometimes by luck of network timing, but
    fails routinely under any load.
    """
    # SETUP
    await ws.send(json.dumps({
        "type": "SETUP", "channel": 0,
        "version": "0.1-DXF-JS/0.3.0",
        "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60,
    }))
    await _wait_for(ws, lambda m: m.get("type") == "SETUP"
                                   and m.get("channel") == 0)

    # AUTH (server first replies with AUTH_STATE: UNAUTHORIZED on connect;
    # we want the AUTHORIZED that follows our AUTH send)
    await ws.send(json.dumps({
        "type": "AUTH", "channel": 0, "token": quote_token,
    }))
    await _wait_for(
        ws,
        lambda m: m.get("type") == "AUTH_STATE"
                   and m.get("state") == "AUTHORIZED")

    # CHANNEL_REQUEST
    await ws.send(json.dumps({
        "type": "CHANNEL_REQUEST", "channel": 3,
        "service": "FEED", "parameters": {"contract": "AUTO"},
    }))
    await _wait_for(
        ws,
        lambda m: m.get("type") == "CHANNEL_OPENED"
                   and m.get("channel") == 3)

    # FEED_SETUP
    await ws.send(json.dumps({
        "type": "FEED_SETUP", "channel": 3,
        "acceptAggregationPeriod": aggregation_period,
        "acceptDataFormat": "COMPACT",
        "acceptEventFields": FIELD_LAYOUTS,
    }))
    await _wait_for(
        ws,
        lambda m: m.get("type") == "FEED_CONFIG"
                   and m.get("channel") == 3)


async def dxlink_subscribe(ws, dx_symbols: list[str],
                            reset: bool = True,
                            batch_size: int = STREAMER_BATCH_SIZE,
                            inter_batch_sleep: float = 0.05) -> None:
    """Subscribe to Quote/Trade/Summary/Greeks for every symbol, in batches.
    Yields briefly between batches so the server can process them in order
    (otherwise 30+ batches fired back-to-back can saturate its input)."""
    for i in range(0, len(dx_symbols), batch_size):
        batch = dx_symbols[i:i + batch_size]
        add_list = []
        for sym in batch:
            add_list += [
                {"type": "Quote",   "symbol": sym},
                {"type": "Trade",   "symbol": sym},
                {"type": "Summary", "symbol": sym},
                {"type": "Greeks",  "symbol": sym},
            ]
        await ws.send(json.dumps({
            "type": "FEED_SUBSCRIPTION", "channel": 3,
            "reset": (reset and i == 0),
            "add": add_list,
        }))
        if inter_batch_sleep and i + batch_size < len(dx_symbols):
            await asyncio.sleep(inter_batch_sleep)


def parse_feed_data(msg: dict) -> list[tuple[str, dict]]:
    """
    Parse a FEED_DATA message into a list of (eventSymbol, partial_payload)
    where partial_payload has fields appropriate to the event type:
        Quote   -> {bid, ask}
        Trade   -> {last, volume}
        Summary -> {open_interest, prev_close, day_high, day_low}
        Greeks  -> {iv, delta, gamma, theta, vega, rho}
    """
    out: list[tuple[str, dict]] = []
    if msg.get("type") != "FEED_DATA":
        return out
    data = msg.get("data", [])
    idx = 0
    while idx < len(data) - 1:
        event_type = data[idx]
        values = data[idx + 1]
        idx += 2
        fields = FIELD_LAYOUTS.get(event_type)
        if not fields or not isinstance(values, list):
            continue
        n = len(fields)
        for j in range(0, len(values), n):
            rec = values[j:j + n]
            if len(rec) < n:
                continue
            record = dict(zip(fields, rec))
            sym = record.get("eventSymbol")
            if not sym:
                continue
            payload: dict[str, Any] = {}
            if event_type == "Quote":
                payload["bid"] = record.get("bidPrice")
                payload["ask"] = record.get("askPrice")
            elif event_type == "Trade":
                payload["last"]   = record.get("price")
                payload["volume"] = record.get("dayVolume")
            elif event_type == "Summary":
                payload["open_interest"] = record.get("openInterest")
                payload["prev_close"]    = record.get("prevDayClosePrice")
                payload["day_high"]      = record.get("dayHighPrice")
                payload["day_low"]       = record.get("dayLowPrice")
            elif event_type == "Greeks":
                payload["iv"]    = record.get("volatility")
                payload["delta"] = record.get("delta")
                payload["gamma"] = record.get("gamma")
                payload["theta"] = record.get("theta")
                payload["vega"]  = record.get("vega")
                payload["rho"]   = record.get("rho")
            if payload:
                out.append((sym, payload))
    return out


async def stream_market_events_once(dxlink_url: str, quote_token: str,
                                     dx_symbols: list[str],
                                     collect_seconds: float) -> dict:
    """
    One-shot collector (for batch.py). Connects, subscribes, collects
    events for collect_seconds, returns dict[streamer_symbol] -> payload.
    """
    results: dict[str, dict] = {}
    if not dx_symbols:
        return results
    ssl_ctx = make_dxlink_ssl_context()
    async with websockets.connect(dxlink_url, ssl=ssl_ctx,
                                   max_size=2**24) as ws:
        await dxlink_handshake(ws, quote_token)
        await dxlink_subscribe(ws, dx_symbols, reset=True)

        end = asyncio.get_event_loop().time() + collect_seconds

        async def keepalive():
            while True:
                await asyncio.sleep(20)
                try:
                    await ws.send(json.dumps(
                        {"type": "KEEPALIVE", "channel": 0}))
                except Exception:
                    return

        ka = asyncio.create_task(keepalive())
        try:
            while asyncio.get_event_loop().time() < end:
                remaining = end - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                msg = json.loads(raw)
                for sym, payload in parse_feed_data(msg):
                    bucket = results.setdefault(sym, {})
                    bucket.update(payload)
        finally:
            ka.cancel()
    return results


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------
def ensure_dirs() -> None:
    BASE_SAVE_DIR.mkdir(parents=True, exist_ok=True)
    STREAM_SAVE_DIR.mkdir(parents=True, exist_ok=True)


def ts_compact() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")
