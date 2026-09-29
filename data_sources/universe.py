"""
The universe registry -- one row per symbol the tool knows about (Phase 9).

Replaces `output/final_universe.txt` as the source of truth. The text file is
kept as an import format; `core.paths.load_universe()` reads this registry.

WHY A REGISTRY RATHER THAN A LIST
---------------------------------
A bare ticker list cannot say that XSP is cash-settled and European (so a
cash-secured put does not apply), that BRK.B is `BRK-B` on Yahoo and `BRK/B`
on TastyTrade, or that SPY has no earnings (so the fail-safe "unknown
earnings date blocks the trade" rule must not apply to it -- before Phase 9
it permanently blocked every ETF in the universe). Each of those is a column
here.

STORAGE
-------
`data/universe.duckdb -> universe` is the working table (joins, page edits).
Because it is hand-maintained user data and `data/` is not versioned, every
write is mirrored to `config/universe.csv`, which IS versioned, and an empty
database re-seeds from that snapshot before falling back to
`final_universe.txt` + `stage2_quality_tags_master.csv`.

SYMBOL MAPPING
--------------
`symbol` is the canonical ticker (BRK.B, SPX). `yf_symbol` / `tt_symbol` are
the vendor forms. `price_scale` multiplies the Yahoo series: XSP is stored as
^SPX x 0.1 because Yahoo's own ^XSP history starts only in 2021, while the
index it tracks goes back to 1927 (verified equal to the cent on the overlap).
"""
from __future__ import annotations

import datetime as dt
import re

import duckdb
import pandas as pd

from core.paths import (config_dir, db_universe, final_universe_file, load_universe_file,
                        output_dir)

