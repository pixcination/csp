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


# --- Put credit spreads (Phase 15) ------------------------------------------
#
# A spread is managed differently from a weekly CSP, for three reasons:
#
# * It is usually entered at 30-45 DTE, where "close at 50%" does pay: the
#   freed buying power can be redeployed with weeks of theta left, and the
#   close fee is small against a larger credit. Below
#   `prob_engine.auto_hold_max_dte` the CSP logic still holds -- targets are
#   off and the trade is held -- matching the probability engine's headline
#   policy.
# * Its loss is capped but reached quickly once the short strike is through,
#   so a LOSS STOP (close when the loss reaches k x the credit) replaces the
#   CSP's "accept assignment" fallback. Nobody wants shares from a spread.
# * The same net-of-fee rule applies to every early close: a target whose
#   net gain after the close fee is under `min_net_gain_to_close_early` is
#   not worth taking.
#
# The economic test from the CSP carries over: hold while the price of the
# remaining risk (the mark) exceeds its empirical expected terminal value.

@dataclass(frozen=True)
class OpenSpread:
    ticker: str
    short_strike: float
    long_strike: float
    contracts: int
    entry_credit: float            # net, per share
    spot: float
    current_mark: float            # net debit to close now, per share
    calendar_days_left: int
    trading_days_left: int
    entry_dte_calendar: int
    short_delta: float | None = None
    rolls_used: int = 0
    cash_settled: bool = False

    @property
    def width(self) -> float:
        return abs(self.short_strike - self.long_strike)

    @property
    def profit_pct(self) -> float:
        return (self.entry_credit - self.current_mark) / self.entry_credit \
            if self.entry_credit else 0.0


def spread_config() -> dict:
    cfg = load_config()
    spread = dict((cfg.get("management", {}) or {}).get("spread", {}) or {})
    exit_cfg = (cfg.get("management", {}) or {}).get("exit", {}) or {}
    engine = cfg.get("prob_engine", {}) or {}
    spread.setdefault("profit_target_pct", 50)
    spread.setdefault("loss_stop_multiple", 2.0)
    spread.setdefault("time_stop_dte", None)
    spread.setdefault("roll_when_short_delta_beyond", -0.45)
    spread.setdefault("roll_when_price_below_short", True)
    spread.setdefault("min_dte_to_roll", 5)
    spread.setdefault("max_rolls", 1)
    spread.setdefault("require_net_credit_to_roll", True)
    spread.setdefault("roll_out_days", [7, 35])
    spread.setdefault("close_when_remaining_value_below", 0.05)
    spread["min_net_gain"] = float(exit_cfg.get("min_net_gain_to_close_early", 5.0))
    spread["hold_max_dte"] = int(engine.get("auto_hold_max_dte", 14))
    return spread


def spread_expected_value(daily: pd.DataFrame, spot: float, short: float, long: float,
                          horizon: int, current_rv: float | None = None) -> dict | None:
    """Empirical expected terminal value of a put spread (per share): the mean
    of clip(short - S_T, 0, width) over the same vol-conditioned windows the
    CSP test uses."""
    if horizon <= 0 or daily is None or daily.empty:
        return None
    selected = moves.select_windows(daily, horizon, lookback_years=10, vol_conditioned=True,
                                    current_rv=current_rv, min_observations=40)
    if selected is None:
        return None
    windows, label = selected
    s_t = spot * (1.0 + windows["terminal_return"].astype(float))
    width = abs(short - long)
    value = (short - s_t).clip(lower=0.0, upper=width)
    return {"expected_value": float(value.mean()),
            "prob_short_itm": float((s_t < short).mean()),
            "prob_max_loss": float((s_t <= long).mean()),
            "sample": label, "effective_n": moves._effective_n(len(windows), horizon)}


