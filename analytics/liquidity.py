"""
Option liquidity -- per leg and per position (Phase 12).

A fill you cannot get is not a signal, and a spread is only as liquid as
its thinner leg. For every leg:

    open interest, volume, bid / ask, spread in $ and as % of mid,
    fillability 0-1

and for a position, the least-liquid leg (which is what sizing is capped
by -- `sizing.max_contracts_for_position` takes every leg's OI and volume).

FILLABILITY (config.yaml -> liquidity_score)
--------------------------------------------
The mean of three 0-1 parts:

    spread   1 at or below spread_pct_good of mid, 0 at spread_pct_bad
             (either side of the hybrid rule: a penny-wide market on a cheap
             option passes on dollars even when its % looks wide)
    oi       log10(OI) between oi_log10 bounds
    volume   log10(volume + 1) between volume_log10 bounds; left out when
             volume is 0 outside the session (a stale field, not a dead contract)

OI WALLS
--------
Strikes whose open interest is at least `wall_multiple` x the median OI of
that side in the expiration -- where dealers and other writers are
concentrated. Reported for context; not a gate.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from core.paths import load_config


def _cfg() -> dict:
    defaults = {"spread_pct_good": 0.05, "spread_pct_bad": 0.50, "spread_dollars_good": 0.05,
                "oi_log10": [1.0, 4.0], "volume_log10": [0.0, 3.5], "wall_multiple": 3.0}
    defaults.update(load_config().get("liquidity_score", {}) or {})
    return defaults


def _f(value) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if np.isfinite(value) else None


def _scale(x: float, lo: float, hi: float) -> float:
    return float(min(max((x - lo) / (hi - lo), 0.0), 1.0))


@dataclass
class LegLiquidity:
    side: str
    strike: float
    bid: float | None
    ask: float | None
    mid: float | None
    spread: float | None
    spread_pct: float | None
    open_interest: float | None
    volume: float | None
    fillability: float | None

    def to_dict(self) -> dict:
        return asdict(self)


def fillability(bid, ask, oi, volume) -> float | None:
    cfg = _cfg()
    bid, ask, oi, volume = _f(bid), _f(ask), _f(oi), _f(volume)
    parts = []
    if bid is not None and ask is not None and ask > 0 and ask >= bid:
        mid = (bid + ask) / 2.0
        spread = ask - bid
        pct = spread / mid if mid > 0 else 1.0
        score = 1.0 - _scale(pct, cfg["spread_pct_good"], cfg["spread_pct_bad"])
        if spread <= cfg["spread_dollars_good"]:
            score = max(score, 0.8)
        parts.append(score)
    if oi is not None:
        parts.append(_scale(math.log10(max(oi, 1.0)), *cfg["oi_log10"]))
    if volume is not None and volume > 0:
        parts.append(_scale(math.log10(volume + 1.0), *cfg["volume_log10"]))
    return float(np.mean(parts)) if parts else None


def leg(row: pd.Series | dict, side: str) -> LegLiquidity:
    """One leg's profile from a chain row (`put_*` / `call_*` columns)."""
    get = row.get
    bid, ask = _f(get(f"{side}_bid")), _f(get(f"{side}_ask"))
    mid = (bid + ask) / 2.0 if bid is not None and ask is not None else _f(get(f"{side}_mark"))
    spread = ask - bid if bid is not None and ask is not None else None
    oi, volume = _f(get(f"{side}_open_interest")), _f(get(f"{side}_volume"))
    return LegLiquidity(
        side=side, strike=float(get("strike_price")), bid=bid, ask=ask, mid=mid,
        spread=spread, spread_pct=(spread / mid) if spread is not None and mid else None,
        open_interest=oi, volume=volume, fillability=fillability(bid, ask, oi, volume))


@dataclass
class PositionLiquidity:
    legs: list[LegLiquidity]
    weakest_index: int
    fillability: float | None
    min_open_interest: float | None
    total_spread: float | None          # sum of leg spreads, per share

    @property
    def weakest(self) -> LegLiquidity:
        return self.legs[self.weakest_index]

    def to_dict(self) -> dict:
        return {"fillability": self.fillability, "min_open_interest": self.min_open_interest,
                "total_spread": self.total_spread,
                "weakest_leg": f"{self.weakest.side} {self.weakest.strike:g}",
                "legs": [leg.to_dict() for leg in self.legs]}


def position(legs: list[LegLiquidity]) -> PositionLiquidity:
    """A position is as fillable as its least fillable leg."""
    scores = [l.fillability if l.fillability is not None else -1.0 for l in legs]
    weakest = int(np.argmin(scores))
    ois = [l.open_interest for l in legs if l.open_interest is not None]
    spreads = [l.spread for l in legs]
    return PositionLiquidity(
        legs=legs, weakest_index=weakest,
        fillability=legs[weakest].fillability,
        min_open_interest=min(ois) if ois else None,
        total_spread=sum(spreads) if all(s is not None for s in spreads) else None)


def walls(frame: pd.DataFrame, side: str = "put", multiple: float | None = None) -> pd.DataFrame:
    """Strikes of one expiration whose OI is >= `multiple` x the side's median."""
    multiple = multiple or _cfg()["wall_multiple"]
    column = f"{side}_open_interest"
    if column not in frame or frame[column].dropna().empty:
        return pd.DataFrame(columns=["strike", "open_interest", "multiple"])
    oi = frame[["strike_price", column]].dropna()
    oi = oi[oi[column] > 0]
    if oi.empty:
        return pd.DataFrame(columns=["strike", "open_interest", "multiple"])
    median = float(oi[column].median())
    hits = oi[oi[column] >= multiple * median]
    return pd.DataFrame({"strike": hits["strike_price"].astype(float),
                         "open_interest": hits[column].astype(float),
                         "multiple": hits[column].astype(float) / median}) \
        .sort_values("strike").reset_index(drop=True)


def nearest_wall_below(frame: pd.DataFrame, strike: float, side: str = "put") -> dict | None:
    found = walls(frame, side)
    found = found[found["strike"] <= strike]
    if found.empty:
        return None
    row = found.iloc[-1]
    return {"strike": float(row["strike"]), "open_interest": float(row["open_interest"]),
            "multiple": float(row["multiple"])}
