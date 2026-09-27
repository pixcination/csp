"""
Covered calls against assigned shares -- the second half of the wheel.

Until now this side existed as a `STRATEGIES` dictionary entry, a config
preset, and an `st.info()` placeholder. Everything below is new, and it needs
no new data: the chain snapshots have carried `call_bid`, `call_ask`,
`call_mark`, `call_open_interest`, `call_iv` and every call Greek since Stage 3
was first written.

THE ONE HARD RULE
-----------------
**Never rank a strike below adjusted basis without saying so.**

Selling a call under your basis converts an unrealised loss into a realised
one, and it does it while the yield column looks great -- because strikes
below basis are closer to the money and pay more. A ranking engine sorted on
premium will put them at the top every time. So sub-basis strikes are excluded
by default and, when explicitly requested, are labelled with the exact dollar
loss being locked in. Escaping a position at a small loss is a legitimate
choice; making it by accident because a number sorted well is not.

WHAT "GOOD" MEANS ON THIS SIDE
------------------------------
Different from the put side. Being called away is not a failure, it is the
wheel completing -- you collected premium on the put, premium on the call, and
sold the stock above your basis. So the objective is total return per
capital-day across the whole outcome, not premium alone:

    if called away:  (strike - basis) x 100 + premium, over the days held
    if it expires:   premium alone, and you write another one

Both are reported, with the empirical odds of each.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from analytics import costs, moves, vrp
from core.market_calendar import ET, trading_days_between
from core.paths import load_config


@dataclass
class CallCandidate:
    ticker: str
    expiration: dt.date
    strike: float
    spot: float
    basis: float
    shares: int
    contracts: int
    dte_calendar: int
    dte_trading: int

    bid: float | None
    ask: float | None
    modelled_fill: float
    delta: float | None
    implied_vol: float | None
    open_interest: float | None

    gross_credit: float
    fees: float
    net_credit: float
    cost_drag: float

    # Outcomes
    prob_called_away: float | None
    prob_expires: float | None
    prob_touch: float | None
    upside_forgone: float | None
    gain_if_called: float          # dollars, including the stock leg
    total_return_if_called: float  # on basis capital
    annualised_if_called: float
    annualised_if_expires: float
    expected_value: float
    ev_annualised: float

    above_basis: bool
    locked_in_loss: float          # negative when selling below basis
    iv_rv_ratio: float | None
    ex_dividend_risk: str
    sample_label: str
    effective_n: int

    accepted: bool
    rejections: tuple = ()
    warnings: tuple = ()
    rationale: str = ""

    def to_dict(self) -> dict:
        out = asdict(self)
        out["expiration"] = str(self.expiration)
        return out

    @property
    def rank_key(self) -> float:
        if not self.accepted:
            return -1e9
        return self.ev_annualised


# --- Early assignment ------------------------------------------------------

def ex_dividend_warning(ticker: str, expiration: dt.date, extrinsic: float,
                         contracts: int) -> str:
    """Flag the classic early-assignment setup on a short call.

    A short call is exercised early when the dividend exceeds the remaining
    extrinsic value -- the holder captures the dividend and gives up time value
    they were about to lose anyway. It is the one early-exercise case that is
    genuinely predictable, and it strands you without the shares just before a
    payment you were counting on.
    """
    try:
        from data_sources.yfinance_sync import next_ex_dividend
        projection = next_ex_dividend(ticker)
    except Exception:
        return ""
    if not projection:
        return ""
    ex_date, amount = projection
    if not (dt.date.today() <= ex_date <= expiration):
        return ""

    cfg = load_config().get("management", {}).get("covered_call", {})
    window = cfg.get("avoid_ex_dividend_within_days", 3)
    if extrinsic < amount:
        return (f"HIGH early-assignment risk: projected ex-dividend {ex_date} pays "
                f"${amount:.2f} against ${extrinsic:.2f} of remaining extrinsic value. "
                f"Expect to be called away the day before and to miss the dividend "
                f"(${amount * 100 * contracts:,.0f} across {contracts} contracts).")
    if (ex_date - dt.date.today()).days <= window:
        return (f"Projected ex-dividend {ex_date} (${amount:.2f}) falls inside this "
                f"contract. Extrinsic value of ${extrinsic:.2f} still exceeds it, so "
                f"early exercise is unlikely -- but watch it as expiration approaches.")
    return ""


# --- Evaluation ------------------------------------------------------------

def evaluate_call(ticker: str, row: pd.Series, spot: float, basis: float,
                   shares: int, daily: pd.DataFrame,
                   allow_below_basis: bool = False) -> CallCandidate | None:
    cfg = load_config().get("management", {}).get("covered_call", {})
    strike = float(row["strike_price"])
    expiration = pd.Timestamp(row["expiration"]).date()
    today = dt.datetime.now(ET).date()
    dte_cal = max((expiration - today).days, 0)
    dte_trd = max(trading_days_between(today, expiration), 1)
    contracts = shares // 100
    if contracts < 1:
        return None

    bid, ask, mid = _f(row.get("call_bid")), _f(row.get("call_ask")), _f(row.get("call_mark"))
    fill = costs.realistic_fill(bid, ask, mid, side="sell")
    if fill is None or fill <= 0:
        return None

    oi = _f(row.get("call_open_interest"))
    iv = _f(row.get("call_iv"))
    delta = _f(row.get("call_delta"))

    entry_fees = costs.option_open(contracts, "sell").total
    gross = fill * 100.0 * contracts
    net = gross - entry_fees
    drag = entry_fees / gross if gross > 0 else float("nan")

    above_basis = strike >= basis
    locked = (strike - basis) * 100.0 * contracts if not above_basis else 0.0

    upside = moves.upside_probabilities(daily, ticker, spot, strike, dte_trd,
                                         lookback_years=10, vol_conditioned=True,
                                         min_observations=40) if not daily.empty else None

    # Outcome accounting. Being called away closes the cycle: you keep the
    # premium AND realise the stock move from basis to strike.
    called_fees = (costs.assignment(contracts).total
                   + costs.stock_sell(100 * contracts, strike).total)
    stock_leg = (strike - basis) * 100.0 * contracts
    gain_if_called = net + stock_leg - called_fees
    basis_capital = basis * 100.0 * contracts

    total_return = gain_if_called / basis_capital if basis_capital else float("nan")
    ann_called = total_return * (365.0 / max(dte_cal, 1))
    ann_expires = (net / basis_capital) * (365.0 / max(dte_cal, 1)) if basis_capital else float("nan")

    if upside is not None:
        p_called = upside.prob_called_away
        ev = p_called * gain_if_called + (1 - p_called) * net
        forgone = upside.expected_upside_forgone
        sample, eff_n, touch = upside.sample_label, upside.effective_n, upside.prob_touch
        p_expires = upside.prob_expires_worthless
    else:
        p_called = p_expires = touch = forgone = None
        ev = net
        sample, eff_n = "no empirical sample", 0

    ev_ann = (ev / basis_capital) * (365.0 / max(dte_cal, 1)) if basis_capital else float("nan")

    reading = vrp.reading(ticker, iv, daily, vrp.match_rv_window_to_dte(dte_cal)) \
        if iv and not daily.empty else None

    intrinsic = max(spot - strike, 0.0)
    extrinsic = max(fill - intrinsic, 0.0)
    div_warning = ex_dividend_warning(ticker, expiration, extrinsic, contracts)

    rejections: list[str] = []
    warnings: list[str] = []

    if not above_basis:
        message = (f"strike ${strike:g} is BELOW your ${basis:.2f} adjusted basis -- "
                   f"being called away realises a ${abs(locked):,.0f} loss on the "
                   f"stock leg, which the ${net:,.0f} of premium "
                   + ("more than offsets" if net > abs(locked) else "does not cover"))
        (warnings if allow_below_basis else rejections).append(message)

    min_credit = cfg.get("min_credit_per_contract", 0.15)
    if fill < min_credit:
        rejections.append(f"credit ${fill:.2f} is below the ${min_credit:.2f} floor")

    lo, hi = cfg.get("dte_min", 5), cfg.get("dte_max", 14)
    if not (lo <= dte_cal <= hi):
        warnings.append(f"{dte_cal} DTE is outside the {lo}-{hi} window")

    if div_warning.startswith("HIGH"):
        rejections.append(div_warning)
    elif div_warning:
        warnings.append(div_warning)

    accepted = not rejections

    return CallCandidate(
        ticker=ticker, expiration=expiration, strike=strike, spot=spot, basis=basis,
        shares=shares, contracts=contracts, dte_calendar=dte_cal, dte_trading=dte_trd,
        bid=bid, ask=ask, modelled_fill=fill, delta=delta, implied_vol=iv,
        open_interest=oi,
        gross_credit=gross, fees=entry_fees, net_credit=net, cost_drag=drag,
        prob_called_away=p_called, prob_expires=p_expires, prob_touch=touch,
        upside_forgone=forgone,
        gain_if_called=gain_if_called, total_return_if_called=total_return,
        annualised_if_called=ann_called, annualised_if_expires=ann_expires,
        expected_value=ev, ev_annualised=ev_ann,
        above_basis=above_basis, locked_in_loss=locked,
        iv_rv_ratio=reading.ratio if reading else None,
        ex_dividend_risk=div_warning, sample_label=sample, effective_n=eff_n,
        accepted=accepted, rejections=tuple(rejections), warnings=tuple(warnings),
        rationale=_rationale(ticker, strike, expiration, fill, contracts, net,
                              p_called, gain_if_called, ann_called, basis, above_basis),
    )


def _f(value) -> float | None:
    try:
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return None
        out = float(value)
        return out if np.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _rationale(ticker, strike, expiration, fill, contracts, net, p_called,
                gain, ann, basis, above_basis) -> str:
    bits = [f"Sell {contracts} {ticker} {expiration} ${strike:g} call at ~${fill:.2f} "
            f"for ${net:,.0f} net."]
    if above_basis:
        bits.append(f"Strike is ${strike - basis:.2f} above your ${basis:.2f} basis.")
    if p_called is not None:
        bits.append(f"{p_called:.0%} empirical chance of being called away, which "
                    f"closes the cycle for ${gain:,.0f} total "
                    f"({ann:.1%} annualised on basis capital).")
    else:
        bits.append(f"If called away the cycle closes for ${gain:,.0f}.")
    return " ".join(bits)


# --- Sheet -----------------------------------------------------------------

def candidates_for_lot(ticker: str, shares: int, basis: float,
                        allow_below_basis: bool = False,
                        limit: int = 5) -> list[CallCandidate]:
    """Rank covered calls against one assigned share lot."""
    from data_sources import chains
    from data_sources.yfinance_sync import load_daily_total_return

    cfg = load_config().get("management", {}).get("covered_call", {})
    chain, under = chains.load_chain(ticker)
    spot = chains.spot_from_underlying(under)
    if chain.empty or not spot or "call_delta" not in chain.columns:
        return []

    today = dt.datetime.now(ET).date()
    frame = chain.copy()
    frame["expiration"] = pd.to_datetime(frame["expiration"])
    frame["dte"] = (frame["expiration"].dt.date - today).apply(lambda d: d.days)
    frame = frame[(frame["dte"] >= cfg.get("dte_min", 5))
                   & (frame["dte"] <= cfg.get("dte_max", 14))]
    frame = frame[frame["call_delta"].notna()]
    # Only strikes at or above spot: writing a call below spot is selling
    # intrinsic value, a different trade with a different purpose.
    frame = frame[frame["strike_price"].astype(float) >= spot * 0.98]
    if frame.empty:
        return []

    daily = load_daily_total_return(ticker)
    out = []
    for _, row in frame.iterrows():
        candidate = evaluate_call(ticker, row, spot, basis, shares, daily,
                                   allow_below_basis)
        if candidate is not None:
            out.append(candidate)

    out.sort(key=lambda c: c.rank_key, reverse=True)
    return out[:limit]


def sheet_for_all_lots(allow_below_basis: bool = False) -> pd.DataFrame:
    """Best covered call for every share lot currently held."""
    from analytics import paper
    lots = paper.list_share_lots(open_only=True)
    if lots.empty:
        return pd.DataFrame()

    rows = []
    for _, lot in lots.iterrows():
        best = candidates_for_lot(str(lot["ticker"]).upper(), int(lot["shares"]),
                                   float(lot["adjusted_basis"]),
                                   allow_below_basis, limit=1)
        if best:
            record = best[0].to_dict()
            record["lot_id"] = int(lot["lot_id"])
            rows.append(record)
        else:
            rows.append({
                "lot_id": int(lot["lot_id"]), "ticker": str(lot["ticker"]).upper(),
                "shares": int(lot["shares"]), "basis": float(lot["adjusted_basis"]),
                "accepted": False,
                "rationale": ("No call above basis in the target window. Either wait "
                               "for the stock to recover, or sell below basis "
                               "deliberately -- the tool will show the exact loss."),
            })
    return pd.DataFrame(rows)