def evaluate_put_spread(position: OpenSpread, daily: pd.DataFrame | None = None,
                        current_rv: float | None = None) -> Decision:
    """Decide what to do with one open put credit spread, right now.

    Order: nothing left to earn -> loss stop -> short strike threatened ->
    profit target (net of fees) -> time stop -> economic test -> hold.
    """
    cfg = spread_config()
    n = max(position.contracts, 1)
    close_fees = costs.legs_close([("buy", n), ("sell", n)]).total
    open_fees = costs.legs_open([("sell", n), ("buy", n)]).total
    credit, mark = position.entry_credit, position.current_mark
    pnl_if_closed = (credit - mark) * 100 * n - open_fees - close_fees
    numbers = {"profit_pct": round(position.profit_pct, 4),
               "pnl_if_closed": round(pnl_if_closed, 2),
               "cost_to_close": round(mark + close_fees / (100.0 * n), 4)}
    what = f"{position.ticker} ${position.short_strike:g}/${position.long_strike:g}"
    days = position.calendar_days_left
    can_roll = (days >= cfg["min_dte_to_roll"]
                and position.rolls_used < int(cfg["max_rolls"]))

    # 1. Nothing left in it.
    if mark <= float(cfg["close_when_remaining_value_below"]) and days > 0:
        return Decision(
            Action.CLOSE, "routine", f"{what}: take it off, nothing left in it",
            f"The spread costs ${mark:.2f} to close with {days} day(s) left -- "
            f"${pnl_if_closed:,.2f} net locked in. The remaining ${mark:.2f} is not worth "
            f"carrying gap risk over, and closing frees ${position.width * 100 * n - credit * 100 * n:,.0f} "
            f"of buying power.", numbers=numbers)

    # 2. Loss stop: the loss has reached k x the credit.
    k = cfg.get("loss_stop_multiple")
    if k and mark - credit >= float(k) * credit:
        return Decision(
            Action.ROLL if can_roll else Action.CLOSE, "act_now",
            f"{what}: loss stop hit",
            f"Closing costs ${mark:.2f} against a ${credit:.2f} credit: the loss is "
            f"{(mark - credit) / credit:.1f}x the credit, past the {float(k):g}x stop "
            f"(${pnl_if_closed:,.0f} net if closed now; max loss "
            f"${(position.width - credit) * 100 * n:,.0f}). "
            + ("Roll down and out only if a later expiry pays a net credit for it; "
               "otherwise close." if can_roll else "Close it: the stop exists so the "
                                                   "remaining max loss is never ridden out."),
            numbers=numbers)

    # 3. Short strike threatened.
    breached = cfg.get("roll_when_price_below_short", True) and \
        position.spot < position.short_strike
    deep = (position.short_delta is not None
            and position.short_delta <= float(cfg["roll_when_short_delta_beyond"]))
    if breached or deep:
        why = (f"spot ${position.spot:.2f} is below the ${position.short_strike:g} short strike"
               if breached else f"the short put's delta is {position.short_delta:.2f}")
        if can_roll:
            return Decision(
                Action.ROLL, "act_now" if breached else "attention",
                f"{what}: short strike under threat",
                f"{why.capitalize()} with {days} day(s) left. Roll out in time (same width, "
                f"same or lower strikes) only for a net credit -- a debit roll pays to "
                f"postpone. If none pays, close.", numbers=numbers)
        settle = ("cash-settled, so no shares change hands, but the loss is real"
                  if position.cash_settled else
                  "an American equity short put this deep can be assigned early")
        return Decision(
            Action.CLOSE, "act_now", f"{what}: close, no roll left",
            f"{why.capitalize()} with {days} day(s) left and "
            f"{'no rolls remaining' if position.rolls_used >= int(cfg['max_rolls']) else 'too little time to roll'}. "
            f"Close it ({settle}).", numbers=numbers)

    # 4. Profit target, net of fees, only where early closes pay.
    target = cfg.get("profit_target_pct")
    targets_apply = target and position.entry_dte_calendar > cfg["hold_max_dte"]
    if targets_apply and position.profit_pct >= float(target) / 100.0:
        net_gain = pnl_if_closed
        if net_gain >= cfg["min_net_gain"]:
            return Decision(
                Action.CLOSE, "routine", f"{what}: profit target reached",
                f"{position.profit_pct:.0%} of max profit captured (target {target}%): "
                f"${net_gain:,.2f} net of every fee. Close and free "
                f"${(position.width - credit) * 100 * n:,.0f} of buying power for a fresh "
                f"trade with its theta still ahead of it.", numbers=numbers)
        numbers["below_min_gain"] = True
        return Decision(
            Action.HOLD, "routine", f"{what}: target reached, but not worth the fee",
            f"{position.profit_pct:.0%} of max profit, but closing nets only "
            f"${net_gain:,.2f} after fees, under the ${cfg['min_net_gain']:.2f} minimum. "
            f"Hold.", numbers=numbers)

    # 5. Time stop.
    stop = cfg.get("time_stop_dte")
    if stop and position.entry_dte_calendar > int(stop) and days <= int(stop):
        return Decision(
            Action.CLOSE, "attention", f"{what}: time stop ({stop} DTE)",
            f"{days} day(s) left: the trade has entered the gamma zone the {stop}-DTE stop "
            f"exists to avoid. {position.profit_pct:.0%} of max profit so far, "
            f"${pnl_if_closed:,.2f} net if closed now.", numbers=numbers)

    # 6. Economic test.
    empirical = None
    if daily is not None and position.trading_days_left > 0:
        empirical = spread_expected_value(daily, position.spot, position.short_strike,
                                          position.long_strike, position.trading_days_left,
                                          current_rv)
    if empirical:
        edge = mark - empirical["expected_value"]
        numbers.update({"expected_value": round(empirical["expected_value"], 4),
                        "prob_max_loss": empirical["prob_max_loss"],
                        "sample": empirical["sample"],
                        "effective_n": empirical["effective_n"]})
        if edge < 0:
            return Decision(
                Action.CLOSE, "attention", f"{what}: remaining risk is underpriced",
                f"Buying the spread back costs ${mark:.2f}, but on comparable history it "
                f"is worth ${empirical['expected_value']:.2f} at expiry "
                f"({empirical['prob_short_itm']:.0%} short-ITM odds). The market is "
                f"paying less than the risk is worth: close.",
                edge_per_share=edge, prob_assignment=empirical["prob_short_itm"],
                numbers=numbers)
        return Decision(
            Action.HOLD, "routine", f"{what}: hold",
            f"{position.profit_pct:.0%} of max profit so far. The ${mark:.2f} to close is "
            f"${edge:.2f}/share above the ${empirical['expected_value']:.2f} expected "
            f"terminal value, so the remaining risk is still overpaid.",
            edge_per_share=edge, prob_assignment=empirical["prob_short_itm"],
            numbers=numbers)

    return Decision(
        Action.HOLD, "routine", f"{what}: hold",
        f"{position.profit_pct:.0%} of max profit with {days} day(s) left; spot "
        f"${position.spot:.2f} above the ${position.short_strike:g} short strike. "
        f"No rule has fired.", numbers=numbers)


