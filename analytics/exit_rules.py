"""
Trade management: when to hold, close, roll, or accept assignment.

This module exists because you asked for best-practice rules and the honest
answer is that the well-known ones do not apply to your book.

WHY THE FOLKLORE DOESN'T TRANSFER
---------------------------------
The canonical short-premium rules -- enter at 45 DTE, manage winners at 50% of
max profit, roll at 21 DTE -- come from studies run on ~45 DTE entries. Two of
the three are meaningless at 7 DTE:

* **"Roll at 21 DTE"** is unreachable. You are *born* inside 21 DTE. The rule
  exists to escape the gamma zone; a weekly seller lives there permanently and
  has to manage it with strike selection and size instead.

* **"Manage winners at 50%"** assumes you can redeploy the freed capital
  immediately at the same rate. On a fixed weekly cadence you cannot -- if you
  close Wednesday and re-enter Friday, you have earned half the premium over
  the same seven days. Closing at 50% is then strictly worse. Run the numbers
  in `docs/` or reproduce them with `costs.csp_economics`.

* **Costs bite differently.** Opening one contract costs $1.12 and closing it
  costs another $0.12. Against a 45-DTE $1.50 credit that is 0.8%. Against a
  7-DTE $0.20 credit it is 6.2% -- before the trade moves at all.

THE RULE THIS USES INSTEAD
--------------------------
Your entry price is a sunk cost. The only question at any moment is whether
the market's price for the *remaining* risk is above or below your empirical
estimate of that risk:

    hold  while   mark + close_fee  >  P(assign) x E[shortfall] x spot
    close when    it is not

That formulation has three properties a percentage target does not. It never
references how much profit you are up, so it cannot be anchored by a good or
bad entry. It closes automatically for pennies near expiry, because the market
stops paying for risk that has essentially resolved. And it fires early and
loudly when a stock moves against you, which is the one moment a fixed profit
target says nothing at all.

Everything here is parameterised in `config.yaml` under `management:` and is
meant to be swept, not trusted. These are starting values.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd

from analytics import costs, moves
from core.paths import load_config


class Action(str, Enum):
    HOLD = "hold"
    CLOSE = "close"
    ROLL = "roll"
    ACCEPT_ASSIGNMENT = "accept_assignment"


@dataclass(frozen=True)
class Decision:
    action: Action
    urgency: str                  # 'routine' | 'attention' | 'act_now'
    headline: str
    rationale: str
    edge_per_share: float | None = None
    prob_assignment: float | None = None
    numbers: dict | None = None

    def to_dict(self) -> dict:
        return {"action": self.action.value, "urgency": self.urgency,
                "headline": self.headline, "rationale": self.rationale,
                "edge_per_share": self.edge_per_share,
                "prob_assignment": self.prob_assignment,
                **(self.numbers or {})}


@dataclass(frozen=True)
class OpenPut:
    ticker: str
    strike: float
    contracts: int
    entry_credit: float           # per share, at entry
    spot: float
    current_mark: float           # per share, to buy it back now
    trading_days_left: int
    current_delta: float | None = None
    rolls_used: int = 0
    earnings_before_expiry: bool = False


def evaluate_short_put(position: OpenPut, daily: pd.DataFrame,
                        current_rv: float | None = None) -> Decision:
    """Decide what to do with one open short put, right now."""
    cfg = load_config().get("management", {})
    exit_cfg = cfg.get("exit", {})
    def_cfg = cfg.get("defense", {})

    close_fee_per_share = costs.option_close(position.contracts, "buy").total \
        / (100.0 * max(position.contracts, 1))
    cost_to_close = position.current_mark + close_fee_per_share

    # --- 1. Hard defensive triggers, checked before any economics ----------
    breached = position.spot < position.strike
    deep = (position.current_delta is not None
            and position.current_delta <= def_cfg.get("roll_when_delta_beyond", -0.45))
    min_dte = def_cfg.get("min_dte_to_roll", 2)

    if (breached or deep) and position.trading_days_left < min_dte:
        return Decision(
            Action.ACCEPT_ASSIGNMENT, "act_now",
            f"{position.ticker}: in the money with {position.trading_days_left}d left",
            f"Spot ${position.spot:.2f} against a ${position.strike:.2f} strike and only "
            f"{position.trading_days_left} session(s) remaining. There is not enough time "
            f"left to roll for a worthwhile credit. Take the shares at ${position.strike:.2f} "
            f"-- effective basis ${position.strike - position.entry_credit:.2f} after the "
            f"premium you kept -- and start writing calls against them.",
            prob_assignment=1.0 if breached else None)

    if (breached or deep) and position.rolls_used >= def_cfg.get("max_rolls_per_cycle", 2):
        return Decision(
            Action.ACCEPT_ASSIGNMENT, "act_now",
            f"{position.ticker}: roll limit reached",
            f"Already rolled {position.rolls_used} time(s). Rolling again converts a wheel "
            f"into an open-ended commitment to a falling stock -- each roll collects less "
            f"and pushes the problem further out. Take assignment and let the covered-call "
            f"side do its job.")

    # --- 2. Economic test: is the remaining risk fairly priced? ------------
    empirical = None
    if position.trading_days_left > 0 and not daily.empty:
        empirical = moves.breach_probabilities(
            daily, position.ticker, position.spot, position.strike,
            position.trading_days_left, lookback_years=10,
            vol_conditioned=True, current_rv=current_rv, min_observations=40)

    if empirical is not None:
        expected_intrinsic = (empirical.prob_terminal
                              * empirical.expected_loss_if_breached
                              * position.spot)
        edge = cost_to_close - expected_intrinsic
        numbers = {
            "cost_to_close": round(cost_to_close, 4),
            "expected_intrinsic": round(expected_intrinsic, 4),
            "prob_touch": empirical.prob_touch,
            "sample": empirical.sample_label,
            "effective_n": empirical.effective_n,
        }

        if edge < 0:
            urgency = "act_now" if breached else "attention"
            return Decision(
                Action.ROLL if position.trading_days_left >= min_dte else Action.CLOSE,
                urgency,
                f"{position.ticker}: remaining risk is underpriced",
                f"Buying this back costs ${cost_to_close:.2f}/share, but on comparable "
                f"history the option is worth ${expected_intrinsic:.2f} of terminal "
                f"intrinsic ({empirical.prob_terminal:.0%} assignment odds over the next "
                f"{position.trading_days_left} session(s)). You are being paid less than "
                f"the risk is worth. "
                + ("Roll out for a credit if one is available; otherwise close."
                   if position.trading_days_left >= min_dte else "Close it."),
                edge_per_share=edge,
                prob_assignment=empirical.prob_terminal, numbers=numbers)

        # Nothing left to earn: the market has stopped paying for resolved risk.
        floor = exit_cfg.get("close_when_remaining_extrinsic_below", 0.05)
        if position.current_mark <= floor:
            gain = (position.entry_credit - position.current_mark) * 100 * position.contracts \
                   - costs.option_open(position.contracts, "sell").total \
                   - costs.option_close(position.contracts, "buy").total
            return Decision(
                Action.CLOSE, "routine",
                f"{position.ticker}: take it off, nothing left in it",
                f"Mark is ${position.current_mark:.2f} with {position.trading_days_left} "
                f"session(s) left -- ${gain:,.2f} net locked in. The remaining "
                f"${position.current_mark:.2f} is not worth carrying gap risk over, and "
                f"closing frees ${position.strike * 100 * position.contracts:,.0f} of "
                f"collateral for the next cycle.",
                edge_per_share=edge, prob_assignment=empirical.prob_terminal,
                numbers=numbers)

        # A positive edge is a reason to hold, not a reason to stop watching.
        # Escalate visibility when the position is genuinely at risk, so a
        # 31%-assignment trade never sits in the same visual bucket as a
        # 3%-assignment one just because both say "hold".
        if breached or deep or empirical.prob_terminal >= 0.25:
            watch = "attention"
            prefix = (f"At risk ({empirical.prob_terminal:.0%} assignment odds"
                      + (f", delta {position.current_delta:.2f}" if position.current_delta else "")
                      + "), but still overpaid. ")
        else:
            watch, prefix = "routine", ""

        return Decision(
            Action.HOLD, watch,
            f"{position.ticker}: hold" + (" - watch" if watch == "attention" else ""),
            prefix
            + f"Cost to close is ${cost_to_close:.2f}/share against ${expected_intrinsic:.2f} "
            f"of expected terminal intrinsic -- ${edge:.2f}/share of edge still in the "
            f"trade, {empirical.prob_terminal:.0%} assignment odds over "
            f"{position.trading_days_left} session(s). Closing here would forfeit "
            f"${edge * 100 * position.contracts:,.0f} of positive expectancy to avoid risk "
            f"you are still being overpaid to carry.",
            edge_per_share=edge, prob_assignment=empirical.prob_terminal,
            numbers=numbers)

    # --- 3. Fallback when history is too thin for an empirical estimate ----
    floor = exit_cfg.get("close_when_remaining_extrinsic_below", 0.05)
    if position.current_mark <= floor:
        return Decision(
            Action.CLOSE, "routine", f"{position.ticker}: close for pennies",
            f"Mark ${position.current_mark:.2f}. Not enough price history to compute "
            f"empirical odds, but there is nothing left to earn here.")
    if breached:
        return Decision(
            Action.ROLL, "act_now", f"{position.ticker}: in the money",
            f"Spot ${position.spot:.2f} is below the ${position.strike:.2f} strike with "
            f"{position.trading_days_left} session(s) left, and there is insufficient "
            f"history for an empirical read. Roll for a credit or accept the shares.")
    return Decision(
        Action.HOLD, "routine", f"{position.ticker}: hold",
        f"Out of the money at ${position.spot:.2f} against ${position.strike:.2f}. "
        f"Insufficient history for an empirical estimate -- managing on structure alone.")


# --- Entry-side gate -------------------------------------------------------

@dataclass(frozen=True)
class EntryVerdict:
    accepted: bool
    reasons: tuple[str, ...]
    warnings: tuple[str, ...] = ()


def screen_entry(credit: float, strike: float, contracts: int, dte: int,
                  iv_rv_ratio: float | None = None,
                  earnings_before_expiry: bool = False,
                  prob_otm_empirical: float | None = None,
                  dte_window: tuple[int, int] | None = None) -> EntryVerdict:
    """Apply the hard entry gates before a candidate is ever ranked.

    These are rejections, not score penalties: a trade that fails any of them
    is not a worse trade, it is a trade you should not take. Keeping them out
    of the composite score means a high yield can never buy its way past an
    earnings print or a 12% cost drag.
    """
    cfg = load_config().get("management", {}).get("entry", {})
    reasons, warnings = [], []

    min_credit = cfg.get("min_credit_per_contract", 0.15)
    if credit < min_credit:
        reasons.append(
            f"credit ${credit:.2f} is below the ${min_credit:.2f} floor -- "
            f"fees would consume too much of it")

    econ = costs.csp_economics(strike, credit, contracts, dte, outcome="expire")
    max_drag = cfg.get("max_cost_drag", 0.10)
    if econ.cost_drag_pct > max_drag:
        reasons.append(
            f"fees are {econ.cost_drag_pct:.1%} of gross credit "
            f"(limit {max_drag:.0%}) -- ${econ.entry_fees:.2f} on ${econ.gross_credit:.2f}")

    if earnings_before_expiry and cfg.get("block_if_earnings_before_expiry", True):
        reasons.append("earnings report falls before expiration")

    min_ratio = cfg.get("min_iv_rv_ratio", 1.05)
    if iv_rv_ratio is not None and iv_rv_ratio < min_ratio:
        reasons.append(
            f"IV/RV is {iv_rv_ratio:.2f}, below the {min_ratio:.2f} floor -- "
            f"implied vol is not priced above what the stock has been delivering")

    if prob_otm_empirical is not None and prob_otm_empirical < 0.70:
        warnings.append(
            f"empirical odds of finishing OTM are only {prob_otm_empirical:.0%} -- "
            f"size this as a position you are content to be assigned in")

    lo, hi = dte_window or (cfg.get("dte_min", 5), cfg.get("dte_max", 10))
    if not (lo <= dte <= hi):
        warnings.append(f"{dte} DTE sits outside the {lo}-{hi} target window")

    return EntryVerdict(not reasons, tuple(reasons), tuple(warnings))


def recommended_starting_rules() -> dict:
    """The rule set the config ships with, and why each value is what it is.

    Surfaced in the app so the rules are visible and arguable rather than
    buried in a YAML file.
    """
    return {
        "entry": {
            "7 DTE target, 5-10 acceptable":
                "Matches your stated cadence. Weekly expiries are the most liquid "
                "short-dated contracts and let the wheel turn 52 times a year.",
            "20-delta target, 12-30 band":
                "Slightly further out than the classic 30-delta because at 7 DTE "
                "gamma is unforgiving and you have no time to repair a bad strike.",
            "minimum $0.15 credit":
                "Below this, the $1.12 opening cost is over 7% of gross. There is "
                "no strategy that survives that drag repeated weekly.",
            "IV/RV at least 1.05":
                "Refuses to sell volatility for less than the stock has been "
                "delivering. This is the economic basis of the whole strategy.",
            "no earnings before expiry":
                "The one large risk that is fully known in advance. A yield-weighted "
                "score actively rewards pre-earnings IV, so this has to be a gate "
                "rather than a penalty.",
        },
        "exit": {
            "no fixed profit target":
                "Hold while the market pays more for the remaining risk than history "
                "says it is worth. At a weekly cadence, closing at 50% earns half the "
                "premium over the same seven days.",
            "close under $0.05 remaining":
                "Falls out of the same test -- the market stops paying for resolved "
                "risk, so carrying overnight gap exposure for a nickel is a bad trade.",
        },
        "defense": {
            "roll when delta passes -0.45 or price breaks the strike":
                "Delta is the earlier signal; a price break is the confirmation.",
            "credit-only rolls":
                "A debit roll is paying to postpone. If no credit is available at an "
                "acceptable strike, the trade is telling you to take the shares.",
            "at most 2 rolls, then accept assignment":
                "Unbounded rolling is how a wheel becomes a bag hold. Assignment is "
                "the strategy working, not the strategy failing.",
            "under 2 sessions left, stop rolling":
                "There is no time value left to harvest and rolls price badly.",
        },
    }
