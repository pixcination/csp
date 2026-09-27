"""
IV rank and percentile, built from your own accumulated chain captures.

REWIRED IN PHASE 5
------------------
This module read `data/stage3_chains/<date>/`, which is where the legacy
`scripts/04` wrote. Phase 2's capture writes to `data/chains/<session_block>/`
instead -- so every snapshot the new pipeline took was invisible here, and the
component that has been neutral-filled at 0.5 since the beginning would have
stayed that way forever no matter how many times you pressed the button.

It now reads both:

    data/chains/<block>/          the current capture path (RTH blocks only)
    data/stage3_chains/<date>/    the legacy path, for what already exists

and keys observations on the **session block** rather than the date. That is
the point of the block design: a Friday-evening, Saturday and Sunday capture
all carry `2026-08-21_closed` and collapse to one row, instead of manufacturing
three "independent" observations of a single stale quote. Only regular-session
blocks are ingested at all -- pre-market and weekend marks are last-trade
echoes with synthetic spreads, and treating them as samples is what would
corrupt the metric this module exists to produce.

The observation itself is the put IV at the strike nearest the configured
target delta within the DTE window: the IV of the option you would actually be
selling, not an arbitrary at-the-money point.
"""
from __future__ import annotations

import duckdb
import pandas as pd

from analytics.chain_utils import nearest_target_delta_put
from core.paths import db_iv_history, load_config

MIN_OBSERVATIONS_FOR_RANK = 10


