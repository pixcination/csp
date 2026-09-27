"""
Stage 3 handoff: once you've reviewed output/stage1_candidates.csv, applied
the Stage 2 quality tags, and confirmed option-chain liquidity for your
finalized list, put one ticker per line in output/final_universe.txt
(blank lines and lines starting with # are ignored) and run this script.

It copies each ticker's full raw 1-minute history from pricing_data_root
into project_root/data/raw_1m/<ticker>/. It's safe to re-run: it only
copies files that are new or have changed size/mtime since the last run,
so you can use this same script later to pull in freshly updated monthly
files (e.g. re-run monthly to pick up the latest data for your existing
universe).

Usage (from D:\\csp):
    python scripts\\03_copy_selected_tickers.py
"""
import shutil
import sys
from pathlib import Path

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


def read_ticker_list(path: Path):
    if not path.exists():
        sys.exit(
            f"ERROR: {path} not found.\n"
            f"Create it with one ticker per line (the folder name exactly as it "
            f"appears under pricing_data_root), then re-run this script."
        )
    tickers = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tickers.append(line)
    return tickers


def sync_ticker(src_folder: Path, dst_folder: Path):
    dst_folder.mkdir(parents=True, exist_ok=True)
    copied, skipped = 0, 0
    for src_file in src_folder.glob("*.txt"):
        dst_file = dst_folder / src_file.name
        if dst_file.exists():
            s_stat, d_stat = src_file.stat(), dst_file.stat()
            if s_stat.st_size == d_stat.st_size and s_stat.st_mtime <= d_stat.st_mtime:
                skipped += 1
                continue
        shutil.copy2(src_file, dst_file)
        copied += 1
    return copied, skipped


def main():
    cfg = load_config()
    src_root = Path(cfg["pricing_data_root"])
    proj = Path(cfg["project_root"])
    ticker_list_path = proj / "output" / "final_universe.txt"
    dst_root = proj / "data" / "raw_1m"

    tickers = read_ticker_list(ticker_list_path)
    print(f"{len(tickers)} tickers listed in {ticker_list_path}\n")

    total_copied, total_skipped, missing = 0, 0, []
    for i, ticker in enumerate(tickers, 1):
        src_folder = src_root / ticker
        if not src_folder.exists():
            print(f"[{i}/{len(tickers)}] {ticker}: NOT FOUND under {src_root} -- skipping")
            missing.append(ticker)
            continue
        copied, skipped = sync_ticker(src_folder, dst_root / ticker)
        total_copied += copied
        total_skipped += skipped
        status = f"{copied} new/updated file(s)" if copied else "already up to date"
        print(f"[{i}/{len(tickers)}] {ticker}: {status} ({skipped} unchanged)")

    print(f"\nDone. Copied/updated {total_copied} files, {total_skipped} already current.")
    if missing:
        print(f"WARNING: {len(missing)} ticker(s) not found in source: {missing}")
    print(f"Raw 1-minute data now available at: {dst_root}")
    print("Next: convert this into the app's working format (partitioned parquet / "
          "DuckDB) -- this is the first task to hand to Claude Code.")


if __name__ == "__main__":
    main()
