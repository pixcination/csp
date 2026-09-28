"""
Stage 1 screen on yfinance data -- `scripts/02_stage1_screen.py` without the
1-minute archive (Phase 9).

Same thresholds (`config.yaml -> stage1_thresholds`), same metrics, same
hard reasons and tiers, computed from `daily_bars_raw` on the price basis
(the archive `scripts/02` read is split-adjusted, not dividend-adjusted, so
this is the like-for-like basis). The result is written onto the universe
registry as `stage1_pass` / `stage1_tier` / `stage1_reasons`, so the universe
can be re-screened without `D:\\pricing_data` connected.

Differences from scripts/02, deliberately:
* Drawdown is measured over `stage1_thresholds.drawdown_lookback_years`
  (default 10), not all history. Measured on the first run: over Yahoo's full
  history the unrecovered-drawdown rule rejected C and AIG for 2007 peaks,
  and against the archive scripts/02 read they had *passed* only because
  their 2009/2011 reverse splits were not adjusted there. Neither is
  canonical; a recent-regime window is the useful question. Tier
  classification (history_years) still uses the full series.
* Staleness is measured against the last completed session, not against the
  newest date in the file.
* Indices are not screened (no dollar volume to measure); they report
  `stage1_pass = None`, tier "index".
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.paths import load_config


def metrics(daily: pd.DataFrame, as_of, drawdown_years: float | None = None) -> dict | None:
    """scripts/02's per-ticker metrics for one price-basis daily frame.

    `drawdown_years` limits the window for max drawdown / distance from peak
    (None = all history); history_years always uses the full series."""
    if daily is None or len(daily) < 30:
        return None
    g = daily.sort_values("date").reset_index(drop=True)
    first, last = pd.Timestamp(g["date"].iloc[0]), pd.Timestamp(g["date"].iloc[-1])
    close = g["close"].astype(float)
    dollar_volume = close * g["volume"].astype(float)
    log_ret = np.log(close / close.shift(1)).dropna()
    window = close
    if drawdown_years:
        cutoff = last - pd.DateOffset(years=float(drawdown_years))
        window = close[pd.to_datetime(g["date"]) >= cutoff]
    running_max = window.cummax()
    drawdown = window / running_max - 1.0
    return {
        "history_years": (last - first).days / 365.25,
        "first_date": first.date(), "last_date": last.date(),
        "days_since_last": (pd.Timestamp(as_of) - last).days,
        "adv_90d_dollars": float(dollar_volume.tail(90).mean()),
        "last_price": float(close.iloc[-1]),
        "rv_20d_annualized": float(log_ret.tail(20).std() * np.sqrt(252)) if len(log_ret) >= 20 else np.nan,
        "rv_60d_annualized": float(log_ret.tail(60).std() * np.sqrt(252)) if len(log_ret) >= 60 else np.nan,
        "max_drawdown": float(drawdown.min()),
        "pct_off_peak_now": float(window.iloc[-1] / running_max.iloc[-1] - 1.0),
    }


def classify(m: dict, thr: dict, category: str | None = None) -> tuple[str, list[str]]:
    """(tier, hard_reasons) with scripts/02's exact rules. A broad index ETF
    (`category` broad_index_*) uses `min_rv_broad_index` as its RV floor
    (Phase 17): calm by design, SPY and DIA failed 0.12 in quiet markets."""
    reasons = []
    min_rv = thr["min_rv"]
    if category and str(category).startswith("broad_index") \
            and thr.get("min_rv_broad_index") is not None:
        min_rv = float(thr["min_rv_broad_index"])
    if m["days_since_last"] > thr["max_staleness_days"]:
        reasons.append("stale_data")
    if pd.isna(m["adv_90d_dollars"]) or m["adv_90d_dollars"] < thr["min_adv_dollars"]:
        reasons.append("adv_too_low")
    if not (thr["min_price"] <= m["last_price"] <= thr["max_price"]):
        reasons.append("price_out_of_range")
    rv = m["rv_20d_annualized"]
    if pd.isna(rv) or not (min_rv <= rv <= thr["max_rv"]):
        reasons.append("volatility_out_of_range")
    if (m["max_drawdown"] < thr["max_drawdown_floor"]
            and m["pct_off_peak_now"] < thr["max_drawdown_floor"] * 0.5):
        reasons.append("unrecovered_drawdown")
    if reasons:
        return "rejected", reasons
    if m["history_years"] < thr["min_history_years"]:
        return "tier2_limited_history", reasons
    return "tier1_backtestable", reasons


def screen(symbols: list[str] | None = None, write: bool = True) -> pd.DataFrame:
    """Screen registry symbols; optionally record the result on the registry."""
    from core.freshness import last_completed_session
    from data_sources import universe
    from data_sources.yfinance_sync import load_daily

    thr = load_config()["stage1_thresholds"]
    registry = universe.load(active_only=True).set_index("symbol")
    symbols = symbols or registry.index.tolist()
    as_of = last_completed_session()
    rows = []
    for symbol in symbols:
        row = {"symbol": symbol, "stage1_pass": None, "tier": None, "reasons": ""}
        if symbol in registry.index and registry.loc[symbol, "asset_class"] == "index":
            row.update(tier="index", reasons="not screened: index (no dollar volume)")
            rows.append(row)
            continue
        m = metrics(load_daily(symbol, basis="price"), as_of,
                    thr.get("drawdown_lookback_years"))
        if m is None:
            row.update(stage1_pass=False, tier="rejected", reasons="insufficient_data")
        else:
            category = registry.loc[symbol, "category"] if symbol in registry.index \
                and "category" in registry.columns else None
            tier, reasons = classify(m, thr, category if isinstance(category, str) else None)
            row.update(m)
            row.update(stage1_pass=tier != "rejected", tier=tier, reasons=";".join(reasons))
        rows.append(row)
    frame = pd.DataFrame(rows)
    if write:
        universe.record_stage1(frame)
    return frame
