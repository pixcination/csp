"""
Stage 0: Build a consolidated daily OHLCV summary for the ENTIRE ticker
universe by scanning the raw 1-minute text files and resampling to daily
bars (regular session only).

This deliberately does NOT materialize full 1-minute data for all ~1050
tickers -- that would be a huge amount of I/O for names you're about to
throw away. It produces one compact DuckDB file used purely for Stage 1
screening. Only the tickers that survive screening later get their full
1-minute history copied into the project folder (see script 03).

This script only READS from pricing_data_root -- it never modifies your
source drive.

Usage (from D:\\csp):
    python scripts\\01_build_daily_summary.py
"""
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import duckdb
import yaml

CSV_COLUMNS = {
    "Datetime": "VARCHAR",
    "Open": "DOUBLE",
    "High": "DOUBLE",
    "Low": "DOUBLE",
    "Close": "DOUBLE",
    "Volume": "BIGINT",
}


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


def list_ticker_folders(root: Path):
    if not root.exists():
        sys.exit(f"ERROR: pricing_data_root does not exist: {root}")
    return sorted([p for p in root.iterdir() if p.is_dir()])


def resolve_aliases(folders, alias_overrides):
    """
    Detect ticker-folder collisions caused by punctuation differences
    (e.g. BRK.B vs BRK-B, treated the same once you strip '.'/'-').
    Returns (resolved, collisions) where resolved is a list of
    (folder_path, canonical_ticker_name) to actually ingest.
    """
    groups = {}
    for f in folders:
        norm = re.sub(r"[^A-Za-z0-9]", "", f.name).upper()
        groups.setdefault(norm, []).append(f)

    resolved = []
    collisions = []
    for norm, group in groups.items():
        if len(group) == 1:
            resolved.append((group[0], group[0].name))
            continue

        collisions.append([f.name for f in group])
        chosen = None
        for f in group:
            if alias_overrides.get(f.name) == "canonical":
                chosen = f
        if chosen is None:
            # default: keep whichever folder has the most files (usually = longest history)
            chosen = max(group, key=lambda p: len(list(p.glob("*.txt"))))
        skipped = [f.name for f in group if f != chosen]
        print(f"  [ALIAS] {norm}: keeping '{chosen.name}', skipping {skipped} "
              f"(override in config.yaml -> alias_overrides to change)")
        resolved.append((chosen, chosen.name))

    return resolved, collisions