def _ensure_schema(con) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS iv_points (
            ticker VARCHAR, block VARCHAR, date DATE, iv DOUBLE, put_delta DOUBLE,
            dte INTEGER, strike DOUBLE, spot DOUBLE, source VARCHAR,
            PRIMARY KEY (ticker, block)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS ingested_ticker_blocks (
            ticker VARCHAR, block VARCHAR, PRIMARY KEY (ticker, block)
        )
    """)
    # Migrate a pre-Phase-5 database, which keyed on date and had no block or
    # source column. Dropping it would silently discard real accumulated
    # history, so the old rows are carried forward with the date as the block.
    columns = {row[1] for row in con.execute("PRAGMA table_info('iv_points')").fetchall()}
    if "block" not in columns:
        con.execute("ALTER TABLE iv_points RENAME TO iv_points_legacy")
        _ensure_schema(con)
        con.execute("""
            INSERT OR IGNORE INTO iv_points
            SELECT ticker, CAST(date AS VARCHAR), date, iv, put_delta, dte,
                   strike, spot, 'legacy' FROM iv_points_legacy
        """)
        con.execute("DROP TABLE iv_points_legacy")


def _extract(chain: pd.DataFrame, underlying: pd.DataFrame | None,
              as_of: str, cfg: dict) -> dict | None:
    thresholds = cfg["stage3_thresholds"]
    nearest = nearest_target_delta_put(
        chain, as_of, thresholds["target_delta"],
        thresholds["dte_min"], thresholds["dte_max"])
    if nearest is None or nearest.get("put_iv") is None:
        return None

    spot = None
    if underlying is not None and not underlying.empty:
        for column in ("mark", "last", "bid"):
            if column in underlying.columns and pd.notna(underlying[column].iloc[0]):
                spot = float(underlying[column].iloc[0])
                break

    return {"iv": nearest["put_iv"], "put_delta": nearest["put_delta"],
            "dte": nearest["dte"], "strike": nearest["strike"], "spot": spot}


def _available_blocks(ticker: str) -> list[tuple[str, str, str]]:
    """Every ingestable capture for a ticker as (block, iso_date, source).

    Regular-session captures only. Sorted oldest first so the newest
    observation is genuinely the most recent.
    """
    from core.market_calendar import is_ingestable_for_iv_history

    out: list[tuple[str, str, str]] = []

    # Current path.
    try:
        from data_sources.chains import _paths, list_blocks
        for block in list_blocks():
            if not is_ingestable_for_iv_history(block):
                continue
            if _paths(ticker, block)[0].exists():
                out.append((block, block.split("_", 1)[0], "chains"))
    except Exception:
        pass

    # Legacy path, so nothing already captured is lost.
    try:
        for date in _stage3_dates(ticker):
            out.append((str(date), str(date), "stage3"))
    except Exception:
        pass

    return sorted(set(out), key=lambda row: (row[1], row[0]))


def _load(ticker: str, block: str, source: str):
    if source == "chains":
        from data_sources.chains import load_chain
        return load_chain(ticker, block)
    return _load_stage3(ticker, block)


# --- Legacy stage3_chains reader -------------------------------------------
# Ported from the retired analytics/data_access.py (now legacy/) in Phase 8:
# scripts/04 still writes this layout, and IV history must keep reading it.

def _stage3_root():
    from core.paths import data_dir
    return data_dir() / "stage3_chains"


def _stage3_file(date: str, ticker: str, kind: str):
    matches = sorted((_stage3_root() / date).glob(f"{ticker}_{kind}_*.parquet"))
    return matches[-1] if matches else None      # last scan of the day


def _stage3_dates(ticker: str) -> list[str]:
    root = _stage3_root()
    if not root.exists():
        return []
    return [d.name for d in sorted(root.iterdir())
            if d.is_dir() and _stage3_file(d.name, ticker, "full_chain")]


def _load_stage3(ticker: str, date: str):
    """(chain, underlying) for one legacy snapshot date, as data_access did."""
    import pandas as pd
    chain_file = _stage3_file(date, ticker, "full_chain")
    if chain_file is None:
        return pd.DataFrame(), None
    under_file = _stage3_file(date, ticker, "underlying")
    return (pd.read_parquet(chain_file),
            pd.read_parquet(under_file) if under_file else None)


def ingest_new_snapshots(tickers: list[str] | None = None) -> int:
    """Cache any capture not yet seen. Cheap no-op when nothing is new."""
    cfg = load_config()
    if tickers is None:
        from core.paths import load_universe
        tickers = load_universe()

    con = duckdb.connect(str(db_iv_history()))
    try:
        _ensure_schema(con)
        already = {(r[0], r[1]) for r in con.execute(
            "SELECT ticker, block FROM ingested_ticker_blocks").fetchall()}

        added = 0
        for ticker in tickers:
            for block, iso_date, source in _available_blocks(ticker):
                if (ticker, block) in already:
                    continue
                try:
                    chain, underlying = _load(ticker, block, source)
                except Exception:
                    chain, underlying = pd.DataFrame(), None
                point = (_extract(chain, underlying, iso_date, cfg)
                         if chain is not None and not chain.empty else None)
                if point is not None:
                    con.execute(
                        "INSERT OR REPLACE INTO iv_points VALUES (?,?,?,?,?,?,?,?,?)",
                        [ticker, block, iso_date, point["iv"], point["put_delta"],
                         point["dte"], point["strike"], point["spot"], source])
                    added += 1
                # Record the attempt either way, so an unusable capture is not
                # re-parsed on every page load.
                con.execute("INSERT OR REPLACE INTO ingested_ticker_blocks VALUES (?,?)",
                             [ticker, block])
        return added
    finally:
        con.close()


def load_iv_history(ticker: str, ingest: bool = True) -> pd.DataFrame:
    if ingest:
        try:
            ingest_new_snapshots([ticker])
        except Exception:
            pass
    con = duckdb.connect(str(db_iv_history()))
    try:
        _ensure_schema(con)
        frame = con.execute(
            "SELECT block, date, iv, put_delta, dte, strike, spot, source "
            "FROM iv_points WHERE ticker = ? ORDER BY date, block", [ticker]).fetchdf()
    finally:
        con.close()
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"])
    return frame


def iv_rank_and_percentile(ticker: str) -> dict:
    """Where today's IV sits against this ticker's own accumulated history.

    IV rank is the position between the observed min and max; IV percentile is
    the share of observations at or below today. Both are meaningless from two
    or three captures, so under `MIN_OBSERVATIONS_FOR_RANK` this returns None
    with an explanatory note rather than a precise-looking number built on
    nothing.
    """
    history = load_iv_history(ticker)
    if history.empty:
        return {"iv_rank": None, "iv_percentile": None, "current_iv": None,
                "n_observations": 0,
                "note": "no regular-session chain captures for this ticker yet"}

    current = float(history["iv"].iloc[-1])
    n = len(history)
    if n < MIN_OBSERVATIONS_FOR_RANK:
        return {
            "iv_rank": None, "iv_percentile": None, "current_iv": current,
            "n_observations": n,
            "note": (f"{n} regular-session capture(s) so far -- "
                     f"{MIN_OBSERVATIONS_FOR_RANK} needed. Each weekday run during "
                     f"market hours adds one; weekend and after-hours runs "
                     f"deliberately do not."),
        }

    low, high = float(history["iv"].min()), float(history["iv"].max())
    rank = (current - low) / (high - low) if high > low else 0.5
    return {
        "iv_rank": float(rank),
        "iv_percentile": float((history["iv"] <= current).mean()),
        "current_iv": current, "n_observations": n, "note": None,
    }


def coverage() -> pd.DataFrame:
    """How close is each ticker to having a usable IV rank?

    Surfaced on the Validation page: the honest answer to "when does this
    metric switch on" is a count per ticker, not a promise.
    """
    from core.paths import load_universe
    rows = []
    for ticker in load_universe():
        history = load_iv_history(ticker, ingest=False)
        n = len(history)
        rows.append({
            "ticker": ticker, "observations": n,
            "needed": max(MIN_OBSERVATIONS_FOR_RANK - n, 0),
            "active": n >= MIN_OBSERVATIONS_FOR_RANK,
            "first": str(history["date"].min().date()) if n else None,
            "latest": str(history["date"].max().date()) if n else None,
        })
    return pd.DataFrame(rows).sort_values("observations", ascending=False)
