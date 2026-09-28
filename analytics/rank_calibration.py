"""
Calibration of the underlying-ranking weights (Phase 14).

Phase 11 shipped the ranking weights as a starting point, "not validated
until Phase 14". This measures, for every component that can be rebuilt
POINT-IN-TIME from daily bars, whether a higher score on the entry date went
with a better short-put outcome afterwards -- across names on the same date,
which is the ranker's actual job (it orders names, it does not time them).

SETUP
-----
    entries    one shared calendar -- every `horizon`-th session over the last
               `years` years -- so names are compared on the same dates and
               outcomes never overlap; every registry symbol with bars
    trade      a synthetic put 1 expected move below spot, EM = spot x IV x
               sqrt(h/252) with IV = 20-day RV x `vol_premium` (the backtest
               proxy); credit by Black-Scholes at that IV
    outcome    premium kept = (credit - intrinsic at expiry) / credit, held to
               expiry: 1 = kept it all, negative = lost more than collected.
               Every strike sits 1 EM out, so this is vol-neutral. The return
               on strike (`outcome_strike`) is kept for reference but NOT
               ranked on: with a fixed vol premium, a higher-vol name earns
               more per dollar of strike by construction, so any component
               correlated with vol level (drawdown!) would look predictive.
    metric     per date, Spearman rank correlation (IC) between a component's
               score and the outcome across names; then the mean IC over
               dates, its t-statistic (dates are independent: outcomes do not
               overlap) and the top-minus-bottom tercile spread

COMPONENTS AND HOW EACH IS REBUILT (scored by underlying_rank's own functions)
------------------------------------------------------------------------------
    trend      trend_state.classify on the bars up to the entry -- exact
    drawdown   max drawdown over stage1 drawdown_lookback_years -- exact
    support    PROXY: nearest daily MA (21/50/100/200 EMA/SMA) below spot, in
               EM units, scored with support_em_band. The live component uses
               only STRONG levels, and strength comes from a full-history
               study, which would leak the future into the past.
    iv_rank    PROXY: percentile of 20-day RV within its trailing year (there
               is no stored IV history before 2026)
    iv_rv      NOT TESTABLE: with IV proxied from RV the ratio is a constant
    liquidity  NOT TESTABLE point-in-time: only today's TastyTrade rating exists

LIMITS, STATED UP FRONT
-----------------------
The trade is priced at a fixed volatility premium, so the outcome measures
"did the name move less than its recent realised vol implied" -- the part of
the premium the ranker can plausibly forecast. It cannot credit a component
for finding RICH implied vol, which is what iv_rank and iv_rv are for; their
weights stay judgement calls.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from analytics import underlying_rank as ur
from core.paths import load_config

TESTABLE = ("trend", "drawdown", "support", "iv_rank")
UNTESTABLE = {"iv_rv": "IV proxied from RV makes the ratio constant",
              "liquidity": "only today's rating exists (no point-in-time history)"}
MA_COLUMNS = [f"{k}_{n}" for k in ("ema", "sma") for n in (21, 50, 100, 200)]


@dataclass
class Settings:
    horizon: int = 21                 # trading days to expiry
    years: int = 8
    vol_premium: float = 1.15
    rv_window: int = 20
    min_names: int = 10               # names needed on a date to compute an IC
    rate: float = 0.045

    @property
    def calendar_days(self) -> int:
        return int(round(self.horizon * 365 / 252))


def _rolling_max_drawdown(close: np.ndarray, idx: int, lookback: int) -> float:
    window = close[max(idx - lookback, 0): idx + 1]
    peak = np.maximum.accumulate(window)
    return float(np.min(window / peak - 1.0))


def entry_calendar(reference: pd.DataFrame, s: Settings) -> set:
    """Every `horizon`-th session of `reference` (SPY) inside the window."""
    dates = pd.to_datetime(reference["date"]).sort_values().reset_index(drop=True)
    dates = dates[dates >= dates.max() - pd.DateOffset(years=s.years)]
    return set(dates.iloc[::s.horizon])


def symbol_panel(symbol: str, daily: pd.DataFrame, s: Settings,
                 entries: set | None = None) -> pd.DataFrame:
    """One row per entry date: component scores and the synthetic outcome."""
    from analytics import indicators, trend_state
    from analytics.options_math import bs_price_greeks

    if daily is None or len(daily) < 300 + s.horizon:
        return pd.DataFrame()
    cfg = load_config()
    rank_cfg = cfg.get("underlying_rank", {}) or {}
    dd_years = (cfg.get("stage1_thresholds") or {}).get("drawdown_lookback_years") or 10
    band = tuple(rank_cfg.get("support_em_band", [0.5, 2.0]))
    trend_table = rank_cfg.get("trend_scores", {"uptrend": 1.0, "range": 0.6, "downtrend": 0.1})
    dd_floor = float(rank_cfg.get("drawdown_floor", -0.65))

    frame = indicators.compute(daily.sort_values("date").reset_index(drop=True))
    frame["trend"] = trend_state.classify(frame)
    close = frame["close"].to_numpy(float)
    log_ret = np.log(frame["close"]).diff()
    rv = (log_ret.rolling(s.rv_window).std() * math.sqrt(252)).to_numpy(float)
    rv_pct = pd.Series(rv).rolling(252, min_periods=200).rank(pct=True).to_numpy(float)
    dates = pd.to_datetime(frame["date"])

    start = dates.max() - pd.DateOffset(years=s.years)
    first = max(int(np.searchsorted(dates.to_numpy(), start.to_datetime64())), 260)
    rows = []
    if entries is None:
        indices = range(first, len(frame) - s.horizon, s.horizon)
    else:
        indices = [i for i in range(first, len(frame) - s.horizon) if dates.iloc[i] in entries]
    for i in indices:
        spot, vol = close[i], rv[i]
        if not np.isfinite(vol) or vol <= 0:
            continue
        iv = vol * s.vol_premium
        em = spot * iv * math.sqrt(s.horizon / 252)
        strike = spot - em
        credit = bs_price_greeks(spot, strike, s.calendar_days, iv, s.rate, "put").price
        terminal = close[i + s.horizon]
        pnl = credit - max(strike - terminal, 0.0)

        levels = [frame.at[i, c] for c in MA_COLUMNS if c in frame]
        below = [lv for lv in levels if np.isfinite(lv) and lv < spot]
        distance = (spot - max(below)) / em if below else None
        rows.append({
            "date": dates.iloc[i], "symbol": symbol,
            "trend": ur.score_trend(frame.at[i, "trend"], trend_table),
            "drawdown": ur.score_drawdown(
                _rolling_max_drawdown(close, i, int(dd_years * 252)), dd_floor),
            "support": ur.score_support(distance, band, studied=True),
            "iv_rank": ur.score_iv_rank(rv_pct[i] if np.isfinite(rv_pct[i]) else None, None),
            "outcome": pnl / credit if credit > 0 else np.nan,
            "outcome_strike": pnl / strike, "breached": terminal < strike,
        })
    return pd.DataFrame(rows)


def panel(symbols: list[str], s: Settings, loader=None, reference: str = "SPY") -> pd.DataFrame:
    if loader is None:
        from data_sources.yfinance_sync import load_daily

        def loader(sym):
            return load_daily(sym, basis="price")
    try:
        entries = entry_calendar(loader(reference), s)
    except Exception:
        entries = None
    frames = []
    for symbol in symbols:
        try:
            part = symbol_panel(symbol, loader(symbol), s, entries)
        except Exception:
            continue
        if not part.empty:
            frames.append(part)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def composite_scores(frame: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    """The ranker's composite over the testable components (renormalised)."""
    usable = {k: v for k, v in weights.items() if k in TESTABLE and v > 0}
    return frame.apply(lambda r: ur.composite({k: (None if pd.isna(r[k]) else r[k])
                                               for k in usable}, usable)[0], axis=1)