TABLE = "universe"
SNAPSHOT = "universe.csv"
#: Symbols registered by the Symbol Lookup page (Phase 20B). Kept out of
#: `symbols()` (so `universe: all`, the nightly data stages and the archive)
#: and out of every keyed request universe until promoted with
#: `promote_adhoc`; an explicit list or `tag:adhoc` still reaches them.
ADHOC_TAG = "adhoc"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    symbol VARCHAR PRIMARY KEY,
    yf_symbol VARCHAR, tt_symbol VARCHAR, price_scale DOUBLE DEFAULT 1.0,
    asset_class VARCHAR,            -- stock | etf | index
    category VARCHAR, sector VARCHAR, industry VARCHAR, quality_tier VARCHAR,
    optionable BOOLEAN, weeklies BOOLEAN, leverage_flag BOOLEAN,
    settlement VARCHAR,             -- physical | cash
    exercise VARCHAR,               -- american | european
    settlement_times VARCHAR,       -- PM | AM+PM (from TastyTrade expirations)
    active BOOLEAN DEFAULT TRUE,
    tags VARCHAR, notes VARCHAR, source VARCHAR,
    added_at TIMESTAMP, updated_at TIMESTAMP,
    stage1_pass BOOLEAN, stage1_tier VARCHAR, stage1_reasons VARCHAR,
    stage1_checked_at TIMESTAMP
)
"""
COLUMNS = ["symbol", "yf_symbol", "tt_symbol", "price_scale", "asset_class", "category",
           "sector", "industry", "quality_tier", "optionable", "weeklies",
           "leverage_flag", "settlement", "exercise", "settlement_times", "active",
           "tags", "notes", "source", "added_at", "updated_at", "stage1_pass",
           "stage1_tier", "stage1_reasons", "stage1_checked_at"]
ASSET_CLASSES = ("stock", "etf", "index")

#: Cash-settled, European-style index options. CSP does not apply to these.
INDEXES = {
    # symbol: (yf_symbol, tt_symbol, price_scale)
    "SPX": ("^SPX", "SPX", 1.0),
    "XSP": ("^SPX", "XSP", 0.1),
    "NDX": ("^NDX", "NDX", 1.0),
    "RUT": ("^RUT", "RUT", 1.0),
}
#: Added to the seed beyond final_universe.txt (Phase 9 decision).
DEFAULT_ADDITIONS = {"SPY": "etf", "QQQ": "etf", "IWM": "etf", "DIA": "etf",
                     "SPX": "index", "XSP": "index", "NDX": "index", "RUT": "index"}


# --- Mapping ---------------------------------------------------------------

def normalise(symbol: str) -> str:
    """Canonical form: upper case, class shares with a dot (BRK-B, BRK/B -> BRK.B),
    index carets dropped (^SPX -> SPX)."""
    s = str(symbol).strip().upper().lstrip("^")
    return s.replace("/", ".").replace("-", ".")


def default_mapping(symbol: str) -> tuple[str, str, float]:
    """(yf_symbol, tt_symbol, price_scale) for a canonical symbol."""
    symbol = normalise(symbol)
    if symbol in INDEXES:
        return INDEXES[symbol]
    return symbol.replace(".", "-"), symbol.replace(".", "/"), 1.0


def _defaults(symbol: str, asset_class: str | None) -> dict:
    symbol = normalise(symbol)
    yf_symbol, tt_symbol, scale = default_mapping(symbol)
    asset_class = (asset_class or ("index" if symbol in INDEXES else "stock")).lower()
    if asset_class not in ASSET_CLASSES:
        raise ValueError(f"asset_class must be one of {ASSET_CLASSES}")
    is_index = asset_class == "index"
    return {"symbol": symbol, "yf_symbol": yf_symbol, "tt_symbol": tt_symbol,
            "price_scale": scale, "asset_class": asset_class,
            "settlement": "cash" if is_index else "physical",
            "exercise": "european" if is_index else "american",
            "optionable": True, "active": True, "leverage_flag": False}


# --- Storage ---------------------------------------------------------------

def _connect(read_only: bool = False):
    path = db_universe()
    if read_only and not path.exists():
        read_only = False
    con = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA)
    return con


def _count(con) -> int:
    return con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]


def _upsert(con, rows: list[dict]) -> None:
    if not rows:
        return
    frame = pd.DataFrame(rows)
    for column in COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    frame = frame[COLUMNS]
    con.register("incoming", frame)
    try:
        con.execute(f"DELETE FROM {TABLE} WHERE symbol IN (SELECT symbol FROM incoming)")
        con.execute(f"INSERT INTO {TABLE} SELECT * FROM incoming")
    finally:
        con.unregister("incoming")


def export_snapshot(con=None) -> None:
    """Mirror the registry to the versioned config/universe.csv."""
    own = con is None
    con = con or _connect()
    try:
        frame = con.execute(f"SELECT * FROM {TABLE} ORDER BY symbol").fetchdf()
    finally:
        if own:
            con.close()
    frame.to_csv(config_dir() / SNAPSHOT, index=False, lineterminator="\n")


def _seed_rows() -> tuple[list[dict], str]:
    """Rows for an empty registry: the versioned snapshot if present, else
    final_universe.txt + stage-2 tags + DEFAULT_ADDITIONS."""
    snapshot = config_dir() / SNAPSHOT
    if snapshot.exists():
        frame = pd.read_csv(snapshot)
        frame = frame.astype(object).where(frame.notna(), None)
        return frame.to_dict("records"), "config/universe.csv"

    now = dt.datetime.now()
    tags_path = output_dir() / "stage2_quality_tags_master.csv"
    tags = (pd.read_csv(tags_path).set_index("ticker")
            if tags_path.exists() else pd.DataFrame())
    rows = []
    symbols = [normalise(s) for s in load_universe_file()]
    for symbol in symbols + [s for s in DEFAULT_ADDITIONS if s not in symbols]:
        asset_class = DEFAULT_ADDITIONS.get(symbol)
        row = {}
        key = symbol.replace(".", "-")
        for candidate in (symbol, key):
            if candidate in tags.index:
                row = tags.loc[candidate].to_dict()
                break
        if not asset_class:
            asset_class = "etf" if str(row.get("asset_class", "")).upper() == "ETF" else "stock"
        record = _defaults(symbol, asset_class)
        record.update({
            "category": row.get("category"),
            "quality_tier": row.get("quality_tier"),
            "leverage_flag": str(row.get("leverage_flag", "No")).lower() in ("yes", "true", "1"),
            "notes": row.get("notes") if isinstance(row.get("notes"), str) else None,
            "source": ("final_universe.txt" if symbol in symbols else "phase9 default"),
            "added_at": now, "updated_at": now,
        })
        rows.append(record)
    return rows, "final_universe.txt + stage2 tags"


def ensure() -> int:
    """Create and seed the registry if empty. Returns the row count."""
    con = _connect()
    try:
        if _count(con) == 0:
            rows, _ = _seed_rows()
            _upsert(con, rows)
            export_snapshot(con)
        return _count(con)
    finally:
        con.close()


# --- Reads -----------------------------------------------------------------

def load(active_only: bool = False) -> pd.DataFrame:
    ensure()
    con = _connect(read_only=True)
    try:
        query = f"SELECT * FROM {TABLE}"
        if active_only:
            query += " WHERE active"
        return con.execute(query + " ORDER BY symbol").fetchdf()
    finally:
        con.close()


def tag_list(tags) -> list[str]:
    """A registry `tags` value as lower-case tags."""
    if not isinstance(tags, str):
        return []
    return [t for t in re.split(r"[,; ]+", tags.strip().lower()) if t]


def is_adhoc(tags) -> bool:
    return ADHOC_TAG in tag_list(tags)


def adhoc_symbols() -> set[str]:
    """Registered through Symbol Lookup and not yet promoted."""
    frame = load()
    return set(frame.loc[frame["tags"].map(is_adhoc), "symbol"]) if not frame.empty else set()


def symbols(scope: str = "csp") -> list[str]:
    """Active symbols. scope="csp" drops cash-settled indices; "all" keeps them.
    Ad-hoc (Symbol Lookup) symbols are left out of both."""
    if scope not in ("csp", "all"):
        raise ValueError("scope must be 'csp' or 'all'")
    frame = load(active_only=True)
    frame = frame[~frame["tags"].map(is_adhoc)]
    if scope == "csp":
        frame = frame[frame["settlement"].fillna("physical") != "cash"]
    return frame["symbol"].tolist()


def get(symbol: str) -> dict | None:
    frame = load()
    rows = frame[frame["symbol"] == normalise(symbol)]
    return rows.iloc[0].to_dict() if not rows.empty else None


def asset_class(symbol: str) -> str | None:
    row = get(symbol)
    return row["asset_class"] if row else None


def vendor_map(symbol_list: list[str] | None = None) -> dict[str, tuple[str, float]]:
    """{symbol: (yf_symbol, price_scale)} -- registry values, else defaults."""
    try:
        frame = load().set_index("symbol")
    except Exception:
        frame = pd.DataFrame()
    out = {}
    for symbol in symbol_list if symbol_list is not None else frame.index.tolist():
        key = normalise(symbol)
        if key in frame.index:
            row = frame.loc[key]
            out[symbol] = (row["yf_symbol"] or default_mapping(key)[0],
                           float(row["price_scale"] or 1.0))
        else:
            yf_symbol, _, scale = default_mapping(key)
            out[symbol] = (yf_symbol, scale)
    return out


def tt_symbols(symbol_list: list[str]) -> dict[str, str]:
    """{symbol: tt_symbol}."""
    frame = load().set_index("symbol")
    return {s: (frame.loc[normalise(s), "tt_symbol"] if normalise(s) in frame.index
                else default_mapping(s)[1]) for s in symbol_list}


# --- Writes ----------------------------------------------------------------

def add(symbol: str, asset_class: str | None = None, tags: str = "",
        notes: str = "", **fields) -> dict:
    """Add (or re-activate) a symbol with default vendor mapping."""
    ensure()
    record = _defaults(symbol, asset_class)
    now = dt.datetime.now()
    con = _connect()
    try:
        existing = con.execute(f"SELECT * FROM {TABLE} WHERE symbol = ?",
                               [record["symbol"]]).fetchdf()
        if not existing.empty:
            merged = existing.iloc[0].to_dict()
            merged.update({"active": True, "updated_at": now})
            if asset_class:
                merged.update({k: record[k] for k in ("asset_class", "settlement", "exercise")})
            if tags:
                merged["tags"] = tags
            if notes:
                merged["notes"] = notes
            merged.update(fields)
            record = merged
        else:
            record.update({"tags": tags or None, "notes": notes or None,
                           "source": "manual", "added_at": now, "updated_at": now})
            record.update(fields)
        _upsert(con, [record])
        export_snapshot(con)
    finally:
        con.close()
    return record


def update(symbol: str, **fields) -> None:
    unknown = set(fields) - set(COLUMNS)
    if unknown:
        raise ValueError(f"unknown registry fields: {sorted(unknown)}")
    ensure()
    con = _connect()
    try:
        assignments = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = ?"
        con.execute(f"UPDATE {TABLE} SET {assignments} WHERE symbol = ?",
                    list(fields.values()) + [dt.datetime.now(), normalise(symbol)])
        export_snapshot(con)
    finally:
        con.close()


def promote_adhoc(symbol: str) -> None:
    """"Add to universe": drop the adhoc tag, keeping any others."""
    row = get(symbol)
    if row is None:
        raise ValueError(f"{normalise(symbol)} is not registered")
    kept = [t for t in tag_list(row.get("tags")) if t != ADHOC_TAG]
    update(symbol, tags=",".join(kept) or None, active=True)


def set_active(symbol: str, active: bool) -> None:
    update(symbol, active=bool(active))


def remove(symbol: str) -> None:
    """Delete outright. Prefer `set_active(symbol, False)` to keep the tags."""
    ensure()
    con = _connect()
    try:
        con.execute(f"DELETE FROM {TABLE} WHERE symbol = ?", [normalise(symbol)])
        export_snapshot(con)
    finally:
        con.close()


def apply_market_metrics(metrics: pd.DataFrame) -> int:
    """Fill vendor-derived columns from a market_metrics snapshot: sector and
    industry (only where blank), optionable, weeklies, settlement_times."""
    if metrics is None or metrics.empty:
        return 0
    ensure()
    con = _connect()
    changed = 0
    try:
        for _, row in metrics.iterrows():
            symbol = normalise(row["symbol"])
            current = con.execute(f"SELECT sector, industry FROM {TABLE} WHERE symbol = ?",
                                  [symbol]).fetchone()
            if current is None:
                continue
            sets = {"optionable": bool(row.get("n_expirations") or 0),
                    "weeklies": bool(row.get("weeklies")),
                    "settlement_times": row.get("settlement_times")}
            if not current[0] and isinstance(row.get("sector"), str):
                sets["sector"] = row["sector"]
            if not current[1] and isinstance(row.get("industry"), str):
                sets["industry"] = row["industry"]
            assignments = ", ".join(f"{k} = ?" for k in sets)
            con.execute(f"UPDATE {TABLE} SET {assignments} WHERE symbol = ?",
                        list(sets.values()) + [symbol])
            changed += 1
        export_snapshot(con)
    finally:
        con.close()
    return changed


def record_stage1(results: pd.DataFrame) -> None:
    """Write `universe_screen.screen()` output onto the registry rows."""
    if results is None or results.empty:
        return
    ensure()
    con = _connect()
    now = dt.datetime.now()
    try:
        for _, row in results.iterrows():
            con.execute(f"UPDATE {TABLE} SET stage1_pass = ?, stage1_tier = ?, "
                        f"stage1_reasons = ?, stage1_checked_at = ? WHERE symbol = ?",
                        [None if pd.isna(row["stage1_pass"]) else bool(row["stage1_pass"]),
                         row["tier"], row["reasons"] or None, now, row["symbol"]])
        export_snapshot(con)
    finally:
        con.close()


def import_text_file(path=None) -> int:
    """Add every symbol in a final_universe.txt-style file. Returns rows added."""
    path = path or final_universe_file()
    from pathlib import Path
    existing = set(load()["symbol"])
    added = 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        symbol = line.split("#", 1)[0].strip()
        if symbol and normalise(symbol) not in existing:
            add(symbol)
            added += 1
    return added
