"""
Wheel-cycle backtest -- replaces the naked-put harness (finding F-03).

WHAT THE OLD ONE MEASURED
-------------------------
`analytics/backtest.py` computed `premium - max(strike - exit_close, 0)` and
ended the trade. That books assignment as an immediate realised loss, which is
a naked short put. It is not what you trade. Its drawdown numbers describe a
strategy you would never run, and its 81-83% "win rate" answers a question
about option expiry rather than about money.

WHAT THIS ONE MEASURES
----------------------
A full cycle, the way it actually unfolds:

    sell put -> expires OTM                      -> cycle closes, keep premium
             -> assigned                         -> hold shares at strike
                  -> sell call above basis
                       -> expires                -> keep premium, write another
                       -> called away            -> cycle closes, sell at strike

and reports what a wheel trader actually needs: return per cycle, cycle
duration, and **capital-days** -- because the strategy's real cost is not the
loss on a bad trade, it is the collateral immobilised for eleven weeks while a
stock recovers.

HONEST LIMITS
-------------
* Option prices are simulated. TastyTrade has no historical chain API, so
  entry premiums come from Black-Scholes on a trailing-realized-vol proxy
  scaled by a variance risk premium. That multiplier is an assumption, and the
  results are only as good as it is -- which is precisely why the paper book
  exists: real recorded fills will eventually calibrate it per ticker.
* Every leg is charged commission, exchange fees and a slippage haircut, so
  the numbers are net. The old harness charged nothing.
* Early assignment is not modelled on the put side. For OTM short puts held to
  a weekly expiry that is a reasonable simplification.
* Cycles are sequential per ticker, one at a time. That matches how capital
  actually works in a cash-secured account and makes capital-days meaningful.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import norm

from analytics import costs
from core.paths import load_config

TRADING_DAYS = 252.0


# --- Simulated option pricing ---------------------------------------------

def _bs(spot: float, strike: float, days: int, vol: float, rate: float,
         kind: str) -> float:
    """Black-Scholes price. Days are TRADING days, matching the vol estimate."""
    if days <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        return max(strike - spot, 0.0) if kind == "put" else max(spot - strike, 0.0)
    t = days / TRADING_DAYS
    d1 = (np.log(spot / strike) + (rate + 0.5 * vol * vol) * t) / (vol * np.sqrt(t))
    d2 = d1 - vol * np.sqrt(t)
    if kind == "put":
        return float(strike * np.exp(-rate * t) * norm.cdf(-d2) - spot * norm.cdf(-d1))
    return float(spot * norm.cdf(d1) - strike * np.exp(-rate * t) * norm.cdf(d2))


def _strike_for_delta(spot: float, target_delta: float, days: int, vol: float,
                       rate: float, kind: str) -> float:
    """Closed-form inversion of the delta formula -- exact, not a search."""
    if days <= 0 or vol <= 0:
        return spot
    t = days / TRADING_DAYS
    if kind == "put":
        d1 = norm.ppf(target_delta + 1.0)          # target_delta is negative
    else:
        d1 = norm.ppf(target_delta)
    return float(spot * np.exp(-d1 * vol * np.sqrt(t) + (rate + 0.5 * vol * vol) * t))


def _round_strike(strike: float) -> float:
    """Snap to a plausible listed increment, so simulated strikes are not
    infinitely fine-grained in a way real chains never are."""
    if strike >= 200:
        return round(strike)
    if strike >= 50:
        return round(strike * 2) / 2
    if strike >= 20:
        return round(strike * 2) / 2
    return round(strike)


# --- Parameters and results ------------------------------------------------

@dataclass
class WheelParams:
    put_delta: float = -0.20
    put_dte: int = 7                  # trading days
    call_delta: float = 0.25
    call_dte: int = 7
    rv_window: int = 20
    vol_risk_premium: float = 1.15
    rate: float = 0.045
    contracts: int = 1
    never_below_basis: bool = True
    slippage_fraction: float = 0.40
    max_cycle_days: int = 504         # abandon a cycle after ~2 years

    def label(self) -> str:
        return (f"{abs(self.put_delta):.2f}d put / {self.put_dte}d, "
                f"{self.call_delta:.2f}d call / {self.call_dte}d")


@dataclass
class Cycle:
    ticker: str
    start_date: dt.date
    end_date: dt.date | None = None
    put_strike: float = 0.0
    put_premium: float = 0.0
    assigned: bool = False
    calls_written: int = 0
    call_premium_total: float = 0.0
    stock_pnl: float = 0.0
    fees: float = 0.0
    outcome: str = "open"             # expired | called_away | abandoned
    basis: float = 0.0
    trading_days: int = 0

    @property
    def net_pnl(self) -> float:
        return (self.put_premium + self.call_premium_total
                + self.stock_pnl - self.fees)

    @property
    def collateral(self) -> float:
        return self.put_strike * 100.0

    @property
    def capital_days(self) -> float:
        return self.collateral * self.trading_days

    @property
    def return_on_collateral(self) -> float:
        return self.net_pnl / self.collateral if self.collateral else float("nan")


@dataclass
class WheelResult:
    ticker: str
    params: dict
    cycles: pd.DataFrame
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ticker": self.ticker, "params": self.params, "summary": self.summary}


# --- The simulation --------------------------------------------------------

def run_wheel(daily: pd.DataFrame, ticker: str,
               params: WheelParams | None = None) -> WheelResult:
    """Simulate sequential wheel cycles over a ticker's full history."""
    params = params or WheelParams()
    frame = daily.copy()
    frame.columns = [str(c).lower() for c in frame.columns]
    if "date" in frame.columns:
        frame = frame.set_index(pd.to_datetime(frame["date"]))
    frame = frame.dropna(subset=["close"]).sort_index()
    if len(frame) < params.rv_window + params.put_dte + 20:
        return WheelResult(ticker, asdict(params), pd.DataFrame(),
                            {"error": "insufficient history"})

    close = frame["close"].to_numpy(dtype=float)
    dates = frame.index.date
    log_ret = np.log(frame["close"]).diff()
    rv = (log_ret.rolling(params.rv_window).std() * np.sqrt(TRADING_DAYS)).to_numpy()

    entry_fee = costs.option_open(params.contracts, "sell").total
    close_fee = costs.option_close(params.contracts, "buy").total
    assign_fee = costs.assignment(params.contracts).total

    cycles: list[Cycle] = []
    i = params.rv_window
    n = len(close)

    while i < n - params.put_dte - 1:
        vol = rv[i]
        if not np.isfinite(vol) or vol <= 0:
            i += 1
            continue
        vol *= params.vol_risk_premium
        spot = close[i]

        # --- Leg 1: sell the cash-secured put ---------------------------
        strike = _round_strike(_strike_for_delta(spot, params.put_delta,
                                                  params.put_dte, vol, params.rate,
                                                  "put"))
        theoretical = _bs(spot, strike, params.put_dte, vol, params.rate, "put")
        premium = theoretical * (1.0 - params.slippage_fraction * 0.05)
        if premium <= 0.01:
            i += 1
            continue

        cycle = Cycle(ticker=ticker, start_date=dates[i], put_strike=strike,
                       put_premium=premium * 100 * params.contracts,
                       fees=entry_fee, basis=strike - premium)

        expiry = i + params.put_dte
        exit_close = close[expiry]
        cursor = expiry
        cycle.trading_days = params.put_dte

        if exit_close >= strike:
            cycle.outcome = "expired"
            cycle.end_date = dates[expiry]
            cycles.append(cycle)
            i = expiry + 1
            continue

        # --- Assigned: hold shares, write calls above basis --------------
        cycle.assigned = True
        cycle.fees += assign_fee
        shares = 100 * params.contracts
        basis = strike - premium          # every premium reduces the basis

        while cursor < n - params.call_dte - 1:
            if cycle.trading_days >= params.max_cycle_days:
                cycle.outcome = "abandoned"
                break
            spot_now = close[cursor]
            vol_now = rv[cursor]
            if not np.isfinite(vol_now) or vol_now <= 0:
                cursor += 1
                cycle.trading_days += 1
                continue
            vol_now *= params.vol_risk_premium

            call_strike = _round_strike(_strike_for_delta(
                spot_now, params.call_delta, params.call_dte, vol_now,
                params.rate, "call"))
            # THE HARD RULE, enforced in the simulation exactly as in the app:
            # never write below basis. When the stock is far under water there
            # is simply no call to write, and the cycle waits -- which is the
            # capital-days cost the old harness could not see.
            if params.never_below_basis and call_strike < basis:
                call_strike = _round_strike(basis)
                if call_strike < basis:
                    call_strike = basis

            call_theo = _bs(spot_now, call_strike, params.call_dte, vol_now,
                             params.rate, "call")
            call_prem = call_theo * (1.0 - params.slippage_fraction * 0.05)

            if call_prem <= 0.01:
                # Nothing worth writing. Hold and wait a week.
                cursor += params.call_dte
                cycle.trading_days += params.call_dte
                continue

            cycle.calls_written += 1
            cycle.call_premium_total += call_prem * 100 * params.contracts
            cycle.fees += entry_fee
            basis -= call_prem

            call_expiry = cursor + params.call_dte
            cursor = call_expiry
            cycle.trading_days += params.call_dte
            price_at_expiry = close[call_expiry]

            if price_at_expiry >= call_strike:
                cycle.fees += assign_fee + costs.stock_sell(shares, call_strike).total
                cycle.stock_pnl = (call_strike - strike) * shares
                cycle.outcome = "called_away"
                cycle.end_date = dates[call_expiry]
                break

        if cycle.outcome == "open":
            cycle.outcome = "abandoned"
        if cycle.end_date is None:
            end_index = min(cursor, n - 1)
            cycle.end_date = dates[end_index]
            # Mark an abandoned cycle to market so it cannot flatter the results
            # by simply never closing.
            cycle.stock_pnl = (close[end_index] - strike) * shares

        cycles.append(cycle)
        i = min(cursor, n - 1) + 1

    if not cycles:
        return WheelResult(ticker, asdict(params), pd.DataFrame(),
                            {"error": "no cycles generated"})

    table = pd.DataFrame([{
        "start_date": c.start_date, "end_date": c.end_date, "outcome": c.outcome,
        "put_strike": c.put_strike, "assigned": c.assigned,
        "calls_written": c.calls_written, "put_premium": c.put_premium,
        "call_premium": c.call_premium_total, "stock_pnl": c.stock_pnl,
        "fees": c.fees, "net_pnl": c.net_pnl, "collateral": c.collateral,
        "trading_days": c.trading_days, "capital_days": c.capital_days,
        "return_on_collateral": c.return_on_collateral,
    } for c in cycles])

    return WheelResult(ticker, asdict(params), table, _summarise(table, params))