def spread_roll_candidates(chain: pd.DataFrame, position: OpenSpread,
                           expiration, today=None, limit: int = 5) -> pd.DataFrame:
    """Rolls for a put spread: later expirations (within `roll_out_days`),
    the same width, short strike at or below the current one, priced at the
    modelled package fill. `net` is per share after closing the current
    spread at its mark and paying both sides' fees; with
    `require_net_credit_to_roll` only net credits are returned. Sorted by
    the lowest short strike that still pays, then the nearest expiry."""
    import datetime as dt
    cfg = spread_config()
    if chain is None or chain.empty:
        return pd.DataFrame()
    today = today or dt.date.today()
    frame = chain.copy()
    frame["expiration"] = pd.to_datetime(frame["expiration"]).dt.date
    frame["strike_price"] = frame["strike_price"].astype(float)
    current = pd.Timestamp(expiration).date()
    lo, hi = (list(cfg["roll_out_days"]) + [35])[:2]
    n = max(position.contracts, 1)
    fees = (costs.legs_close([("buy", n), ("sell", n)]).total
            + costs.legs_open([("sell", n), ("buy", n)]).total) / (100.0 * n)
    rows = []
    for exp, group in frame.groupby("expiration"):
        gap = (exp - current).days
        if gap < lo or gap > hi:
            continue
        by_strike = group.set_index("strike_price")
        for short in sorted(by_strike.index, reverse=True):
            if short > position.short_strike:
                continue
            long = short - position.width
            if long not in by_strike.index:
                continue
            s, l = by_strike.loc[short], by_strike.loc[long]
            if isinstance(s, pd.DataFrame):
                s = s.iloc[0]
            if isinstance(l, pd.DataFrame):
                l = l.iloc[0]
            fill = costs.package_fill(s.get("put_bid"), s.get("put_ask"),
                                      l.get("put_bid"), l.get("put_ask"))
            if fill is None or fill["modelled"] <= 0:
                continue
            net = fill["modelled"] - position.current_mark - fees
            rows.append({"expiration": exp, "dte": (exp - today).days,
                         "short_strike": short, "long_strike": long,
                         "new_credit": fill["modelled"], "close_debit": position.current_mark,
                         "net": round(net, 4), "short_delta": s.get("put_delta")})
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    if cfg["require_net_credit_to_roll"]:
        out = out[out["net"] > 0]
    return out.sort_values(["short_strike", "expiration"], ascending=[True, True]) \
        .head(limit).reset_index(drop=True)


# --- Strategy-spec positions (Phase 16) -----------------------------------------
#
# A position opened from a strategy spec (condor, calendar, diagonal...) is
# managed by the spec's own `exit` block, with the same order and the same
# net-of-fee rule as a spread. Profit is measured against the position's max
# profit, and a loss stop against the credit taken in -- or, for a debit
# trade, against the debit paid (a calendar's `loss_stop_multiple: 0.5`
# closes when half the debit is gone).

