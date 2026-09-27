"""
Roll ranking -- the direct expression of "avoid assignment where possible".

Assignment is avoided by *managing*, not by selecting. A good strike choice
lowers the odds; a good roll is what you do when the odds went against you
anyway. Until now the tool had nothing to say at that moment.

A roll is two legs at once: buy back the current short option, sell a new one
further out (and usually further down, for a put). What matters is not the new
premium in isolation but the four things together:

    net credit          new premium received minus cost to close the old leg
    new breakeven       where you are now defended to
    additional capital  how many more days the collateral stays committed
    return on that      the credit against the *extra* days, annualised

THE CREDIT-ONLY RULE
--------------------
A debit roll is paying money to postpone a loss. There are narrow cases where
that is right; none of them are "the ranking put it first." So rolls that cost
money are excluded by default, and when the best available roll is a debit the
engine says exactly that -- which is the trade telling you to take assignment.

THE ROLL-COUNT LIMIT
--------------------
Each roll collects less than the last and pushes the problem further out.
Unbounded rolling is how a wheel becomes an open-ended commitment to a falling
stock. After `max_rolls_per_cycle` the engine stops proposing them.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from analytics import costs, moves
from core.market_calendar import ET, trading_days_between
from core.paths import load_config


@dataclass
class RollCandidate:
    ticker: str
    from_expiration: dt.date
    from_strike: float
    to_expiration: dt.date
    to_strike: float
    contracts: int
    spot: float

    cost_to_close: float        # per share, what you pay to buy the old leg back
    new_credit: float           # per share, what the new leg pays
    net_credit_per_share: float
    net_credit_dollars: float   # after both legs' fees
    fees: float

    strike_change: float
    new_breakeven: float
    days_added: int
    trading_days_added: int
    return_on_added_days: float
    annualised_on_added_days: float

    old_prob_assign: float | None
    new_prob_assign: float | None
    risk_reduction: float | None

    new_delta: float | None
    open_interest: float | None
    is_credit: bool
    accepted: bool
    rejections: tuple = ()
    rationale: str = ""

    def to_dict(self) -> dict:
        out = asdict(self)
        out["from_expiration"] = str(self.from_expiration)
        out["to_expiration"] = str(self.to_expiration)
        return out

    @property
    def rank_key(self) -> float:
        """Rank on credit per added capital-day, not on raw credit.

        A $400 credit that commits the collateral for three more weeks is worse
        than a $150 credit that commits it for one, and raw credit cannot see
        the difference.
        """
        if not self.accepted:
            return -1e9
        return self.annualised_on_added_days


def _f(value) -> float | None:
    try:
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return None
        out = float(value)
        return out if np.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def rank_rolls(ticker: str, current_strike: float, current_expiration: dt.date,
                contracts: int, rolls_used: int = 0,
                max_candidates: int = 8,
                allow_debit: bool = False) -> list[RollCandidate]:
    """Every roll available for one open short put, ranked."""
    from data_sources import chains
    from data_sources.yfinance_sync import load_daily_total_return

    cfg = load_config().get("management", {}).get("defense", {})
    chain, under = chains.load_chain(ticker)
    spot = chains.spot_from_underlying(under)
    if chain.empty or not spot:
        return []

    today = dt.datetime.now(ET).date()
    frame = chain.copy()
    frame["expiration"] = pd.to_datetime(frame["expiration"])

    # Cost to close the current leg, from the same snapshot -- both sides of the
    # roll must be priced off one capture or the net credit is fiction.
    current = frame[(frame["expiration"].dt.date == current_expiration)
                     & (frame["strike_price"].astype(float) == float(current_strike))]
    if current.empty:
        return []
    close_price = costs.realistic_fill(
        _f(current.iloc[0].get("put_bid")), _f(current.iloc[0].get("put_ask")),
        _f(current.iloc[0].get("put_mark")), side="buy")
    if close_price is None:
        return []

    if rolls_used >= cfg.get("max_rolls_per_cycle", 2):
        return [RollCandidate(
            ticker=ticker, from_expiration=current_expiration,
            from_strike=current_strike, to_expiration=current_expiration,
            to_strike=current_strike, contracts=contracts, spot=spot,
            cost_to_close=close_price, new_credit=0.0, net_credit_per_share=0.0,
            net_credit_dollars=0.0, fees=0.0, strike_change=0.0,
            new_breakeven=current_strike, days_added=0, trading_days_added=0,
            return_on_added_days=0.0, annualised_on_added_days=0.0,
            old_prob_assign=None, new_prob_assign=None, risk_reduction=None,
            new_delta=None, open_interest=None, is_credit=False, accepted=False,
            rejections=(f"already rolled {rolls_used} time(s) this cycle -- the limit "
                        f"is {cfg.get('max_rolls_per_cycle', 2)}. Rolling again turns "
                        f"a wheel into an open-ended commitment; take assignment and "
                        f"let the covered-call side work.",),
            rationale="Roll limit reached. Accept assignment.")]

    weeks = cfg.get("roll_out_weeks", [1, 2])
    horizon = max(weeks) * 7 + 4
    targets = frame[(frame["expiration"].dt.date > current_expiration)
                     & (frame["expiration"].dt.date
                        <= current_expiration + dt.timedelta(days=horizon))]
    targets = targets[targets["put_delta"].notna()]
    # Roll down or across, never up: raising the strike increases assignment
    # odds, which is the opposite of what a defensive roll is for.
    targets = targets[targets["strike_price"].astype(float) <= float(current_strike)]
    if targets.empty:
        return []

    daily = load_daily_total_return(ticker)
    old_dte_trd = max(trading_days_between(today, current_expiration), 1)
    old_probs = moves.breach_probabilities(daily, ticker, spot, current_strike,
                                            old_dte_trd, lookback_years=10,
                                            vol_conditioned=True, min_observations=40) \
        if not daily.empty else None

    out: list[RollCandidate] = []
    for _, row in targets.iterrows():
        candidate = _build(ticker, row, spot, current_strike, current_expiration,
                            close_price, contracts, daily, old_probs, today,
                            allow_debit)
        if candidate is not None:
            out.append(candidate)

    out.sort(key=lambda c: c.rank_key, reverse=True)
    return out[:max_candidates]


def _build(ticker, row, spot, current_strike, current_expiration, close_price,
            contracts, daily, old_probs, today, allow_debit) -> RollCandidate | None:
    cfg = load_config().get("management", {}).get("defense", {})
    new_strike = float(row["strike_price"])
    new_expiration = pd.Timestamp(row["expiration"]).date()

    new_credit = costs.realistic_fill(_f(row.get("put_bid")), _f(row.get("put_ask")),
                                       _f(row.get("put_mark")), side="sell")
    if new_credit is None or new_credit <= 0:
        return None

    net_per_share = new_credit - close_price
    fees = (costs.option_close(contracts, "buy").total
            + costs.option_open(contracts, "sell").total)
    net_dollars = net_per_share * 100.0 * contracts - fees

    days_added = (new_expiration - current_expiration).days
    trd_added = max(trading_days_between(current_expiration, new_expiration), 1)
    collateral = new_strike * 100.0 * contracts
    return_added = net_dollars / collateral if collateral else float("nan")
    annualised = return_added * (365.0 / max(days_added, 1))

    new_dte_trd = max(trading_days_between(today, new_expiration), 1)
    new_probs = moves.breach_probabilities(daily, ticker, spot, new_strike,
                                            new_dte_trd, lookback_years=10,
                                            vol_conditioned=True, min_observations=40) \
        if not daily.empty else None

    old_p = old_probs.prob_terminal if old_probs else None
    new_p = new_probs.prob_terminal if new_probs else None
    reduction = (old_p - new_p) if (old_p is not None and new_p is not None) else None

    is_credit = net_dollars > 0
    rejections: list[str] = []
    if not is_credit and cfg.get("require_net_credit_to_roll", True) and not allow_debit:
        rejections.append(
            f"debit roll: costs ${abs(net_dollars):,.0f} to move from "
            f"${current_strike:g} to ${new_strike:g}. Paying to postpone.")

    oi = _f(row.get("put_open_interest"))
    min_oi = load_config().get("liquidity_limits", {}).get("min_open_interest", 250)
    if oi is not None and oi < min_oi:
        rejections.append(f"target strike has only {oi:,.0f} open interest")

    accepted = not rejections
    breakeven = new_strike - new_credit

    return RollCandidate(
        ticker=ticker, from_expiration=current_expiration, from_strike=current_strike,
        to_expiration=new_expiration, to_strike=new_strike, contracts=contracts,
        spot=spot, cost_to_close=close_price, new_credit=new_credit,
        net_credit_per_share=net_per_share, net_credit_dollars=net_dollars, fees=fees,
        strike_change=new_strike - current_strike, new_breakeven=breakeven,
        days_added=days_added, trading_days_added=trd_added,
        return_on_added_days=return_added, annualised_on_added_days=annualised,
        old_prob_assign=old_p, new_prob_assign=new_p, risk_reduction=reduction,
        new_delta=_f(row.get("put_delta")), open_interest=oi, is_credit=is_credit,
        accepted=accepted, rejections=tuple(rejections),
        rationale=_rationale(ticker, current_strike, new_strike, new_expiration,
                              net_dollars, days_added, annualised, old_p, new_p,
                              breakeven, is_credit),
    )


def _rationale(ticker, old_strike, new_strike, new_expiration, net, days,
                annualised, old_p, new_p, breakeven, is_credit) -> str:
    direction = ("down" if new_strike < old_strike else "across")
    bits = [f"Roll {ticker} {direction} from ${old_strike:g} to ${new_strike:g}, "
            f"out to {new_expiration} ({days} more days), for a "
            f"{'credit' if is_credit else 'DEBIT'} of ${abs(net):,.0f}."]
    if is_credit:
        bits.append(f"That is {annualised:.1%} annualised on the additional "
                    f"capital-days.")
    if old_p is not None and new_p is not None:
        bits.append(f"Assignment odds move {old_p:.0%} to {new_p:.0%}.")
    bits.append(f"New breakeven ${breakeven:.2f}.")
    return " ".join(bits)


def best_roll_or_accept(ticker: str, strike: float, expiration: dt.date,
                         contracts: int, rolls_used: int = 0) -> dict:
    """The decision, not just the list.

    Returns the best available roll, or a plain statement that assignment is
    the right answer -- which is a legitimate outcome and should read like one
    rather than like a failure.
    """
    options = rank_rolls(ticker, strike, expiration, contracts, rolls_used)
    accepted = [option for option in options if option.accepted]

    if accepted:
        best = accepted[0]
        return {"action": "roll", "candidate": best.to_dict(),
                "alternatives": [o.to_dict() for o in accepted[1:4]],
                "message": best.rationale}

    if options and options[0].rejections and "already rolled" in options[0].rejections[0]:
        return {"action": "accept_assignment", "candidate": None, "alternatives": [],
                "message": options[0].rejections[0]}

    debits = [o for o in options if not o.is_credit]
    if debits:
        cheapest = max(debits, key=lambda o: o.net_credit_dollars)
        return {
            "action": "accept_assignment", "candidate": None,
            "alternatives": [o.to_dict() for o in options[:3]],
            "message": (f"No credit roll is available -- the cheapest is a "
                        f"${abs(cheapest.net_credit_dollars):,.0f} debit to reach "
                        f"${cheapest.to_strike:g} on {cheapest.to_expiration}. "
                        f"That is the trade telling you to take the shares: pay "
                        f"nothing, own the stock at an effective basis you already "
                        f"agreed to, and start writing calls against it."),
        }

    return {"action": "accept_assignment", "candidate": None, "alternatives": [],
            "message": ("No roll targets found in the captured chain. Widen the "
                        "capture window or take assignment.")}
