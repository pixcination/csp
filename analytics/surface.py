"""
A self-consistent volatility surface built from one chain snapshot.

WHY THIS EXISTS
---------------
`skew.py` originally read the `put_iv` / `call_iv` columns that arrive on the
DXLink greeks feed. Those columns are not wrong, but they are not usable for
skew, and the reason is specific: each option symbol carries the IV computed at
*that symbol's own last update*. During a live session the updates are close
enough together that it does not matter. In a closed session -- which is when
this tool is mostly used -- the last update for each symbol is scattered across
the previous trading day, so the put wing and the call wing of the same
expiration can be sampled hours apart, at different spot levels.

Skew is a *difference* between two wings. A time mismatch between the wings is
therefore not noise around the answer, it is the answer.

Measured against the 2026-08-21 weekend capture, the feed reported skew an
average of 4.6 points of ATM vol below what the same snapshot's own bid/ask
quotes imply, and disagreed with those quotes on the *sign* for 11 of 29 names.

WHAT THIS MODULE DOES INSTEAD
-----------------------------
Everything is derived from a single snapshot, so every number shares one
instant by construction:

1. **The forward comes from put-call parity**, not from spot. `F = K + (C - P)`
   read off the most tightly quoted strikes. Nothing has to be assumed about
   dividends or borrow -- whatever the market is pricing is already in the
   quoted call and put.
2. **IV is solved from the quotes**, on out-of-the-money options only. In-the-
   money options carry almost all intrinsic and almost no time value, so their
   implied vol is dominated by the spread.
3. **Delta is computed from that same IV and forward**, so the strike at 25
   delta is located on the same surface whose IV is then read off it.
4. **The bid and the ask are each solved separately.** Every reading carries the
   interval the quotes actually permit, rather than pretending the mid is a
   measurement.

Point 4 is the one that changes conclusions. On that weekend capture, 21 of 29
names had a skew interval wide enough to contain zero: the quotes could not
determine even the sign. Reporting a classification for those names is
reporting the spread, not the market.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm

#: Strikes used to infer the forward, chosen by tightest combined spread.
FORWARD_STRIKES = 5
#: Vol search bounds for the solver. Outside this range the quote is not a
#: vol quote -- it is a spread artifact.
VOL_FLOOR, VOL_CEILING = 1e-4, 5.0


@dataclass(frozen=True)
class Leg:
    """One wing of the surface, read at a target delta."""
    strike: float
    iv: float        # from the mid
    iv_low: float    # from the quote that implies the lowest vol
    iv_high: float   # from the quote that implies the highest vol

    @property
    def uncertain(self) -> bool:
        return not np.isfinite(self.iv_low) or not np.isfinite(self.iv_high)


def _black76(forward: float, strike: float, years: float, vol: float,
             side: str) -> float:
    if vol <= 0 or years <= 0:
        return max(0.0, (forward - strike) if side == "call" else (strike - forward))
    root = vol * np.sqrt(years)
    d1 = (np.log(forward / strike) + 0.5 * vol * vol * years) / root
    d2 = d1 - root
    if side == "call":
        return forward * norm.cdf(d1) - strike * norm.cdf(d2)
    return strike * norm.cdf(-d2) - forward * norm.cdf(-d1)


def _delta(forward: float, strike: float, years: float, vol: float,
           side: str) -> float:
    if vol <= 0 or years <= 0:
        return float("nan")
    d1 = (np.log(forward / strike) + 0.5 * vol * vol * years) / (vol * np.sqrt(years))
    return float(norm.cdf(d1) if side == "call" else norm.cdf(d1) - 1.0)


def solve_iv(price: float, forward: float, strike: float, years: float,
             side: str) -> float:
    """Implied vol from one price, or NaN when the price carries no time value.

    A price at or below intrinsic is not a failed solve -- it is a quote that
    contains no volatility information at all, which is the normal state of a
    zero bid. NaN says so; a floor value would launder it into data.
    """
    intrinsic = max(0.0, (forward - strike) if side == "call" else (strike - forward))
    if not np.isfinite(price) or price <= intrinsic + 1e-6:
        return float("nan")
    try:
        return float(brentq(
            lambda v: _black76(forward, strike, years, v, side) - price,
            VOL_FLOOR, VOL_CEILING, xtol=1e-6))
    except (ValueError, RuntimeError):
        return float("nan")


def implied_forward(frame: pd.DataFrame, years: float,
                    strikes: int = FORWARD_STRIKES) -> float | None:
    """The forward the chain itself is pricing, via put-call parity.

    Taken from the most tightly quoted strikes rather than the strike nearest
    spot: parity holds everywhere, but it is only *measurable* where the spread
    is small relative to the difference being read.

    This replaces using spot as the reference. Spot ignores the dividend and
    the financing that the option market has already priced in, and for a
    dividend payer with an ex-date inside the window the difference is large
    enough on its own to tilt a skew reading.
    """
    quoted = frame[(frame["call_bid"] > 0) & (frame["put_bid"] > 0)].copy()
    if len(quoted) < 2:
        return None
    call_mid = (quoted["call_bid"] + quoted["call_ask"]) / 2.0
    put_mid = (quoted["put_bid"] + quoted["put_ask"]) / 2.0
    width = ((quoted["call_ask"] - quoted["call_bid"])
             + (quoted["put_ask"] - quoted["put_bid"]))
    tightest = width.nsmallest(min(strikes, len(quoted))).index
    forward = (quoted.loc[tightest, "strike_price"].astype(float)
               + (call_mid.loc[tightest] - put_mid.loc[tightest]))
    value = float(forward.median())
    return value if np.isfinite(value) and value > 0 else None


def otm_surface(frame: pd.DataFrame, forward: float, years: float) -> pd.DataFrame:
    """Out-of-the-money IV and delta at every strike, from bid, mid and ask.

    Only the OTM side of each strike is kept. An in-the-money option's price is
    almost all intrinsic, so its implied vol is the spread divided by a very
    small vega -- which is how a 4:1-wide quote on a deep call became a 56% vol
    reading in the first run.
    """
    rows = []
    for _, row in frame.iterrows():
        strike = float(row["strike_price"])
        side = "put" if strike < forward else "call"
        bid, ask = row.get(f"{side}_bid"), row.get(f"{side}_ask")
        if not (pd.notna(bid) and pd.notna(ask)) or ask <= 0:
            continue
        bid, ask = float(bid), float(ask)
        mid = (bid + ask) / 2.0
        iv_mid = solve_iv(mid, forward, strike, years, side)
        if not np.isfinite(iv_mid):
            continue
        rows.append({
            "strike": strike, "side": side, "iv": iv_mid,
            "iv_bid": solve_iv(bid, forward, strike, years, side),
            "iv_ask": solve_iv(ask, forward, strike, years, side),
            "delta": _delta(forward, strike, years, iv_mid, side),
            "spread": ask - bid, "mid": mid,
        })
    surface = pd.DataFrame(rows)
    if surface.empty:
        return surface
    surface["abs_delta"] = surface["delta"].abs()
    return surface.sort_values("strike").reset_index(drop=True)


def leg_at_delta(surface: pd.DataFrame, side: str, target: float) -> Leg | None:
    """Interpolate one wing to exactly `target` delta, carrying its quote band.

    Extrapolation is refused. If the chain never reaches the target delta on
    this side, the honest answer is that the wing was not observed.
    """
    wing = surface[(surface["side"] == side) & surface["abs_delta"].notna()]
    wing = wing[(wing["abs_delta"] > 0.01) & (wing["abs_delta"] < 0.99)]
    wing = wing.sort_values("abs_delta")
    if len(wing) < 2:
        return None
    deltas = wing["abs_delta"].to_numpy(dtype=float)
    if not (deltas.min() <= target <= deltas.max()):
        return None

    def at(column: str) -> float:
        """Interpolate one column, ignoring strikes where it is undefined.

        A zero bid far out on the wing makes `iv_bid` NaN there, and requiring
        the whole wing to be defined would discard the band for almost every
        name -- the deep tail always has a zero bid somewhere. What matters is
        whether the target delta is still bracketed by strikes that *do* carry
        the column; if it is not, that band edge is genuinely unknown.
        """
        usable = wing[np.isfinite(wing[column].to_numpy(dtype=float))]
        if len(usable) < 2:
            return float("nan")
        d = usable["abs_delta"].to_numpy(dtype=float)
        if not (d.min() <= target <= d.max()):
            return float("nan")
        return float(np.interp(target, d, usable[column].to_numpy(dtype=float)))

    strike = at("strike")
    if not np.isfinite(strike):
        return None
    # The bid always implies the lower vol and the ask the higher one, but
    # interpolation is done per-column, so order them explicitly rather than
    # assuming which is which.
    low, high = at("iv_bid"), at("iv_ask")
    if np.isfinite(low) and np.isfinite(high) and low > high:
        low, high = high, low
    return Leg(strike=strike, iv=at("iv"), iv_low=low, iv_high=high)


def atm_vol(surface: pd.DataFrame, forward: float) -> float:
    """The average of the two IVs closest to the forward.

    Averaging two strikes rather than picking one avoids the reading jumping
    whenever the forward crosses a strike.
    """
    if surface.empty:
        return float("nan")
    nearest = surface.assign(
        distance=(surface["strike"] - forward).abs()).nsmallest(2, "distance")
    values = nearest["iv"].dropna()
    return float(values.mean()) if len(values) else float("nan")