def _summarise(table: pd.DataFrame, params: WheelParams) -> dict:
    total_pnl = float(table["net_pnl"].sum())
    total_capital_days = float(table["capital_days"].sum())
    cumulative = table["net_pnl"].cumsum()
    drawdown = cumulative - cumulative.cummax()

    assigned = table[table["assigned"]]
    return {
        "n_cycles": int(len(table)),
        "pct_expired_clean": float((table["outcome"] == "expired").mean()),
        "assignment_rate": float(table["assigned"].mean()),
        "pct_called_away": float((table["outcome"] == "called_away").mean()),
        "pct_abandoned": float((table["outcome"] == "abandoned").mean()),
        "mean_cycle_days": float(table["trading_days"].mean()),
        "median_cycle_days": float(table["trading_days"].median()),
        "mean_assigned_cycle_days": float(assigned["trading_days"].mean())
        if len(assigned) else float("nan"),
        "worst_cycle_days": int(table["trading_days"].max()),
        "total_pnl": total_pnl,
        "mean_cycle_pnl": float(table["net_pnl"].mean()),
        "pct_profitable_cycles": float((table["net_pnl"] > 0).mean()),
        "total_fees": float(table["fees"].sum()),
        # The number that matters: return per dollar of collateral per day,
        # annualised. Comparable across tickers and across parameter sets in a
        # way per-trade return is not.
        "annualised_on_capital_deployed": (
            total_pnl / total_capital_days * 252.0 if total_capital_days else float("nan")),
        "worst_drawdown": float(drawdown.min()),
        "mean_calls_per_assignment": float(assigned["calls_written"].mean())
        if len(assigned) else 0.0,
    }


