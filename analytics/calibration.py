"""
Calibration -- is the engine's confidence justified?

Two separate questions, both answered from the paper book:

**Are the probabilities honest?** The engine claims a strike has an 87% chance
of finishing out of the money. Over enough trades, roughly 87% of the trades it
said that about should finish out of the money. If they come in at 70%, the
sampling window or the volatility conditioning is wrong -- and P&L alone will
never tell you, because a long run of luck and a correct model look identical
for a surprisingly long time.

**Is the fill assumption honest?** Every displayed yield rests on giving up 40%
of the half-spread. That number was a guess. Real recorded fills measure it,
and it scales everything.

WHAT MAKES A PROBABILITY MODEL "CALIBRATED"
-------------------------------------------
Not accuracy -- calibration. A model that says 85% and is right 85% of the time
is perfectly calibrated even though it is wrong on one trade in seven. The
reliability curve buckets predictions and compares each bucket's claim against
its realised rate; a calibrated model sits on the diagonal.

The Brier score combines both properties into one number (lower is better), and
its decomposition separates them: **reliability** is calibration error,
**resolution** is how much the predictions actually vary between outcomes. A
model that predicts 85% for everything can be perfectly calibrated and
completely useless -- zero resolution. Both are reported.

Nothing here edits `config.yaml`. It produces evidence and a recommendation;
changing a parameter that affects every future trade should be a decision you
make, not one that happens while you are looking elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from analytics import paper
from core.paths import load_config

MIN_FOR_VERDICT = 25
WIN_STATUSES = {"expired_otm", "closed_early"}


# --- Probability calibration ----------------------------------------------

@dataclass
class Reliability:
    n: int
    buckets: pd.DataFrame
    brier: float
    reliability: float      # calibration error; lower is better
    resolution: float       # discrimination; higher is better
    uncertainty: float      # irreducible, given the base rate
    skill: float            # 1 - brier/uncertainty; >0 beats always-guessing
    mean_predicted: float
    mean_actual: float
    verdict: str

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "buckets"}


def reliability_curve(predicted: pd.Series, outcomes: pd.Series,
                       bins: int = 5) -> Reliability | None:
    """Bucket predictions and compare each bucket's claim to its realised rate."""
    frame = pd.DataFrame({"p": predicted, "y": outcomes}).dropna()
    frame = frame[(frame["p"] >= 0) & (frame["p"] <= 1)]
    if len(frame) < 5:
        return None

    base = float(frame["y"].mean())
    brier = float(((frame["p"] - frame["y"]) ** 2).mean())
    uncertainty = base * (1 - base)

    # Quantile buckets rather than fixed-width: short-put predictions cluster
    # in 0.75-0.95, and fixed bins would leave most of them empty.
    try:
        frame["bucket"] = pd.qcut(frame["p"], q=min(bins, frame["p"].nunique()),
                                   duplicates="drop")
    except ValueError:
        frame["bucket"] = 0

    grouped = frame.groupby("bucket", observed=True).agg(
        n=("y", "size"), predicted=("p", "mean"), actual=("y", "mean"))
    grouped["gap"] = grouped["actual"] - grouped["predicted"]
    grouped = grouped.reset_index()

    weights = grouped["n"] / len(frame)
    rel = float((weights * (grouped["predicted"] - grouped["actual"]) ** 2).sum())
    res = float((weights * (grouped["actual"] - base) ** 2).sum())
    skill = 1.0 - brier / uncertainty if uncertainty > 0 else float("nan")

    mean_p, mean_a = float(frame["p"].mean()), base
    gap = mean_a - mean_p

    if len(frame) < MIN_FOR_VERDICT:
        verdict = (f"{len(frame)} outcomes -- too few to judge. "
                   f"{MIN_FOR_VERDICT} is the minimum, 100 is where it means something.")
    elif abs(gap) <= 0.05:
        verdict = (f"Well calibrated: predicted {mean_p:.0%}, realised {mean_a:.0%}. "
                   f"The empirical engine is telling the truth.")
    elif gap < 0:
        verdict = (f"OPTIMISTIC by {abs(gap):.0%}: predicted {mean_p:.0%}, realised "
                   f"{mean_a:.0%}. The sample the engine draws from is not matching "
                   f"live outcomes. Most likely causes, in order: the volatility "
                   f"conditioning band is too wide, the 10-year lookback spans a "
                   f"regime that no longer applies, or trades are being taken in "
                   f"conditions the sample under-represents.")
    else:
        verdict = (f"Conservative by {gap:.0%}: predicted {mean_p:.0%}, realised "
                   f"{mean_a:.0%}. Not dangerous, but you may be selling further out "
                   f"of the money than the risk justifies and leaving premium behind.")

    if res < 0.005 and len(frame) >= MIN_FOR_VERDICT:
        verdict += (" Resolution is near zero -- the predictions barely vary between "
                    "winners and losers, so they are not adding information even if "
                    "the average is right.")

    return Reliability(len(frame), grouped, brier, rel, res, uncertainty, skill,
                        mean_p, mean_a, verdict)


