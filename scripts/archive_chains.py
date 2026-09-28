"""Phase 18: take today's full-universe chain snapshot into data/chain_archive/<date>/.

    .venv\Scripts\python scripts\archive_chains.py            # every active symbol
    .venv\Scripts\python scripts\archive_chains.py SPY QQQ    # a subset

Meant for 15:45 ET on trading days (config.yaml -> archive). Phase 19 schedules it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.progress import ConsoleReporter  # noqa: E402
from data_sources import chain_archive  # noqa: E402

if __name__ == "__main__":
    manifest = chain_archive.archive(sys.argv[1:] or None,
                                     reporter=ConsoleReporter([("archive", "Chain archive")]))
    print(f"{manifest['date']} {manifest['block']}: {len(manifest['tickers'])} tickers, "
          f"{manifest['rows']:,} rows, {manifest['bytes'] / 1e6:.1f} MB in "
          f"{manifest['seconds']:.0f}s; failed: {manifest['failed'] or 'none'}")