# --- Parameter sweep -------------------------------------------------------

def sweep(daily: pd.DataFrame, ticker: str,
           put_deltas=(-0.15, -0.20, -0.25, -0.30),
           put_dtes=(5, 7, 10),
           call_deltas=(0.20, 0.25, 0.30),
           base: WheelParams | None = None,
           reporter=None) -> pd.DataFrame:
    """Grid search over the management rules.

    This is what turns the reasoned starting rules into fitted ones. Read the
    output with the same scepticism you would give any in-sample optimisation:
    a grid this size will always produce a winner, and the difference between
    the top few settings is usually noise. What it is genuinely good for is
    finding the *shape* -- whether returns fall off a cliff below 0.20 delta,
    whether 5 DTE is systematically worse than 10 -- rather than the single
    best cell.
    """
    base = base or WheelParams()
    rows = []
    total = len(put_deltas) * len(put_dtes) * len(call_deltas)
    done = 0
    for put_delta in put_deltas:
        for put_dte in put_dtes:
            for call_delta in call_deltas:
                params = WheelParams(**{**asdict(base), "put_delta": put_delta,
                                         "put_dte": put_dte, "call_delta": call_delta})
                result = run_wheel(daily, ticker, params)
                done += 1
                if reporter:
                    reporter.advance(1, note=f"{ticker} {params.label()}")
                if result.summary.get("error"):
                    continue
                rows.append({"ticker": ticker, "put_delta": put_delta,
                              "put_dte": put_dte, "call_delta": call_delta,
                              **result.summary})
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.sort_values("annualised_on_capital_deployed", ascending=False)
    return frame.reset_index(drop=True)


