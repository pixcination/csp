"""
Legs and positions -- the shape every strategy builds (Phase 12, roadmap B.6).

A `Position` is a list of `Leg`s plus the credit actually modelled for the
whole package. It knows its payoff at expiry, its value before expiry
(Black-Scholes per leg), max profit / max loss, breakevens, the buying power
it ties up and its net Greeks. Per-share throughout; multiply by 100 x
contracts for dollars.

Conventions: `side` is "short" or "long"; `qty` is per contract of the
position (a vertical is one short and one long); option deltas are the
chain's (a put's is negative), and a position's Greeks flip the sign of
short legs.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field

import numpy as np


@dataclass
class Leg:
    option_type: str                 # "put" | "call"
    side: str                        # "short" | "long"
    strike: float
    expiration: dt.date | str
    qty: int = 1
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    open_interest: float | None = None
    volume: float | None = None
    symbol: str | None = None

    @property
    def sign(self) -> int:
        return -1 if self.side == "short" else 1

    def intrinsic(self, s_t: np.ndarray | float) -> np.ndarray | float:
        s_t = np.asarray(s_t, dtype=float)
        if self.option_type == "put":
            return np.maximum(self.strike - s_t, 0.0)
        return np.maximum(s_t - self.strike, 0.0)

    def to_dict(self) -> dict:
        out = asdict(self)
        out["expiration"] = str(self.expiration)
        return out


@dataclass
class Position:
    strategy: str
    ticker: str
    legs: list[Leg]
    credit: float                    # modelled net credit per share (debit < 0)
    collateral_per_contract: float | None = None   # overrides the defined-risk BPR
    notes: list[str] = field(default_factory=list)

    # --- Payoff --------------------------------------------------------------

    def payoff(self, s_t) -> np.ndarray | float:
        """P&L per share at expiry for terminal prices `s_t` (before fees)."""
        s_t = np.asarray(s_t, dtype=float)
        value = sum(leg.sign * leg.qty * leg.intrinsic(s_t) for leg in self.legs)
        return self.credit + value

    def value(self, spot: float, days_left: float, iv_fn=None, rate: float = 0.045) -> float:
        """Mark-to-model value of the legs per share (what closing would cost,
        as a signed amount: negative = you pay). `iv_fn(leg)` gives each leg's
        vol; default the leg's own IV."""
        from analytics.options_math import bs_price_greeks
        total = 0.0
        for leg in self.legs:
            vol = iv_fn(leg) if iv_fn else leg.iv
            if days_left <= 0 or not vol:
                price = float(leg.intrinsic(spot))
            else:
                price = bs_price_greeks(spot, leg.strike, days_left, vol, rate,
                                        leg.option_type).price
            total += leg.sign * leg.qty * price
        return total

    # --- Risk ---------------------------------------------------------------

    def _grid(self) -> np.ndarray:
        strikes = sorted({leg.strike for leg in self.legs})
        return np.array([0.0] + strikes + [strikes[-1] * 3.0 + 1.0])

    @property
    def max_profit(self) -> float:
        """Per share. Payoff is piecewise linear, so the extremes sit at 0,
        the strikes, or far above the top strike."""
        return float(np.max(self.payoff(self._grid())))

    @property
    def max_loss(self) -> float:
        """Per share, as a positive number (0 if the position cannot lose)."""
        return float(max(-np.min(self.payoff(self._grid())), 0.0))

    @property
    def breakevens(self) -> list[float]:
        grid = self._grid()
        values = self.payoff(grid)
        out = []
        for (x0, y0), (x1, y1) in zip(zip(grid, values), zip(grid[1:], values[1:])):
            if y0 == 0:
                out.append(float(x0))
            elif y0 * y1 < 0:
                out.append(float(x0 + (x1 - x0) * (-y0) / (y1 - y0)))
        return sorted(set(round(b, 6) for b in out))

    @property
    def collateral(self) -> float:
        """Buying power per contract: the stated collateral (a cash-secured
        put's strike x 100), else the defined-risk max loss x 100."""
        if self.collateral_per_contract is not None:
            return float(self.collateral_per_contract)
        return self.max_loss * 100.0

    @property
    def width(self) -> float | None:
        strikes = [leg.strike for leg in self.legs]
        return max(strikes) - min(strikes) if len(strikes) > 1 else None

    def net_greeks(self) -> dict:
        """Per contract (x100 shares), signed for the position."""
        out = {}
        for greek in ("delta", "gamma", "theta", "vega"):
            values = [getattr(leg, greek) for leg in self.legs]
            if any(v is None or not np.isfinite(v) for v in values):
                out[greek] = None
                continue
            out[greek] = float(sum(leg.sign * leg.qty * v * 100.0
                                   for leg, v in zip(self.legs, values)))
        return out