def probability_calibration() -> Reliability | None:
    """Reliability of the engine's prob-OTM claims against the paper book."""
    positions = paper.list_positions()
    if positions.empty:
        return None
    closed = positions[positions["status"] != "open"].copy()
    closed = closed[closed["rec_prob_otm"].notna()]
    if closed.empty:
        return None
    outcomes = closed["status"].isin(WIN_STATUSES).astype(float)
    return reliability_curve(closed["rec_prob_otm"].astype(float), outcomes)


# --- Fill calibration ------------------------------------------------------

@dataclass
class FillCalibration:
    n: int
    assumed_fraction: float
    implied_fraction: float | None
    mean_slippage: float
    median_slippage: float
    p10_slippage: float
    recommendation: float | None
    verdict: str

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def fill_calibration() -> FillCalibration | None:
    """Infer the real slippage fraction from recorded fills.

    The model assumes a sell fills at `mid - f x half_spread`. Given a recorded
    fill and the quote it was taken against, `f` is recoverable directly. Only
    positions with a genuinely entered fill count -- rows left at the modelled
    price would just return the assumption unchanged.
    """
    positions = paper.list_positions()
    if positions.empty:
        return None
    real = positions[positions["actual_fill"].notna()].copy()
    if real.empty:
        return None

    assumed = float(load_config().get("costs", {}).get(
        "slippage_fraction_of_half_spread", 0.40))
    slippage = (real["actual_fill"].astype(float)
                - real["modelled_fill"].astype(float))

    # With the quote stored, the realised fraction is recoverable exactly:
    #   sell fill = mid - f x half_spread   =>   f = (mid - fill) / half_spread
    # Rows without a stored quote (entered before Phase 5, or on a one-sided
    # market) are skipped rather than guessed at.
    implied = None
    if {"quote_bid", "quote_ask"}.issubset(real.columns):
        quoted = real[real["quote_bid"].notna() & real["quote_ask"].notna()].copy()
        quoted = quoted[quoted["quote_ask"] > quoted["quote_bid"]]
        if not quoted.empty:
            mid = (quoted["quote_bid"].astype(float)
                   + quoted["quote_ask"].astype(float)) / 2.0
            half = (quoted["quote_ask"].astype(float)
                    - quoted["quote_bid"].astype(float)) / 2.0
            fractions = ((mid - quoted["actual_fill"].astype(float))
                         / half.replace(0, np.nan)).dropna()
            if len(fractions):
                # Median, not mean: one fill on an unusually wide market would
                # otherwise dominate a small sample.
                implied = float(np.clip(fractions.median(), -0.5, 1.5))

    mean_slip = float(slippage.mean())
    recommendation = None
    quoted_n = int(real["quote_bid"].notna().sum()) if "quote_bid" in real.columns else 0
    if quoted_n >= MIN_FOR_VERDICT and implied is not None:
        # Move only part of the way toward the measurement: a sample this size
        # is noisy, and over-correcting on it is its own error.
        recommendation = round(float(np.clip(assumed + 0.5 * (implied - assumed),
                                              0.05, 1.0)), 2)

    if quoted_n < MIN_FOR_VERDICT:
        verdict = (f"{len(real)} recorded fill(s), {quoted_n} with a stored quote. "
                   f"Need {MIN_FOR_VERDICT} quoted fills before the fraction is worth "
                   f"acting on; the assumption stays at {assumed:.0%} until then.")
    elif abs(mean_slip) < 0.005:
        verdict = (f"Fills track the model closely (mean {mean_slip:+.3f}). "
                   f"Keep the {assumed:.0%} assumption.")
    elif mean_slip < 0:
        verdict = (f"Fills come in {abs(mean_slip):.3f} BELOW the model on average -- "
                   f"every displayed yield is optimistic. Raising the slippage "
                   f"fraction toward {recommendation:.0%} would make them honest.")
    else:
        verdict = (f"Fills beat the model by {mean_slip:.3f} on average. The "
                   f"{assumed:.0%} assumption is too pessimistic and is suppressing "
                   f"candidates that are actually tradable.")

    return FillCalibration(
        n=len(real), assumed_fraction=assumed, implied_fraction=implied,
        mean_slippage=mean_slip, median_slippage=float(slippage.median()),
        p10_slippage=float(slippage.quantile(0.10)),
        recommendation=recommendation, verdict=verdict)


