"""
Black-Scholes pricing/Greeks for European equity options -- used both to
theoretically price/greek chain snapshots that don't already carry
TastyTrade's own Greeks, and by the backtest harness (analytics/backtest.py)
to simulate historical option pricing where no historical chain exists.

American-style early exercise isn't modeled -- a reasonable simplification
for short-dated, modest-dividend equity puts at this tool's scope (the
wheel strategy sells puts to collect premium, not to trade early-exercise
edge cases).
"""
from dataclasses import dataclass

import numpy as np
from scipy.stats import norm


@dataclass
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float  # per calendar day
    vega: float   # per 1 vol point (0.01 change in IV)
    rho: float    # per 1% change in rate


def _d1_d2(spot, strike, dte_years, vol, rate):
    if dte_years <= 0 or vol <= 0:
        return None, None
    sqrt_t = np.sqrt(dte_years)
    d1 = (np.log(spot / strike) + (rate + 0.5 * vol ** 2) * dte_years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    return d1, d2


def bs_price_greeks(spot: float, strike: float, dte_days: float, vol: float,
                     rate: float, option_type: str = "put") -> Greeks:
    """
    spot: underlying price
    strike: option strike
    dte_days: days to expiration (calendar days)
    vol: annualized implied/realized volatility as a decimal (0.25 = 25%)
    rate: annualized risk-free rate as a decimal (0.045 = 4.5%)
    option_type: "put" or "call"
    """
    dte_years = max(dte_days, 0) / 365.0
    is_put = option_type == "put"

    if dte_years <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        # At/past expiration or degenerate inputs -- intrinsic value only, all greeks zero.
        intrinsic = max(strike - spot, 0.0) if is_put else max(spot - strike, 0.0)
        return Greeks(price=intrinsic, delta=0.0, gamma=0.0, theta=0.0, vega=0.0, rho=0.0)

    d1, d2 = _d1_d2(spot, strike, dte_years, vol, rate)
    disc = np.exp(-rate * dte_years)
    sqrt_t = np.sqrt(dte_years)

    if is_put:
        price = strike * disc * norm.cdf(-d2) - spot * norm.cdf(-d1)
        delta = norm.cdf(d1) - 1.0
        rho = -strike * dte_years * disc * norm.cdf(-d2) / 100.0
        theta_annual = (-spot * norm.pdf(d1) * vol / (2 * sqrt_t)
                         + rate * strike * disc * norm.cdf(-d2))
    else:
        price = spot * norm.cdf(d1) - strike * disc * norm.cdf(d2)
        delta = norm.cdf(d1)
        rho = strike * dte_years * disc * norm.cdf(d2) / 100.0
        theta_annual = (-spot * norm.pdf(d1) * vol / (2 * sqrt_t)
                         - rate * strike * disc * norm.cdf(d2))

    gamma = norm.pdf(d1) / (spot * vol * sqrt_t)
    vega = spot * norm.pdf(d1) * sqrt_t / 100.0
    theta = theta_annual / 365.0

    return Greeks(price=float(price), delta=float(delta), gamma=float(gamma),
                  theta=float(theta), vega=float(vega), rho=float(rho))


def implied_vol(target_price: float, spot: float, strike: float, dte_days: float,
                 rate: float, option_type: str = "put",
                 lo: float = 0.01, hi: float = 5.0, tol: float = 1e-6,
                 max_iter: int = 100) -> float | None:
    """Bisection solve for IV given a market price -- used when a chain
    snapshot has bid/ask/mark but a missing or zero IV field. Returns None
    if it doesn't bracket a root (e.g. price outside no-arbitrage bounds)."""
    def price_at(vol):
        return bs_price_greeks(spot, strike, dte_days, vol, rate, option_type).price

    f_lo, f_hi = price_at(lo) - target_price, price_at(hi) - target_price
    if f_lo == 0:
        return lo
    if f_hi == 0:
        return hi
    if f_lo * f_hi > 0:
        return None

    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        f_mid = price_at(mid) - target_price
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


def probability_otm(spot: float, strike: float, dte_days: float, vol: float,
                     rate: float, option_type: str = "put") -> float:
    """Risk-neutral probability the option expires out-of-the-money (i.e.
    the wheel seller keeps full premium with no assignment) -- P(S_T > K)
    for a put, P(S_T < K) for a call, under GBM with drift = rate."""
    dte_years = max(dte_days, 0) / 365.0
    if dte_years <= 0 or vol <= 0:
        is_put = option_type == "put"
        otm_now = spot > strike if is_put else spot < strike
        return 1.0 if otm_now else 0.0

    _, d2 = _d1_d2(spot, strike, dte_years, vol, rate)
    prob_itm_risk_neutral = norm.cdf(d2)  # P(S_T > K) under risk-neutral measure
    if option_type == "put":
        return float(prob_itm_risk_neutral)       # put OTM means S_T > K
    return float(1.0 - prob_itm_risk_neutral)      # call OTM means S_T < K


def expected_move(spot: float, dte_days: float, vol: float, sigmas: float = 1.0) -> float:
    """Expected-move price range at `sigmas` standard deviations to
    expiration -- the building block for the probability cone chart."""
    dte_years = max(dte_days, 0) / 365.0
    return spot * vol * np.sqrt(dte_years) * sigmas