def compare_to_buy_and_hold(daily: pd.DataFrame, result: WheelResult) -> dict:
    """The comparison that decides whether the strategy is worth running.

    A wheel that returns 9% while the stock returned 22% is not a good wheel,
    however high its win rate. Measured over the same window, on the same
    capital, net of the same fees.
    """
    if result.cycles.empty:
        return {}
    frame = daily.copy()
    frame.columns = [str(c).lower() for c in frame.columns]
    if "date" in frame.columns:
        frame = frame.set_index(pd.to_datetime(frame["date"]))
    frame = frame.sort_index()

    start = pd.Timestamp(result.cycles["start_date"].iloc[0])
    end = pd.Timestamp(result.cycles["end_date"].iloc[-1])
    window = frame.loc[start:end]
    if len(window) < 2:
        return {}

    years = max((end - start).days / 365.25, 1e-9)
    bh_total = float(window["close"].iloc[-1] / window["close"].iloc[0] - 1.0)
    bh_annual = (1.0 + bh_total) ** (1.0 / years) - 1.0

    wheel_annual = result.summary.get("annualised_on_capital_deployed", float("nan"))
    return {
        "window_start": str(start.date()), "window_end": str(end.date()),
        "years": years,
        "buy_and_hold_total": bh_total,
        "buy_and_hold_annualised": float(bh_annual),
        "wheel_annualised_on_capital": wheel_annual,
        "wheel_beats_buy_and_hold": bool(wheel_annual > bh_annual),
        "note": ("The wheel caps upside by construction. Beating buy-and-hold on a "
                  "strongly trending name is not the expectation -- lower drawdown "
                  "and steadier cash flow are. Judge it on both."),
    }
