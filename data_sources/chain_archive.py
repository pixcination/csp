"""
Daily chain archive (Phase 18, review C.6): our own option-price history.

TastyTrade has no historical chain API, so every option price in the
backtests is synthetic (Black-Scholes at 20-day RV x 1.15). A daily,
full-universe, strike-filtered snapshot -- about 3 MB -- kept forever is the
cheapest way to replace that: after a few months it gives real IV rank, real
pre/post-earnings IV, skew dynamics and a non-synthetic check of the
probability engine (Phase 21).

`archive()` captures every active registry symbol (indices included) at
`archive.dte_min`-`archive.dte_max` DTE into the current session block
(forcing a fresh pull), then copies each ticker's chain and underlying files
to `data/chain_archive/<date>/` with a `manifest.json`. Meant for 15:45 ET
(`archive.time_et`): late enough to be the day's settled picture, early
enough to be regular-session quotes. Phase 18 runs it by hand (Tracking
page or `scripts/archive_chains.py`); Phase 19 schedules it.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import time
from pathlib import Path

from core.paths import chain_archive_dir, load_config, load_universe
from core.progress import BaseReporter, NullReporter


def archive(tickers: list[str] | None = None, reporter: BaseReporter | None = None,
            now: dt.datetime | None = None) -> dict:
    from core.market_calendar import classify, session_block
    from data_sources import chains

    cfg = load_config().get("archive", {}) or {}
    lo, hi = int(cfg.get("dte_min", 0)), int(cfg.get("dte_max", 60))
    reporter = reporter or NullReporter()
    now = now or dt.datetime.now()
    tickers = tickers or load_universe(scope="all")
    block = session_block(now)
    folder = chain_archive_dir() / now.date().isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    limiter = chains.SubscriptionLimiter(load_config().get("stage3_subs_per_minute_budget", 8000))
    started = time.perf_counter()
    done, failed, rows, subs = [], {}, 0, 0
    with reporter.stage("archive", "Chain archive", total=len(tickers)):
        for ticker in tickers:
            res = chains.capture(ticker, dte_min=lo, dte_max=hi, force=True, limiter=limiter,
                                 reporter=reporter, reference_dte=(lo + hi) / 2)
            if res.error or not res.ref:
                failed[ticker] = res.error or res.reason
                reporter.advance(1, note=f"{ticker}: {failed[ticker][:60]}")
                continue
            for path in (res.ref.chain_path, res.ref.underlying_path):
                shutil.copy2(path, folder / Path(path).name)
            done.append(ticker)
            rows += int(res.rows or res.ref.rows or 0)
            subs += int(res.subscriptions or 0)
            reporter.advance(1, note=f"{ticker}: {res.rows} rows")
    size = sum(p.stat().st_size for p in folder.glob("*.parquet"))
    manifest = {"date": now.date().isoformat(), "block": block,
                "session_state": classify(now).state.value,
                "captured_at": now.isoformat(timespec="seconds"), "dte_window": [lo, hi],
                "tickers": done, "failed": failed, "rows": rows, "subscriptions": subs,
                "bytes": size, "seconds": round(time.perf_counter() - started, 1)}
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def list_archives() -> list[dict]:
    """Every archived day's manifest, newest first."""
    out = []
    for folder in sorted(chain_archive_dir().iterdir(), reverse=True):
        path = folder / "manifest.json"
        if folder.is_dir() and path.exists():
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except ValueError:
                continue
    return out
