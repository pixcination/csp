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


def outcomes(closed: pd.DataFrame) -> pd.Series:
    """1 when a closed position finished profitable, per share before fees.

    A CSP's POP claim is about assignment, so `assigned` is the loss and
    expiry the win. Otherwise -- a spread, or any early close or roll -- the
    outcome is whether the net debit paid back was below the credit taken
    in. (Before Phase 15 every `closed_early` CSP counted as a win, even one
    bought back at a loss.)
    """
    fill = closed["actual_fill"].fillna(closed["modelled_fill"]).astype(float)
    exit_price = closed["exit_price"].fillna(0.0).astype(float)
    won = (fill - exit_price > 0).astype(float)
    won[closed["status"] == "assigned"] = 0.0
    won[closed["status"] == "expired_otm"] = 1.0
    # A spread assigned into shares made money at expiry if it settled above
    # its breakeven (short strike - credit): that is what its POP claimed.
    if "settlement_price" in closed:
        spread = (closed["status"] == "assigned") & (closed["strategy"].fillna("csp") == "pcs") \
            & closed["settlement_price"].notna()
        won[spread] = (closed.loc[spread, "settlement_price"].astype(float)
                       > closed.loc[spread, "strike"].astype(float) - fill[spread]).astype(float)
    return won


def probability_calibration(strategy: str | None = None) -> Reliability | None:
    """Reliability of the engine's POP claims against the paper book.

    The claim is the recommendation's empirical `prob_otm` (a CSP's P(expire
    OTM), a spread's P(finish above breakeven)); `strategy` restricts it to
    one strategy, None pools them.
    """
    positions = paper.list_positions()
    if positions.empty:
        return None
    closed = positions[positions["status"] != "open"].copy()
    closed = closed[closed["rec_prob_otm"].notna()]
    if strategy:
        closed = closed[closed["strategy"].fillna("csp") == strategy]
    if closed.empty:
        return None
    return reliability_curve(closed["rec_prob_otm"].astype(float), outcomes(closed))


def by_strategy() -> dict[str, dict]:
    """POP reliability per strategy -- a spread and a put are different
    claims, and pooling them can hide an error in either."""
    positions = paper.list_positions()
    out = {}
    if positions.empty:
        return out
    for name in sorted(positions["strategy"].fillna("csp").unique()):
        rel = probability_calibration(name)
        if rel is not None:
            out[name] = rel.to_dict()
    return out


# --- P(reach X% of max profit) ---------------------------------------------

def target_outcomes() -> pd.DataFrame:
    """One row per closed position x profit target it carried a prediction for.

        hit       the best profit seen (marks and the exit) reached X% of max
        miss      it did not, AND the position was held to expiry, so the
                  whole path is known
        censored  closed early or rolled without reaching X: the rest of the
                  path was never seen, so it is neither -- excluded

    Marks are sampled (once per pipeline run, plus any you record), so a hit
    between marks can be missed: the observed rate is a LOWER bound, and a
    model that looks slightly optimistic here may be right.
    """
    positions = paper.list_positions()
    if positions.empty:
        return pd.DataFrame()
    closed = positions[positions["status"] != "open"]
    preds = paper.list_predictions()
    if closed.empty or preds.empty:
        return pd.DataFrame()
    preds = preds[preds["metric"].str.fullmatch(r"p_hit_\d+") & (preds["model"] == "blend")]
    rows = []
    for _, pos in closed.iterrows():
        seen = pos.get("max_profit_pct_seen")
        seen = float(seen) if seen is not None and pd.notna(seen) else None
        held = pos["status"] in paper.HELD_TO_EXPIRY
        for _, p in preds[preds["position_id"] == pos["id"]].iterrows():
            target = int(p["metric"].split("_")[-1])
            if target >= 100:
                # "100%" is P(expire worthless): judged only at expiry.
                result = ("hit" if pos["status"] == "expired_otm" else
                          "miss" if held else "censored")
            elif seen is not None and seen >= target / 100.0 - 1e-9:
                result = "hit"
            else:
                result = "miss" if held else "censored"
            rows.append({"position_id": int(pos["id"]), "ticker": pos["ticker"],
                         "strategy": pos.get("strategy") or "csp", "target": target,
                         "predicted": float(p["value"]), "result": result,
                         "observed": {"hit": 1.0, "miss": 0.0}.get(result)})
    return pd.DataFrame(rows)