def information_coefficients(frame: pd.DataFrame, columns: list[str],
                             min_names: int = 10) -> pd.DataFrame:
    """Mean cross-sectional Spearman IC per column, t-stat, tercile spread."""
    out = []
    for column in columns:
        ics, spreads = [], []
        for _, day in frame.groupby("date"):
            day = day.dropna(subset=[column, "outcome"])
            if (len(day) < min_names or day[column].nunique() < 2
                    or day["outcome"].nunique() < 2):
                continue
            ics.append(day[column].rank().corr(day["outcome"].rank()))
            lo, hi = day[column].quantile([1 / 3, 2 / 3])
            top, bottom = day[day[column] >= hi], day[day[column] <= lo]
            if len(top) and len(bottom):
                spreads.append(top["outcome"].mean() - bottom["outcome"].mean())
        ics = np.array([x for x in ics if np.isfinite(x)])
        n = len(ics)
        mean = float(ics.mean()) if n else float("nan")
        sd = float(ics.std(ddof=1)) if n > 1 else float("nan")
        out.append({"component": column, "dates": n, "mean_ic": mean,
                    "t_stat": (mean / sd * math.sqrt(n) if sd > 0
                               else math.copysign(math.inf, mean)) if n > 1 else float("nan"),
                    "hit_rate": float((ics > 0).mean()) if n else float("nan"),
                    "tercile_spread": float(np.mean(spreads)) if spreads else float("nan")})
    return pd.DataFrame(out)


def suggested_weights(ic: pd.DataFrame, current: dict[str, float],
                      t_min: float = 2.0) -> dict[str, float]:
    """Keep the untestable components' current share of weight; split the
    testable share in proportion to positive mean IC among components with
    t >= t_min. A component with no significant positive IC gets 0."""
    testable_share = sum(current.get(k, 0.0) for k in TESTABLE)
    total = sum(current.values()) or 1.0
    good = ic[(ic["component"].isin(TESTABLE)) & (ic["t_stat"] >= t_min) & (ic["mean_ic"] > 0)]
    out = {k: current.get(k, 0.0) / total for k in current if k not in TESTABLE}
    if good.empty:
        out.update({k: 0.0 for k in TESTABLE})
        leftover = testable_share / total
        rest = sum(out.values()) or 1.0
        out = {k: v + leftover * v / rest for k, v in out.items()}
    else:
        strength = good.set_index("component")["mean_ic"]
        for k in TESTABLE:
            out[k] = float(strength.get(k, 0.0) / strength.sum() * testable_share / total)
    return {k: round(out.get(k, 0.0), 3) for k in ur.COMPONENTS}


def run(symbols: list[str], s: Settings | None = None, loader=None) -> dict:
    from core import user_settings
    s = s or Settings()
    frame = panel(symbols, s, loader)
    if frame.empty:
        return {"panel": frame, "ic": pd.DataFrame(), "presets": pd.DataFrame(),
                "settings": s}
    presets = user_settings.weight_presets()
    for name, weights in presets.items():
        frame[f"preset:{name}"] = composite_scores(frame, weights)
    columns = list(TESTABLE) + [f"preset:{n}" for n in presets]
    ic = information_coefficients(frame, columns, s.min_names)
    return {"panel": frame, "ic": ic, "settings": s,
            "suggested": suggested_weights(
                ic, presets.get(user_settings.default_weight_preset(),
                                next(iter(presets.values()))))}