# --- Combined report -------------------------------------------------------

def report() -> dict:
    """Everything the Validation page needs, plus config recommendations."""
    probability = probability_calibration()
    fills = fill_calibration()
    performance = paper.performance()

    recommendations = []
    if fills and fills.recommendation and abs(
            fills.recommendation - fills.assumed_fraction) >= 0.05:
        recommendations.append({
            "key": "costs.slippage_fraction_of_half_spread",
            "current": fills.assumed_fraction,
            "suggested": fills.recommendation,
            "why": fills.verdict,
            "confidence": "moderate" if fills.n >= 50 else "low",
        })

    if probability and probability.n >= MIN_FOR_VERDICT:
        gap = probability.mean_actual - probability.mean_predicted
        if gap < -0.05:
            recommendations.append({
                "key": "move_analysis.lookback_years / vol conditioning band",
                "current": "10 years, +/-25% RV band",
                "suggested": "5 years, +/-15% RV band",
                "why": ("Predictions are running optimistic. A tighter volatility band "
                        "and a shorter lookback sample conditions closer to today, at "
                        "the cost of a smaller sample -- check `effective_n` stays "
                        "above ~40 after the change."),
                "confidence": "moderate" if probability.n >= 100 else "low",
            })

    return {
        "probability": probability.to_dict() if probability else None,
        "reliability_buckets": probability.buckets if probability else None,
        "fills": fills.to_dict() if fills else None,
        "performance": performance,
        "recommendations": recommendations,
        "ready_for_automation": _automation_gate(probability, fills, performance),
    }


def _automation_gate(probability, fills, performance) -> dict:
    """The explicit checklist standing between here and unattended execution.

    Written as gates rather than advice because the failure mode -- a
    miscalibrated model placing orders every week without anyone checking --
    is quiet, compounding, and only visible in hindsight.
    """
    checks = [
        {"check": "50+ closed paper positions",
         "pass": bool(performance.get("n_closed", 0) >= 50),
         "detail": f"{performance.get('n_closed', 0)} so far"},
        {"check": "Probabilities calibrated within 5 points",
         "pass": bool(probability and probability.n >= MIN_FOR_VERDICT
                      and abs(probability.mean_actual - probability.mean_predicted) <= 0.05),
         "detail": (f"predicted {probability.mean_predicted:.0%}, realised "
                    f"{probability.mean_actual:.0%}" if probability
                    else "no outcomes yet")},
        {"check": "Predictions carry real resolution",
         "pass": bool(probability and probability.resolution >= 0.005),
         "detail": (f"resolution {probability.resolution:.4f}" if probability
                    else "not measurable yet")},
        {"check": "25+ recorded real fills",
         "pass": bool(fills and fills.n >= MIN_FOR_VERDICT),
         "detail": f"{fills.n if fills else 0} so far"},
        {"check": "Fill model within half a cent",
         "pass": bool(fills and fills.n >= MIN_FOR_VERDICT
                      and abs(fills.mean_slippage) < 0.005),
         "detail": (f"mean {fills.mean_slippage:+.3f}" if fills else "no fills yet")},
        {"check": "Walk-forward degradation under one third",
         "pass": None,
         "detail": "run scripts/validate.py --walk-forward"},
    ]
    passed = sum(1 for c in checks if c["pass"] is True)
    total = sum(1 for c in checks if c["pass"] is not None)
    return {
        "checks": checks, "passed": passed, "total": total,
        "ready": passed == total and total > 0,
        "note": ("These are the conditions under which automated execution would be "
                 "defensible. Failing them does not mean the tool is wrong -- it "
                 "means there is not yet evidence that it is right, and an "
                 "unattended system converts that gap into orders."),
    }