def target_calibration() -> dict:
    """Predicted vs observed P(reach X%) per target, censored rows excluded."""
    frame = target_outcomes()
    if frame.empty:
        return {"n": 0, "table": pd.DataFrame(), "reliability": {},
                "verdict": "no closed positions with stored P(reach X%) predictions yet"}
    scored = frame[frame["result"] != "censored"]
    table = frame.groupby("target").agg(
        n=("result", "size"), censored=("result", lambda r: int((r == "censored").sum())))
    if not scored.empty:
        table = table.join(scored.groupby("target").agg(
            scored=("observed", "size"), predicted=("predicted", "mean"),
            observed=("observed", "mean")))
        table["gap"] = table["observed"] - table["predicted"]
    reliability = {}
    for target, group in scored.groupby("target"):
        rel = reliability_curve(group["predicted"], group["observed"])
        if rel is not None:
            reliability[int(target)] = rel.to_dict()
    n = int(len(scored))
    censored = int((frame["result"] == "censored").sum())
    enough = n and scored.groupby("target").size().max() >= MIN_FOR_VERDICT
    verdict = (f"{n} scored target predictions, {censored} censored. "
               + ("Observed rates are lower bounds: marks are sampled, so a touch of the "
                  "target between marks goes unseen." if enough else
                  f"Too few to judge: {MIN_FOR_VERDICT} per target is the minimum."))
    return {"n": n, "table": table.reset_index(), "reliability": reliability,
            "verdict": verdict}


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


def _package_quote(frame: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """(net mid, summed half-spread) per position: the stored package quote,
    else the single leg's bid/ask for rows recorded before Phase 15."""
    nan = pd.Series(np.nan, index=frame.index, dtype=float)
    mid = frame["quote_net_mid"].astype(float) if "quote_net_mid" in frame else nan.copy()
    half = (frame["quote_half_spread"].astype(float) if "quote_half_spread" in frame
            else nan.copy())
    if {"quote_bid", "quote_ask"}.issubset(frame.columns):
        bid, ask = frame["quote_bid"].astype(float), frame["quote_ask"].astype(float)
        single = (frame["strategy"].fillna("csp") != "pcs" if "strategy" in frame
                  else pd.Series(True, index=frame.index))
        legacy = single & mid.isna() & bid.notna() & ask.notna() & (ask > bid)
        mid = mid.where(~legacy, (bid + ask) / 2.0)
        half = half.where(~legacy, (ask - bid) / 2.0)
    return mid, half


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
    # A spread is judged against its PACKAGE quote (net mid, summed
    # half-spreads -- `costs.package_fill`), stored at entry since Phase 15.
    implied = None
    mid, half = _package_quote(real)
    usable = mid.notna() & half.notna() & (half > 0)
    if usable.any():
        fractions = ((mid[usable] - real.loc[usable, "actual_fill"].astype(float))
                     / half[usable]).dropna()
        if len(fractions):
            # Median, not mean: one fill on an unusually wide market would
            # otherwise dominate a small sample.
            implied = float(np.clip(fractions.median(), -0.5, 1.5))

    mean_slip = float(slippage.mean())
    recommendation = None
    quoted_n = int(usable.sum())
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
                   + (f"fraction toward {recommendation:.0%} would make them honest."
                      if recommendation is not None else "fraction would make them honest."))
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
        "by_strategy": by_strategy(),
        "targets": target_calibration(),
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
