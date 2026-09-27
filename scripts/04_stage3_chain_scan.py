"""
04_stage3_chain_scan.py
========================
Batch option-chain liquidity scan across your Stage 2 candidate universe.

This is a DIFFERENT job than snapshot_loop.py. snapshot_loop.py is narrow
and deep: 1-2 symbols, continuously, at 5-min cadence, for building your
analytical engine. This script is broad and shallow: one chain pull per
candidate ticker (up to ~550), restricted to the 5-14 DTE window your
wheel strategy actually targets, run once (or periodically) rather than
continuously.

Reuses your existing tastytrade_common.py primitives and imports
pull_equity() directly from snapshot_loop.py rather than reimplementing
chain-pull logic -- REST for bid/ask/mark, DXLink streaming for open
interest and Greeks, exactly as your continuous loop already does it.

RATE LIMIT SAFETY
------------------
TastyTrade's dxFeed enforces (per their support notice, see your dev
conversation): max 25,000 subscriptions per session, max 10,000
subscription changes per minute (enforced as a rolling window, not an
instantaneous cap -- confirmed by your own SPY+QQQ testing).

Restricting each symbol to the 5-14 DTE window keeps individual symbol
subscription counts small (roughly 600-3,000 for a typical name, well
under the 25K per-session cap even for SPY/QQQ). The remaining risk is
the aggregate rate across many symbols run back-to-back, which this
script manages with an explicit rolling-window rate limiter -- it tracks
subscription counts sent over the trailing 60 seconds and sleeps before
the next symbol if that would push the rolling total over
`stage3_subs_per_minute_budget` (see config.yaml).

RESUMABILITY
------------
Each symbol's output files are checked before pulling -- if a chain +
underlying file pair already exists for today for that symbol, it's
skipped (unless --force). This means an interrupted run (network drop,
Ctrl+C, laptop sleep) can just be re-run and it'll pick up where it left
off rather than re-pulling everything.

USAGE (from D:\\csp, with tastytrade_common.py + snapshot_loop.py on
your Python path -- see config.yaml -> tastytrade_pipeline_dir)
-------------------------------------------------------------------
    python scripts\\04_stage3_chain_scan.py                # full universe
    python scripts\\04_stage3_chain_scan.py --force         # re-pull everything
    python scripts\\04_stage3_chain_scan.py --tickers SPY,QQQ,AAPL   # subset
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import deque
from datetime import date, datetime
from pathlib import Path

import pandas as pd
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


def load_pipeline_modules(pipeline_dir: str):
    """Add the TastyTrade pipeline directory to sys.path and import the
    pieces we need. Done at runtime (not module import time) so this
    script can still be inspected/tested without the pipeline installed."""
    p = Path(pipeline_dir)
    if not p.exists():
        sys.exit(
            f"ERROR: tastytrade_pipeline_dir '{p}' not found. Set it in "
            f"config.yaml to the folder containing tastytrade_common.py "
            f"and snapshot_loop.py."
        )
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
    import tastytrade_common as ttc          # noqa: F401 (imported for side effects / reuse)
    from snapshot_loop import pull_equity     # reuse the tested per-symbol pull logic
    return ttc, pull_equity


def load_stage3_universe(proj: Path, tickers_arg: str | None) -> list[str]:
    """Universe = Stage 2 master tag list minus anything flagged leveraged,
    unless --tickers is given, or output/stage3_universe.txt exists (lets
    you hand-curate the scan list without re-deriving it every time)."""
    if tickers_arg:
        return [t.strip().upper() for t in tickers_arg.split(",") if t.strip()]

    override = proj / "output" / "stage3_universe.txt"
    if override.exists():
        tickers = []
        for line in override.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                tickers.append(line.upper())
        return tickers

    master = proj / "output" / "stage2_quality_tags_master.csv"
    if not master.exists():
        sys.exit(
            f"ERROR: {master} not found and no output/stage3_universe.txt "
            f"override present. Run Stage 2 tagging first, or provide "
            f"--tickers, or create output/stage3_universe.txt manually."
        )
    df = pd.read_csv(master)
    keep = df[df["leverage_flag"] != "Yes"]
    tickers = sorted(keep["ticker"].dropna().unique().tolist())
    return tickers


class RollingSubRateLimiter:
    """Tracks (timestamp, sub_count) pairs over a trailing 60s window and
    sleeps before the next symbol if sending its estimated subscription
    count would push the rolling total over budget_per_min."""

    def __init__(self, budget_per_min: int, window_seconds: float = 60.0):
        self.budget = budget_per_min
        self.window = window_seconds
        self._events: deque[tuple[float, int]] = deque()

    def _prune(self, now: float) -> int:
        while self._events and now - self._events[0][0] > self.window:
            self._events.popleft()
        return sum(c for _, c in self._events)

    def wait_for_budget(self, upcoming_subs: int, log: logging.Logger) -> None:
        now = time.monotonic()
        current = self._prune(now)
        if current + upcoming_subs <= self.budget:
            return
        # Sleep until enough of the window has aged out to make room.
        # Simple approach: sleep in short increments, re-checking, rather
        # than computing an exact wake time -- robust to the event list
        # changing shape as old entries expire.
        while True:
            now = time.monotonic()
            current = self._prune(now)
            if current + upcoming_subs <= self.budget:
                return
            log.info(f"    [rate-limit] pausing ~2s (rolling 60s subs "
                      f"~{current}, budget {self.budget})")
            time.sleep(2.0)

    def record(self, subs_sent: int) -> None:
        self._events.append((time.monotonic(), subs_sent))


def estimate_subs(rows_written: int) -> int:
    """Rough upper-bound estimate: each strike row can carry a call and a
    put symbol (2 dx_symbols), each subscribed to 4 event types."""
    return rows_written * 2 * 4


def already_scanned_today(out_dir: Path, symbol: str) -> bool:
    chain_files = list(out_dir.glob(f"{symbol}_full_chain_*.parquet"))
    return len(chain_files) > 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                     help="Re-pull symbols even if already scanned today")
    ap.add_argument("--tickers", default=None,
                     help="Comma-separated ticker subset, overrides the "
                          "Stage 2-derived universe")
    args = ap.parse_args()

    cfg = load_config()
    proj = Path(cfg["project_root"])
    ttc, pull_equity = load_pipeline_modules(cfg["tastytrade_pipeline_dir"])

    dte_lo = cfg["stage3_thresholds"]["dte_min"]
    dte_hi = cfg["stage3_thresholds"]["dte_max"]
    dte_token = f"between_{dte_lo}_{dte_hi}_dte"

    budget = cfg.get("stage3_subs_per_minute_budget", 8000)
    limiter = RollingSubRateLimiter(budget_per_min=budget)

    out_dir = proj / "data" / "stage3_chains" / date.today().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)

    log = logging.getLogger("stage3_scan")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    tickers = load_stage3_universe(proj, args.tickers)
    log.info(f"Stage 3 scan: {len(tickers)} tickers, DTE window "
             f"{dte_lo}-{dte_hi}, rate budget {budget} subs/min")
    log.info(f"Output: {out_dir}")

    ttc.get_access_token(force=True)

    results = []
    t0 = time.time()
    for i, symbol in enumerate(tickers, 1):
        if not args.force and already_scanned_today(out_dir, symbol):
            log.info(f"[{i}/{len(tickers)}] {symbol}: already scanned today, skipping")
            continue

        ts = datetime.now().strftime("%H%M%S")
        try:
            # Conservative pre-estimate before we know the real row count,
            # just to avoid firing into an already-saturated window; the
            # limiter re-checks actual usage after the pull via record().
            limiter.wait_for_budget(upcoming_subs=3500, log=log)
            stats = pull_equity(symbol, [dte_token], out_dir, ts, log)
            subs_est = estimate_subs(stats.get("rows", 0))
            limiter.record(subs_est)
            results.append({"ticker": symbol, "status": "ok", **stats})
        except Exception as e:
            log.error(f"[{i}/{len(tickers)}] {symbol}: FAILED ({e})")
            results.append({"ticker": symbol, "status": "error", "error": str(e)})

        if i % 25 == 0:
            elapsed = time.time() - t0
            rate = i / elapsed * 60
            remaining = (len(tickers) - i) / max(rate, 0.01)
            log.info(f"  ... {i}/{len(tickers)} done, "
                     f"~{remaining:.0f} min remaining at current pace")

    elapsed_min = (time.time() - t0) / 60
    ok = sum(1 for r in results if r["status"] == "ok")
    err = sum(1 for r in results if r["status"] == "error")
    log.info(f"\nDone in {elapsed_min:.1f} min. {ok} ok, {err} failed, "
             f"{len(tickers) - len(results)} skipped (already scanned).")

    log_path = proj / "output" / "stage3_scan_log.csv"
    pd.DataFrame(results).to_csv(log_path, index=False)
    log.info(f"Per-symbol scan log: {log_path}")
    log.info(f"Next: python scripts\\05_stage3_screen.py")


if __name__ == "__main__":
    main()