@dataclass(frozen=True)
class OpenSpecPosition:
    ticker: str
    label: str
    contracts: int
    entry_credit: float            # net per share; negative for a debit
    current_mark: float            # net debit to close now, per share (negative = you receive)
    max_profit: float              # per share
    calendar_days_left: int        # to the front expiry
    entry_dte_calendar: int
    legs: tuple = ()               # ((side, qty), ...) for the close fees

    @property
    def pnl(self) -> float:
        return self.entry_credit - self.current_mark

    @property
    def profit_pct(self) -> float:
        return self.pnl / self.max_profit if self.max_profit else 0.0


def evaluate_spec_position(position: OpenSpecPosition, exit_cfg: dict) -> Decision:
    """Floor -> loss stop -> profit target (net of fees) -> time stop -> hold."""
    n = max(position.contracts, 1)
    legs = position.legs or (("short", 1),)
    close_fees = costs.legs_close([("buy" if s == "short" else "sell", n * q)
                                   for s, q in legs]).total
    open_fees = costs.legs_open([("sell" if s == "short" else "buy", n * q)
                                 for s, q in legs]).total
    net_if_closed = position.pnl * 100 * n - open_fees - close_fees
    min_gain = float(((load_config().get("management", {}) or {}).get("exit", {}) or {})
                     .get("min_net_gain_to_close_early", 5.0))
    numbers = {"profit_pct": round(position.profit_pct, 4),
               "pnl_if_closed": round(net_if_closed, 2)}
    what = f"{position.ticker} {position.label}"
    days = position.calendar_days_left
    basis = abs(position.entry_credit)

    if position.entry_credit > 0 and position.current_mark <= 0.05 and days > 0:
        return Decision(Action.CLOSE, "routine", f"{what}: take it off, nothing left in it",
                        f"Closing costs ${position.current_mark:.2f}/share: "
                        f"${net_if_closed:,.2f} net locked in.", numbers=numbers)
    k = exit_cfg.get("loss_stop_multiple")
    if k and basis and -position.pnl >= float(k) * basis:
        kind = "credit" if position.entry_credit > 0 else "debit"
        return Decision(Action.CLOSE, "act_now", f"{what}: loss stop hit",
                        f"The loss is {-position.pnl / basis:.1f}x the {kind} "
                        f"(${basis:.2f}), past the {float(k):g}x stop: "
                        f"${net_if_closed:,.0f} net if closed now.", numbers=numbers)
    target = exit_cfg.get("profit_target_pct")
    if target and position.profit_pct >= float(target) / 100.0:
        if net_if_closed >= min_gain:
            return Decision(Action.CLOSE, "routine", f"{what}: profit target reached",
                            f"{position.profit_pct:.0%} of max profit (target {target}%): "
                            f"${net_if_closed:,.2f} net of every fee.", numbers=numbers)
        numbers["below_min_gain"] = True
        return Decision(Action.HOLD, "routine", f"{what}: target reached, not worth the fee",
                        f"Closing nets ${net_if_closed:,.2f}, under the ${min_gain:.2f} "
                        f"minimum. Hold.", numbers=numbers)
    stop = exit_cfg.get("time_stop_dte")
    if stop and position.entry_dte_calendar > int(stop) and days <= int(stop):
        return Decision(Action.CLOSE, "attention", f"{what}: time stop ({stop} DTE)",
                        f"{days} day(s) left: {position.profit_pct:.0%} of max profit, "
                        f"${net_if_closed:,.2f} net if closed now.", numbers=numbers)
    return Decision(Action.HOLD, "routine", f"{what}: hold",
                    f"{position.profit_pct:.0%} of max profit with {days} day(s) to the front "
                    f"expiry. No rule has fired.", numbers=numbers)


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
        "spread": {
            "close at 50% of max profit (entries above 14 DTE)":
                "At 30-45 DTE the freed buying power can be redeployed with weeks of "
                "theta left, and the close fee is small against the credit. The same "
                "minimum net gain applies as for a CSP.",
            "loss stop at 2x the credit":
                "A spread's loss is capped but arrives fast once the short strike is "
                "through. The stop keeps a bad trade from being ridden to max loss.",
            "time stop at 21 DTE":
                "The conventional exit before the gamma zone, kept by choice. Its "
                "cost is measured: Phase 13 (lower EV on SPY/QQQ) and the Phase 15 "
                "backtest (roughly half the annualised return at 45 DTE).",
            "roll only for a net credit, at most once":
                "Same logic as the CSP: a debit roll pays to postpone.",
        },
    }