def build_daily_frame(ticker_folder_str: str, ticker: str, session_start: str, session_end: str,
                       worker_memory_limit_gb: float = 2.0):
    """
    Worker function -- runs in its own process with its own in-memory DuckDB
    connection. Returns (ticker, daily_df, meta_dict_or_reason_string).
    """
    ticker_folder = Path(ticker_folder_str)
    pattern = str(ticker_folder / "*.txt").replace("\\", "/")
    con = duckdb.connect()  # in-memory, isolated per worker

    # IMPORTANT: DuckDB's default memory_limit is ~80% of total system RAM
    # *per connection*. With N parallel worker processes that means N * 80%
    # of RAM gets claimed simultaneously, which reliably OOMs the machine
    # even on modest workloads. Cap each worker explicitly instead.
    con.execute(f"PRAGMA memory_limit='{worker_memory_limit_gb}GB'")
    con.execute("PRAGMA threads=2")

    def _try_read(mem_gb, threads):
        con.execute(f"PRAGMA memory_limit='{mem_gb}GB'")
        con.execute(f"PRAGMA threads={threads}")
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE _raw AS
            SELECT
                TRY_CAST(Datetime AS TIMESTAMP) AS ts,
                Open, High, Low, Close, Volume
            FROM read_csv(
                '{pattern}',
                columns = {CSV_COLUMNS},
                header = true,
                union_by_name = true,
                ignore_errors = true
            )
        """)

    try:
        _try_read(worker_memory_limit_gb, 2)
    except Exception as e1:
        # Retry once, single-threaded with a smaller memory footprint --
        # covers the case of an unusually large ticker on a memory-tight
        # machine rather than immediately giving up on it.
        try:
            _try_read(max(worker_memory_limit_gb / 2, 0.5), 1)
        except Exception as e2:
            return ticker, None, f"read_failed after retry: {e2}"

    n = con.execute("SELECT COUNT(*) FROM _raw WHERE ts IS NOT NULL").fetchone()[0]
    if n == 0:
        return ticker, None, "no_rows_parsed"

    # dedupe exact-timestamp collisions (can happen at source-transition boundaries)
    con.execute("""
        CREATE TEMP TABLE _dedup AS
        SELECT ts, arbitrary(Open) AS Open, MAX(High) AS High, MIN(Low) AS Low,
               arbitrary(Close) AS Close, SUM(Volume) AS Volume
        FROM (SELECT * FROM _raw WHERE ts IS NOT NULL)
        GROUP BY ts
    """)

    con.execute(f"""
        CREATE TEMP TABLE _session AS
        SELECT * FROM _dedup
        WHERE strftime(ts, '%H:%M') >= '{session_start}'
          AND strftime(ts, '%H:%M') <= '{session_end}'
    """)

    daily_df = con.execute("""
        SELECT
            CAST(ts AS DATE)   AS date,
            arg_min(Open, ts)  AS open,
            MAX(High)          AS high,
            MIN(Low)           AS low,
            arg_max(Close, ts) AS close,
            SUM(Volume)        AS volume,
            COUNT(*)           AS bar_count
        FROM _session
        GROUP BY 1
        ORDER BY 1
    """).fetchdf()

    con.close()

    if daily_df.empty:
        return ticker, None, "no_rows_in_session_window"

    meta = {
        "ticker": ticker,
        "days": len(daily_df),
        "first_date": daily_df["date"].iloc[0],
        "last_date": daily_df["date"].iloc[-1],
        "raw_rows": n,
    }
    return ticker, daily_df, meta


def main():
    cfg = load_config()
    src_root = Path(cfg["pricing_data_root"])
    out_db = Path(cfg["project_root"]) / "data" / "universe_daily.duckdb"
    out_db.parent.mkdir(parents=True, exist_ok=True)
    session_start = cfg.get("session_start", "09:30")
    session_end = cfg.get("session_end", "16:00")
    alias_overrides = cfg.get("alias_overrides") or {}

    folders = list_ticker_folders(src_root)
    print(f"Found {len(folders)} ticker folders under {src_root}")

    resolved, collisions = resolve_aliases(folders, alias_overrides)
    print(f"Resolved to {len(resolved)} unique tickers ({len(collisions)} alias collisions handled)\n")

    # fresh DB each run -- this table is cheap to rebuild (daily bars only)
    if out_db.exists():
        out_db.unlink()
    con = duckdb.connect(str(out_db))
    con.execute("""
        CREATE TABLE daily_bars (
            ticker VARCHAR, date DATE, open DOUBLE, high DOUBLE,
            low DOUBLE, close DOUBLE, volume BIGINT, bar_count INTEGER
        )
    """)
    con.execute("""
        CREATE TABLE ticker_meta (
            ticker VARCHAR, days INTEGER, first_date DATE, last_date DATE, raw_rows BIGINT
        )
    """)

    max_workers = cfg.get("ingest_workers", 4)
    worker_mem_gb = cfg.get("worker_memory_limit_gb", 2.0)
    print(f"Using {max_workers} parallel workers, {worker_mem_gb}GB memory cap each "
          f"({max_workers * worker_mem_gb:.0f}GB max total -- lower ingest_workers or "
          f"worker_memory_limit_gb in config.yaml if that exceeds your available RAM)\n")
    t0 = time.time()
    meta_rows = []
    skipped = []
    done = 0

    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(build_daily_frame, str(folder), ticker, session_start, session_end, worker_mem_gb): ticker
            for folder, ticker in resolved
        }
        for fut in as_completed(futures):
            ticker = futures[fut]
            done += 1
            try:
                t, daily_df, meta = fut.result()
            except Exception as e:
                print(f"[{done}/{len(resolved)}] {ticker}: WORKER CRASHED ({e})")
                skipped.append((ticker, f"worker_crashed: {e}"))
                continue

            if daily_df is None:
                print(f"[{done}/{len(resolved)}] {ticker}: SKIPPED ({meta})")
                skipped.append((ticker, meta))
                continue

            con.execute("INSERT INTO daily_bars SELECT ? AS ticker, * FROM daily_df", [t])
            meta_rows.append(meta)
            print(f"[{done}/{len(resolved)}] {ticker}: {meta['days']} days "
                  f"({meta['first_date']} -> {meta['last_date']})")

    for r in meta_rows:
        con.execute(
            "INSERT INTO ticker_meta VALUES (?, ?, ?, ?, ?)",
            [r["ticker"], r["days"], r["first_date"], r["last_date"], r["raw_rows"]],
        )

    con.close()
    print(f"\nDone in {time.time() - t0:.1f}s.")
    print(f"Daily summary DB: {out_db}")
    print(f"Tickers processed successfully: {len(meta_rows)} / {len(resolved)}")
    if skipped:
        print(f"Skipped {len(skipped)} tickers -- see [SKIPPED] lines above for reasons.")


if __name__ == "__main__":
    main()
