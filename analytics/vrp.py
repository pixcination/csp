"""
Variance risk premium -- IV against realized vol.

This is the stopgap for finding F-05's dead IV-rank component, and it is
arguably the better metric anyway.

IV rank asks "is this stock's implied vol high *relative to its own past*?",
which needs ten-plus accumulated snapshots before it says anything, and even
then it is a purely relative statement -- a name can be at IV rank 95 and
still be cheap if its realized vol is higher still.

The variance risk premium asks the question that actually determines whether
selling premium pays: **am I being paid more than this stock has actually been
moving?** It works from a single snapshot, today, and it is the direct
economic basis of the entire strategy. If IV/RV is below 1.0 you are selling
volatility for less than it has been costing to hedge, and no amount of
technical screening rescues that trade.

Both metrics stay in the score. VRP carries the weight now; IV rank takes
over its share as snapshot history accumulates.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# Empirical anchors for equity index and large-cap single names. Short-dated
# equity options have historically priced roughly 10-20% above subsequent
# realized volatility; that spread is the seller's edge, and it varies a lot
# by name and regime. These bands classify, they do not predict.
RICH = 1.25
FAIR = 1.05
THIN = 0.95


@dataclass(frozen=True)
class VrpReading:
    ticker: str
    implied_vol: float
    realized_vol: float
    rv_window: int
    ratio: float
    spread: float          # IV - RV, in vol points
    classification: str
    tradable: bool
    note: str

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker, "implied_vol": self.implied_vol,
            "realized_vol": self.realized_vol, "rv_window": self.rv_window,
            "iv_rv_ratio": self.ratio, "iv_rv_spread": self.spread,
            "vrp_class": self.classification, "vrp_tradable": self.tradable,
            "vrp_note": self.note,
        }

    @property
    def score(self) -> float:
        """0-1 component for the composite score.

        Anchored on the ratio rather than percentile-ranked, because unlike
        yield or liquidity this has a meaningful absolute zero: 1.0 is the
        break-even point where implied and realized agree. Ranking it within
        the universe would hide a day when *everything* is priced thin.
        """
        if not np.isfinite(self.ratio):
            return float("nan")
        # 0.90 -> 0.0, 1.00 -> 0.25, 1.15 -> 0.625, 1.30+ -> 1.0
        return float(np.clip((self.ratio - 0.90) / 0.40, 0.0, 1.0))


def classify(ratio: float) -> tuple[str, bool, str]:
    if not np.isfinite(ratio):
        return "unknown", False, "no implied or realized vol available"
    if ratio >= RICH:
        return "rich", True, (
            f"implied vol is {ratio:.2f}x realized -- well paid for the risk")
    if ratio >= FAIR:
        return "fair", True, (
            f"implied vol is {ratio:.2f}x realized -- normal seller's edge")
    if ratio >= THIN:
        return "thin", False, (
            f"implied vol is only {ratio:.2f}x realized -- barely compensated; "
            f"one gap erases the premium")
    return "negative", False, (
        f"implied vol is {ratio:.2f}x realized -- you would be selling volatility "
        f"below what this stock has actually been delivering")


def realized_vol(daily: pd.DataFrame, window: int = 20,
                  price_col: str = "close") -> float:
    df = daily.copy()
    df.columns = [str(c).lower() for c in df.columns]
    if price_col not in df.columns or len(df) < window + 2:
        return float("nan")
    log_ret = np.log(df[price_col].astype(float)).diff()
    return float(log_ret.rolling(window).std().iloc[-1] * np.sqrt(252.0))


def reading(ticker: str, implied_vol: float | None, daily: pd.DataFrame,
             rv_window: int = 20) -> VrpReading:
    """Compare a chain's implied vol at the traded strike against realized vol.

    `rv_window` should roughly match the option's tenor: for a 7-DTE put, a
    10-day realized window is a closer comparison than a 60-day one. The
    caller picks; 20 is a reasonable default for the 5-14 DTE band.
    """
    rv = realized_vol(daily, rv_window)
    iv = float(implied_vol) if implied_vol not in (None, 0) else float("nan")
    ratio = iv / rv if (np.isfinite(iv) and np.isfinite(rv) and rv > 0) else float("nan")
    label, tradable, note = classify(ratio)
    return VrpReading(ticker, iv, rv, rv_window, ratio,
                      iv - rv if np.isfinite(iv) and np.isfinite(rv) else float("nan"),
                      label, tradable, note)


def match_rv_window_to_dte(dte: int) -> int:
    """Pick a realized-vol window comparable to the option's remaining life.

    Comparing a 7-day option's implied vol to 60-day realized is an
    apples-to-oranges trade decision; this keeps the horizons aligned.
    """
    if dte <= 3:
        return 5
    if dte <= 9:
        return 10
    if dte <= 16:
        return 20
    if dte <= 30:
        return 30
    return 60
