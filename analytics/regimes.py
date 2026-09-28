"""
Regime-split scorecard -- does a name work everywhere, or did one bull run
carry it?

The method is ported from your own `build_regime_scorecard.py`, which asked
the question for ORB day-trades. It matters more for a wheel, because a wheel
holds through the bad periods rather than exiting them: an assigned position in
October 2008 is not closed at a stop, it is *owned* for the next eighteen
months while you write calls into a falling market.

WHY AN AGGREGATE NUMBER CANNOT ANSWER THIS
------------------------------------------
Two tickers both show 17% annualised over fifteen years. One earned it steadily
across nine very different environments. The other was flat-to-negative in
eight of them and made everything in 2020-2021. Those are not similar
candidates, and no aggregate statistic distinguishes them -- but splitting the
history at regime boundaries does, immediately.

The output is a consistency score, and the components behind it so you can
re-rank by your own priorities rather than trusting a composite you did not
choose.

REGIME BOUNDARIES
-----------------
Fixed calendar windows, chosen for being *recognisable* rather than fitted.
Dating regimes from the data would be circular -- you would be splitting the
history at the points where the strategy's behaviour changed, then reporting
that its behaviour differs across them.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd

from analytics.wheel_backtest import WheelParams, run_wheel


@dataclass(frozen=True)
class Regime:
    key: str
    label: str
    start: dt.date
    end: dt.date
    character: str

    def contains(self, when) -> bool:
        day = pd.Timestamp(when).date()
        return self.start <= day <= self.end


REGIMES: list[Regime] = [
    Regime("pre_gfc", "2006-07 pre-crisis", dt.date(2006, 1, 1), dt.date(2007, 9, 30),
           "late-cycle calm, low realized vol"),
    Regime("gfc", "2008 financial crisis", dt.date(2007, 10, 1), dt.date(2009, 3, 31),
           "the stress case: sustained decline, vol regime shift"),
    Regime("recovery", "2009-2014 recovery", dt.date(2009, 4, 1), dt.date(2014, 12, 31),
           "grinding uptrend, falling vol"),
    Regime("mid_2010s", "2015-2017 range", dt.date(2015, 1, 1), dt.date(2017, 12, 31),
           "two vol shocks inside a broadly flat market"),
    Regime("vol_2018", "2018 volmageddon and Q4", dt.date(2018, 1, 1), dt.date(2018, 12, 31),
           "two sharp drawdowns in one year"),
    Regime("pre_covid", "2019 melt-up", dt.date(2019, 1, 1), dt.date(2020, 1, 31),
           "low-vol uptrend"),
    Regime("covid", "2020 COVID crash and rebound", dt.date(2020, 2, 1), dt.date(2020, 12, 31),
           "fastest drawdown on record, then the fastest recovery"),
    Regime("stimulus", "2021 stimulus bull", dt.date(2021, 1, 1), dt.date(2021, 12, 31),
           "relentless uptrend, retail vol bid"),
    Regime("bear_2022", "2022 rate shock", dt.date(2022, 1, 1), dt.date(2022, 12, 31),
           "sustained decline without a vol spike"),
    Regime("modern", "2023-present", dt.date(2023, 1, 1), dt.date(2100, 1, 1),
           "the current environment"),
]

STRESS_REGIMES = {"gfc", "vol_2018", "covid", "bear_2022"}


def regime_for(when) -> Regime | None:
    for regime in REGIMES:
        if regime.contains(when):
            return regime
    return None


# --- Per-ticker scorecard --------------------------------------------------

def scorecard(daily: pd.DataFrame, ticker: str,
               params: WheelParams | None = None,
               min_cycles: int = 5) -> pd.DataFrame:
    """Run the wheel once, then split the resulting cycles by regime.

    One simulation, sliced afterwards -- not one simulation per regime. Running
    each window independently would restart the wheel at every boundary and
    discard exactly the cycles that straddle them, which are the interesting
    ones: an assignment in February 2020 that resolves in August is the whole
    point of the exercise.
    """
    params = params or WheelParams()
    result = run_wheel(daily, ticker, params)
    if result.cycles.empty:
        return pd.DataFrame()

    cycles = result.cycles.copy()
    cycles["regime"] = [
        (regime_for(d).key if regime_for(d) else "unknown")
        for d in cycles["start_date"]
    ]

    rows = []
    for key, group in cycles.groupby("regime"):
        if len(group) < min_cycles:
            continue
        capital_days = float(group["capital_days"].sum())
        pnl = float(group["net_pnl"].sum())
        cumulative = group["net_pnl"].cumsum()
        rows.append({
            "ticker": ticker,
            "regime": key,
            "label": next((r.label for r in REGIMES if r.key == key), key),
            "stress": key in STRESS_REGIMES,
            "n_cycles": int(len(group)),
            "annualised": pnl / capital_days * 252.0 if capital_days else float("nan"),
            "total_pnl": pnl,
            "pct_profitable": float((group["net_pnl"] > 0).mean()),
            "assignment_rate": float(group["assigned"].mean()),
            "mean_cycle_days": float(group["trading_days"].mean()),
            "worst_cycle_days": int(group["trading_days"].max()),
            "worst_cycle_pnl": float(group["net_pnl"].min()),
            "drawdown": float((cumulative - cumulative.cummax()).min()),
        })
    return pd.DataFrame(rows)


# --- Consistency -----------------------------------------------------------

def consistency(detail: pd.DataFrame, min_regimes: int = 4) -> pd.DataFrame:
    """Collapse per-regime rows into one row per ticker.

    The composite is deliberately simple and its components are all reported,
    because the right weighting depends on what you want the book to do. A
    weighting is an opinion; presenting one without its inputs is an opinion
    disguised as a measurement.
    """
    if detail.empty:
        return pd.DataFrame()

    rows = []
    for ticker, group in detail.groupby("ticker"):
        if group["regime"].nunique() < min_regimes:
            continue
        returns = group["annualised"]
        stress = group[group["stress"]]
        positive = int((returns > 0).sum())

        rows.append({
            "ticker": ticker,
            "regimes_covered": int(group["regime"].nunique()),
            "regimes_positive": positive,
            "positive_rate": positive / len(group),
            "median_annualised": float(returns.median()),
            "worst_regime_annualised": float(returns.min()),
            "worst_regime": group.loc[returns.idxmin(), "label"],
            "best_regime_annualised": float(returns.max()),
            "dispersion": float(returns.std()),
            # Does it survive the environments that matter? A wheel is judged
            # in the drawdowns, because that is when it is holding stock.
            "stress_regimes": int(len(stress)),
            "stress_median": float(stress["annualised"].median())
            if len(stress) else float("nan"),
            "stress_positive_rate": float((stress["annualised"] > 0).mean())
            if len(stress) else float("nan"),
            "worst_cycle_days": int(group["worst_cycle_days"].max()),
            "total_cycles": int(group["n_cycles"].sum()),
        })

    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame

    # Composite: reward breadth of positive regimes and stress survival,
    # penalise dispersion. Level enters through the median, but deliberately
    # does not dominate -- a high average earned in one regime is the exact
    # pattern this is built to catch.
    frame["consistency_score"] = (
        0.35 * frame["positive_rate"]
        + 0.30 * frame["stress_positive_rate"].fillna(0.0)
        + 0.20 * _scale(frame["median_annualised"])
        + 0.15 * (1.0 - _scale(frame["dispersion"]))
    )
    frame["tier"] = pd.cut(
        frame["consistency_score"], bins=[-np.inf, 0.40, 0.60, 0.75, np.inf],
        labels=["avoid", "satellite", "solid", "core"])
    return frame.sort_values("consistency_score", ascending=False).reset_index(drop=True)


def _scale(series: pd.Series) -> pd.Series:
    """Min-max to 0-1 within the cohort. Returns 0.5 when everything ties."""
    clean = series.replace([np.inf, -np.inf], np.nan)
    low, high = clean.min(), clean.max()
    if not np.isfinite(low) or not np.isfinite(high) or high <= low:
        return pd.Series(0.5, index=series.index)
    return ((clean - low) / (high - low)).fillna(0.5)


def build(tickers: list[str] | None = None, params: WheelParams | None = None,
           years: int = 20, reporter=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Full scorecard across the universe. Returns (detail, consistency)."""
    from core.paths import load_universe
    from core.progress import NullReporter
    from data_sources.yfinance_sync import load_daily

    tickers = tickers or load_universe()
    reporter = reporter or NullReporter()
    frames = []

    with reporter.stage("regimes", "Regime scorecard", total=len(tickers)):
        for ticker in tickers:
            try:
                daily = load_daily(ticker, basis="price", with_dividends=True)
                if daily.empty:
                    reporter.advance(1, note=f"{ticker} no history")
                    continue
                cutoff = daily["date"].max() - pd.DateOffset(years=years)
                daily = daily[daily["date"] >= cutoff]
                table = scorecard(daily, ticker, params)
                if not table.empty:
                    frames.append(table)
                    reporter.advance(1, note=f"{ticker} {len(table)} regime(s)")
                else:
                    reporter.advance(1, note=f"{ticker} too few cycles")
            except Exception as exc:
                reporter.advance(1, note=f"{ticker} error")
                reporter.log(f"{ticker}: {type(exc).__name__}: {exc}")

    if not frames:
        return pd.DataFrame(), pd.DataFrame()
    detail = pd.concat(frames, ignore_index=True)
    return detail, consistency(detail)
