"""
Shared read layer for the analytics engine and the Streamlit app. Every page
and analytics module reads pipeline output through these functions rather
than re-implementing CSV/DuckDB/parquet access inline, so there's exactly
one place that knows the on-disk layout the scripts/ pipeline produces.

Nothing here writes to D:\\pricing_data -- that source tree is read-only
(see docs/PROJECT_SPEC.md "Working conventions").
"""
from pathlib import Path
from functools import lru_cache

import duckdb
import pandas as pd

from analytics.config import project_root


# === Stage 1-3 pipeline output (CSV) ===

def load_stage3_candidates() -> pd.DataFrame:
    return pd.read_csv(project_root() / "output" / "stage3_candidates.csv")


def load_stage2_tags() -> pd.DataFrame:
    return pd.read_csv(project_root() / "output" / "stage2_quality_tags_master.csv")


def load_scanner_universe(universe: str = "all") -> pd.DataFrame:
    """
    Merges Stage 3 chain-liquidity results with Stage 2 quality tags into
    one row-per-ticker table -- the base the Scanner page and composite
    scoring both build on.

    universe: "all" | "tier1" | "tier2" | comma-separated ticker list
    """
    stage3 = load_stage3_candidates()
    stage3 = stage3[stage3["pass"] == True]  # noqa: E712 -- only tradable candidates
    stage2 = load_stage2_tags()

    df = stage3.merge(stage2, on="ticker", how="left", suffixes=("", "_stage2"))

    if universe == "tier1":
        df = df[df["data_tier"] == "Tier1_backtestable"]
    elif universe == "tier2":
        df = df[df["data_tier"] != "Tier1_backtestable"]
    elif universe not in ("all", None, ""):
        wanted = {t.strip().upper() for t in universe.split(",") if t.strip()}
        df = df[df["ticker"].str.upper().isin(wanted)]

    return df.reset_index(drop=True)


# === Daily bars (data/universe_daily.duckdb) ===

def _daily_db_path() -> Path:
    return project_root() / "data" / "universe_daily.duckdb"


DAILY_COLUMNS = ["ticker", "date", "open", "high", "low", "close",
                  "volume", "bar_count"]


def load_daily_bars(ticker: str | None = None, start: str | None = None,
                     end: str | None = None) -> pd.DataFrame:
    """Daily bars, or an empty frame when the database has not been built yet.

    Every caller in the app already branches on `.empty` and shows a "run the
    pipeline first" message. Raising instead meant a fresh copy of the folder --
    the portable case, where `data/` starts empty -- greeted the user with a
    DuckDB traceback on the Scanner and Ticker Detail pages rather than the
    instruction they needed. An absent database is a normal state, not an error.
    """
    if not _daily_db_path().exists():
        return pd.DataFrame(columns=DAILY_COLUMNS)
    con = duckdb.connect(str(_daily_db_path()), read_only=True)
    try:
        exists = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name = 'daily_bars'").fetchone()[0]
        if not exists:
            return pd.DataFrame(columns=DAILY_COLUMNS)
        clauses, params = [], []
        if ticker:
            clauses.append("ticker = ?")
            params.append(ticker)
        if start:
            clauses.append("date >= ?")
            params.append(start)
        if end:
            clauses.append("date <= ?")
            params.append(end)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        df = con.execute(
            f"SELECT ticker, date, open, high, low, close, volume, bar_count "
            f"FROM daily_bars {where} ORDER BY ticker, date", params,
        ).fetchdf()
    finally:
        con.close()
    df["date"] = pd.to_datetime(df["date"])
    return df


def latest_price_and_adv(ticker: str) -> dict:
    """Latest close + trailing 90-day dollar-volume ADV for a ticker --
    used by the Scanner table when the Stage 2 tag file's snapshot is stale."""
    bars = load_daily_bars(ticker)
    if bars.empty:
        return {"last_price": None, "adv_90d_dollars": None, "last_date": None}
    dollar_vol = bars["close"] * bars["volume"]
    return {
        "last_price": float(bars["close"].iloc[-1]),
        "adv_90d_dollars": float(dollar_vol.tail(90).mean()),
        "last_date": bars["date"].iloc[-1],
    }


# === 1-minute bar cache (data/raw_1m_cache.duckdb, built by scripts/06) ===

def _cache_db_path() -> Path:
    return project_root() / "data" / "raw_1m_cache.duckdb"


def has_1m_cache() -> bool:
    return _cache_db_path().exists()


def load_1m_bars(ticker: str, start: str | None = None, end: str | None = None,
                  regular_session_only: bool = True) -> pd.DataFrame:
    if not has_1m_cache():
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    con = duckdb.connect(str(_cache_db_path()), read_only=True)
    try:
        clauses, params = ["ticker = ?"], [ticker]
        if start:
            clauses.append("datetime >= ?")
            params.append(start)
        if end:
            clauses.append("datetime <= ?")
            params.append(end)
        if regular_session_only:
            clauses.append("regular_session = true")
        where = " AND ".join(clauses)
        df = con.execute(
            f"SELECT datetime, open, high, low, close, volume FROM bars_1m "
            f"WHERE {where} ORDER BY datetime", params,
        ).fetchdf()
    finally:
        con.close()
    return df


# === Stage 3 chain snapshots (data/stage3_chains/<date>/) ===

def _chains_root() -> Path:
    return project_root() / "data" / "stage3_chains"


@lru_cache(maxsize=1)
def list_snapshot_dates() -> list[str]:
    base = _chains_root()
    if not base.exists():
        return []
    return sorted(d.name for d in base.iterdir() if d.is_dir())


def list_snapshot_dates_for_ticker(ticker: str) -> list[str]:
    return [d for d in list_snapshot_dates() if _find_chain_file(d, ticker) is not None]


def _find_chain_file(date: str, ticker: str) -> Path | None:
    matches = sorted((_chains_root() / date).glob(f"{ticker}_full_chain_*.parquet"))
    return matches[-1] if matches else None  # last scan of the day if run more than once


def _find_underlying_file(date: str, ticker: str) -> Path | None:
    matches = sorted((_chains_root() / date).glob(f"{ticker}_underlying_*.parquet"))
    return matches[-1] if matches else None


def load_chain_snapshot(ticker: str, date: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """Returns (chain_df, underlying_quote_df) for a ticker on a given
    snapshot date (defaults to the most recent date that has this ticker)."""
    dates = list_snapshot_dates_for_ticker(ticker)
    if not dates:
        return pd.DataFrame(), None
    date = date or dates[-1]
    chain_file = _find_chain_file(date, ticker)
    if chain_file is None:
        return pd.DataFrame(), None
    chain = pd.read_parquet(chain_file)
    u_file = _find_underlying_file(date, ticker)
    underlying = pd.read_parquet(u_file) if u_file else None
    return chain, underlying


def load_all_snapshots_for_ticker(ticker: str) -> dict[str, pd.DataFrame]:
    """All accumulated chain snapshots for a ticker, keyed by date -- the
    raw material iv_history.py rolls up into a per-symbol IV history."""
    out = {}
    for date in list_snapshot_dates_for_ticker(ticker):
        chain_file = _find_chain_file(date, ticker)
        if chain_file is not None:
            out[date] = pd.read_parquet(chain_file)
    return out
