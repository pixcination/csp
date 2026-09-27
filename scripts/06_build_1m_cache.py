"""
06_build_1m_cache.py
=====================
Converts the raw per-ticker 1-minute text files under data/raw_1m/<ticker>/
(populated by 03_copy_selected_tickers.py for the finalized Stage 3 universe)
into a single query-friendly DuckDB table: data/raw_1m_cache.duckdb.

Why this exists: the source files are reverse-chronological, per-ticker-month
text files with inconsistent precision/line-endings across data eras
(AlphaVantage vs. massive.com -- see docs/PROJECT_SPEC.md). Re-parsing those on
every chart render or volatility calc would be slow and error-prone. This
script does the sort-and-normalize once per ticker and caches the result,
tagging each bar with whether it falls in the regular session (from
config.yaml's session_start/session_end) so downstream code doesn't need to
recompute that on every query.

Safe to re-run: tracks which source files have already been ingested (by
path + size + mtime, same approach as 03_copy_selected_tickers.py) and only
(re)processes files that are new or changed.

Usage (from D:\\csp):
    python scripts\\06_build_1m_cache.py
"""
from pathlib import Path

import duckdb
import yaml


def load_config(path=None):
    """Load config.yaml, resolved relative to this file rather than the shell's
    working directory.

    The old default (`path="config.yaml"`) meant this script only worked when
    launched from the project root, and silently loaded a *different* config if
    one happened to exist wherever you were standing. Portability fix -- see
    core/paths.py.
    """
    import sys
    _root = Path(__file__).resolve().parent.parent
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
    if path is None:
        from core.paths import load_config as _load
        return _load()
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def ensure_schema(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS bars_1m (
            ticker VARCHAR,
            datetime TIMESTAMP,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume BIGINT,
            regular_session BOOLEAN
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS bars_1m_ingested_files (
            ticker VARCHAR,
            filename VARCHAR,
            size BIGINT,
            mtime DOUBLE,
            PRIMARY KEY (ticker, filename)
        )
    """)


def ingested_set(con, ticker):
    rows = con.execute(
        "SELECT filename, size, mtime FROM bars_1m_ingested_files WHERE ticker = ?",
        [ticker],
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def main():
    cfg = load_config()
    proj = Path(cfg["project_root"])
    raw_root = proj / "data" / "raw_1m"
    db_path = proj / "data" / "raw_1m_cache.duckdb"
    session_start = cfg["session_start"]
    session_end = cfg["session_end"]

    if not raw_root.exists():
        print(f"ERROR: {raw_root} not found. Run 03_copy_selected_tickers.py first.")
        return

    tickers = sorted(d.name for d in raw_root.iterdir() if d.is_dir())
    print(f"{len(tickers)} ticker folder(s) under {raw_root}")

    con = duckdb.connect(str(db_path))
    # This machine's free RAM is modest relative to DuckDB's default (~80% of
    # total) memory_limit -- a multi-hundred-file glob + sort blew that
    # budget on the first attempt (OOM on the 9th ticker). Cap it explicitly
    # and insert file-by-file below so peak memory stays bounded to roughly
    # one month's worth of 1-minute bars at a time, not one ticker's full
    # history at once.
    con.execute("PRAGMA memory_limit='3GB'")
    con.execute("PRAGMA threads=2")
    # DuckDB's default temp_directory derivation (db path + ".tmp") produced an
    # invalid UNC-style path on this Windows setup when spilling during the
    # final CREATE INDEX. Point it at an explicit, real directory instead.
    tmp_dir = db_path.parent / "duckdb_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    con.execute(f"PRAGMA temp_directory='{tmp_dir.as_posix()}'")
    ensure_schema(con)

    total_new_rows = 0
    for i, ticker in enumerate(tickers, 1):
        ticker_dir = raw_root / ticker
        files = sorted(ticker_dir.glob("*.txt"))
        already = ingested_set(con, ticker)

        new_files = []
        for f in files:
            stat = f.stat()
            key = (stat.st_size, stat.st_mtime)
            prev = already.get(f.name)
            if prev is None or (prev[0], prev[1]) != key:
                new_files.append(f)

        if not new_files:
            print(f"[{i}/{len(tickers)}] {ticker}: up to date ({len(files)} file(s), 0 new)")
            continue

        # File naming isn't guaranteed stable enough to trust a targeted
        # month-range delete, so any change to a ticker's files triggers a
        # full rebuild of that ticker's rows -- still cheap relative to the
        # whole universe since it's done one ticker at a time.
        con.execute("DELETE FROM bars_1m WHERE ticker = ?", [ticker])

        # Insert one source file (one ticker-month) at a time rather than a
        # single glob+union across a ticker's whole history -- keeps peak
        # memory bounded regardless of how many years of history a ticker
        # has. No ORDER BY needed here: nothing downstream depends on
        # physical row order, and the index below makes ordered range scans
        # cheap at query time regardless of insertion order.
        for f in files:
            con.execute("""
                INSERT INTO bars_1m
                SELECT
                    ? AS ticker,
                    Datetime AS datetime,
                    CAST(Open AS DOUBLE) AS open,
                    CAST(High AS DOUBLE) AS high,
                    CAST(Low AS DOUBLE) AS low,
                    CAST(Close AS DOUBLE) AS close,
                    CAST(Volume AS BIGINT) AS volume,
                    (strftime(Datetime, '%H:%M') >= ? AND strftime(Datetime, '%H:%M') <= ?) AS regular_session
                FROM read_csv_auto(?, header=true)
            """, [ticker, session_start, session_end, str(f)])

        n_rows = con.execute("SELECT COUNT(*) FROM bars_1m WHERE ticker = ?", [ticker]).fetchone()[0]
        total_new_rows += n_rows

        con.execute("DELETE FROM bars_1m_ingested_files WHERE ticker = ?", [ticker])
        con.executemany(
            "INSERT INTO bars_1m_ingested_files VALUES (?, ?, ?, ?)",
            [(ticker, f.name, f.stat().st_size, f.stat().st_mtime) for f in files],
        )
        print(f"[{i}/{len(tickers)}] {ticker}: rebuilt from {len(files)} file(s), {n_rows:,} bars")

    # No explicit index: DuckDB's columnar storage already keeps per-row-group
    # min/max zone maps, which prune ticker/datetime range scans effectively
    # at this scale without needing an ART index -- and building one here
    # repeatedly hit this machine's memory ceiling for no measurable query
    # benefit.
    total_rows = con.execute("SELECT COUNT(*) FROM bars_1m").fetchone()[0]
    con.close()

    print(f"\nDone. {total_rows:,} total bars cached at {db_path}")


if __name__ == "__main__":
    main()
