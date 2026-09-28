"""
Walk-forward validation -- the check that stops a sweep from lying to you.

THE PROBLEM THIS SOLVES
-----------------------
`sweep()` tries 36 parameter combinations and reports the best. With 36 draws
from a noisy process, *something* will look excellent whether or not any real
edge separates the cells. Adopting that winner and quoting its return is the
single most common way to build a system that backtests beautifully and loses
money -- and it is completely invisible from inside the sweep, because the
sweep has no way to know it is measuring luck.

Walk-forward answers it directly. Fit on a window, score on the period *after*
that window, roll forward, repeat. The parameters are always chosen using data
that existed before the period they are judged on, so the out-of-sample number
is what the rule would actually have delivered.

THE NUMBER THAT MATTERS
-----------------------
**Degradation**: in-sample return minus out-of-sample return. A well-behaved
strategy loses a little; a curve-fit loses most of it, and sometimes goes
negative. Anything above roughly a third of the in-sample figure means the
sweep is selecting noise and the "optimum" should be ignored.

The second output matters just as much: whether re-optimising each fold beats
simply fixing one sensible parameter set forever. Usually it does not. If
adaptive selection cannot beat a static baseline out of sample, the honest
conclusion is that the parameters barely matter and you should stop tuning
them -- which is a genuinely useful thing to learn, and a sweep alone can
never tell you.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from analytics.wheel_backtest import WheelParams, run_wheel

METRIC = "annualised_on_capital_deployed"


@dataclass
class Fold:
    index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    chosen_put_delta: float
    chosen_put_dte: int
    chosen_call_delta: float
    in_sample: float
    out_of_sample: float
    baseline_out_of_sample: float
    test_cycles: int

    @property
    def degradation(self) -> float:
        return self.in_sample - self.out_of_sample

    @property
    def beat_baseline(self) -> bool:
        return self.out_of_sample > self.baseline_out_of_sample


def _slice(daily: pd.DataFrame, start, end) -> pd.DataFrame:
    frame = daily.copy()
    frame.columns = [str(c).lower() for c in frame.columns]
    if "date" not in frame.columns:
        frame = frame.reset_index().rename(columns={frame.index.name or "index": "date"})
        frame.columns = [str(c).lower() for c in frame.columns]
    frame["date"] = pd.to_datetime(frame["date"])
    return frame[(frame["date"] >= start) & (frame["date"] < end)].reset_index(drop=True)


def _score(daily: pd.DataFrame, ticker: str, params: WheelParams) -> tuple[float, int]:
    result = run_wheel(daily, ticker, params)
    if result.summary.get("error"):
        return float("nan"), 0
    return (float(result.summary.get(METRIC, float("nan"))),
            int(result.summary.get("n_cycles", 0)))


def walk_forward(daily: pd.DataFrame, ticker: str,
                  train_years: float = 5.0, test_years: float = 1.0,
                  step_years: float = 1.0,
                  put_deltas=(-0.15, -0.20, -0.25, -0.30),
                  put_dtes=(5, 7, 10),
                  call_deltas=(0.20, 0.25, 0.30),
                  baseline: WheelParams | None = None,
                  min_test_cycles: int = 8,
                  reporter=None) -> dict:
    """Roll a train/test window through history, re-optimising each fold."""
    baseline = baseline or WheelParams()
    frame = daily.copy()
    frame.columns = [str(c).lower() for c in frame.columns]
    if "date" not in frame.columns:
        frame = frame.reset_index()
        frame.columns = [str(c).lower() for c in frame.columns]
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.sort_values("date")

    first, last = frame["date"].min(), frame["date"].max()
    train_span = pd.DateOffset(months=int(train_years * 12))
    test_span = pd.DateOffset(months=int(test_years * 12))
    step = pd.DateOffset(months=int(step_years * 12))

    folds: list[Fold] = []
    train_start = first
    index = 0

    while True:
        train_end = train_start + train_span
        test_end = train_end + test_span
        if test_end > last:
            break

        train = _slice(frame, train_start, train_end)
        test = _slice(frame, train_end, test_end)
        if train.empty or test.empty:
            break

        # Choose parameters using ONLY the training window.
        best, best_score = None, -np.inf
        for put_delta in put_deltas:
            for put_dte in put_dtes:
                for call_delta in call_deltas:
                    params = WheelParams(**{**asdict(baseline),
                                             "put_delta": put_delta,
                                             "put_dte": put_dte,
                                             "call_delta": call_delta})
                    score, cycles = _score(train, ticker, params)
                    if np.isfinite(score) and cycles >= min_test_cycles and score > best_score:
                        best, best_score = params, score

        if best is None:
            train_start = train_start + step
            index += 1
            continue

        oos, cycles = _score(test, ticker, best)
        base_oos, _ = _score(test, ticker, baseline)

        if cycles >= min_test_cycles and np.isfinite(oos):
            folds.append(Fold(
                index=index,
                train_start=str(train_start.date()), train_end=str(train_end.date()),
                test_start=str(train_end.date()), test_end=str(test_end.date()),
                chosen_put_delta=best.put_delta, chosen_put_dte=best.put_dte,
                chosen_call_delta=best.call_delta,
                in_sample=best_score, out_of_sample=oos,
                baseline_out_of_sample=base_oos if np.isfinite(base_oos) else float("nan"),
                test_cycles=cycles))
            if reporter:
                reporter.advance(1, note=f"{ticker} fold {index}: "
                                          f"IS {best_score:.1%} -> OOS {oos:.1%}")

        train_start = train_start + step
        index += 1

    return _summarise(ticker, folds, baseline)


def _summarise(ticker: str, folds: list[Fold], baseline: WheelParams) -> dict:
    if not folds:
        return {"ticker": ticker, "n_folds": 0,
                "verdict": "not enough history for a walk-forward test"}

    table = pd.DataFrame([asdict(f) for f in folds])
    table["degradation"] = table["in_sample"] - table["out_of_sample"]
    table["beat_baseline"] = table["out_of_sample"] > table["baseline_out_of_sample"]

    mean_is = float(table["in_sample"].mean())
    mean_oos = float(table["out_of_sample"].mean())
    mean_base = float(table["baseline_out_of_sample"].mean())
    degradation = mean_is - mean_oos
    ratio = degradation / abs(mean_is) if mean_is else float("nan")

    # How often did the optimiser even pick the same thing twice? Unstable
    # parameter selection across folds is itself evidence that the grid is
    # measuring noise -- a real edge would keep choosing the same region.
    stability = float(table.groupby(
        ["chosen_put_delta", "chosen_put_dte", "chosen_call_delta"]
    ).size().max() / len(table))

    if not np.isfinite(ratio):
        verdict = "inconclusive"
    elif ratio > 0.60:
        verdict = ("SEVERE degradation -- the sweep is selecting noise. Ignore the "
                   "in-sample optimum entirely and use a fixed, defensible setting.")
    elif ratio > 0.33:
        verdict = ("Substantial degradation. Some edge survives, but the in-sample "
                   "figure is not achievable. Size expectations to the out-of-sample "
                   "number.")
    elif ratio > 0.15:
        verdict = "Mild degradation, within the range a real edge shows."
    else:
        verdict = "Holds up out of sample."

    beat_rate = float(table["beat_baseline"].mean())
    if beat_rate < 0.5:
        adaptive = (f"Re-optimising each fold beat a fixed baseline only "
                    f"{beat_rate:.0%} of the time. The tuning is not earning its "
                    f"keep -- pick one sensible setting and leave it alone.")
    elif mean_oos - mean_base < 0.01:
        adaptive = (f"Adaptive selection wins {beat_rate:.0%} of folds but by "
                    f"{mean_oos - mean_base:.1%} on average. Not worth the machinery.")
    else:
        adaptive = (f"Adaptive selection beat the baseline in {beat_rate:.0%} of folds, "
                    f"by {mean_oos - mean_base:.1%} annualised on average.")

    return {
        "ticker": ticker, "n_folds": len(folds),
        "mean_in_sample": mean_is, "mean_out_of_sample": mean_oos,
        "mean_baseline_oos": mean_base,
        "degradation": degradation, "degradation_ratio": ratio,
        "oos_positive_rate": float((table["out_of_sample"] > 0).mean()),
        "parameter_stability": stability,
        "beat_baseline_rate": beat_rate,
        "baseline": f"{baseline.put_delta:.2f}d / {baseline.put_dte}d / "
                    f"{baseline.call_delta:.2f}c",
        "verdict": verdict, "adaptive_verdict": adaptive,
        "folds": table,
    }


def across_universe(tickers: list[str] | None = None, years: int = 20,
                     baseline: WheelParams | None = None,
                     reporter=None) -> tuple[pd.DataFrame, dict]:
    """Walk-forward every ticker and pool the verdict.

    One ticker's degradation is itself a noisy estimate. Pooling across the
    universe is what makes the answer trustworthy -- and it is the same logic
    that makes the cross-sectional sweep more informative than a single-name
    one.
    """
    from core.paths import load_universe
    from core.progress import NullReporter
    from data_sources.yfinance_sync import load_daily

    tickers = tickers or load_universe()
    reporter = reporter or NullReporter()
    rows, fold_frames = [], []

    with reporter.stage("walkforward", "Walk-forward validation", total=len(tickers)):
        for ticker in tickers:
            try:
                daily = load_daily(ticker, basis="price", with_dividends=True)
                if daily.empty:
                    reporter.advance(1, note=f"{ticker} no history")
                    continue
                cutoff = daily["date"].max() - pd.DateOffset(years=years)
                daily = daily[daily["date"] >= cutoff]
                result = walk_forward(daily, ticker, baseline=baseline)
                if result.get("n_folds", 0) == 0:
                    reporter.advance(1, note=f"{ticker} too little history")
                    continue
                folds = result.pop("folds")
                folds["ticker"] = ticker
                fold_frames.append(folds)
                rows.append(result)
                reporter.advance(1, note=f"{ticker} {result['n_folds']} folds, "
                                          f"OOS {result['mean_out_of_sample']:.1%}")
            except Exception as exc:
                reporter.advance(1, note=f"{ticker} error")
                reporter.log(f"{ticker}: {type(exc).__name__}: {exc}")

    if not rows:
        return pd.DataFrame(), {"verdict": "no usable tickers"}

    summary = pd.DataFrame(rows)
    pooled = {
        "tickers": int(len(summary)),
        "total_folds": int(summary["n_folds"].sum()),
        "mean_in_sample": float(summary["mean_in_sample"].mean()),
        "mean_out_of_sample": float(summary["mean_out_of_sample"].mean()),
        "mean_baseline_oos": float(summary["mean_baseline_oos"].mean()),
        "median_degradation_ratio": float(summary["degradation_ratio"].median()),
        "tickers_positive_oos": int((summary["mean_out_of_sample"] > 0).sum()),
        "mean_parameter_stability": float(summary["parameter_stability"].mean()),
        "adaptive_beat_rate": float(summary["beat_baseline_rate"].mean()),
    }
    ratio = pooled["median_degradation_ratio"]
    pooled["verdict"] = (
        "Pooled degradation is severe -- treat every sweep optimum as noise."
        if ratio > 0.60 else
        "Pooled degradation is substantial -- expect the out-of-sample number."
        if ratio > 0.33 else
        "Pooled results hold up out of sample.")
    return summary.sort_values("mean_out_of_sample", ascending=False), pooled
