"""
Volatility-regime gate from the VIX term structure.

`data_refresh.py` has been downloading VIX, VIX9D, VIX3M, VVIX and SKEW into
parquet for months, and nothing has ever read them. This module is the reader.

WHY TERM STRUCTURE AND NOT VIX LEVEL
------------------------------------
The absolute level of VIX tells you how expensive options are; the *shape* of
the curve tells you what the market expects to happen next. VIX9D over VIX3M
is near-dated implied vol divided by further-dated implied vol:

* **Contango (ratio < 1)** is the normal state, roughly 80% of trading days.
  Near-term risk is priced below longer-term risk. Short premium works.
* **Backwardation (ratio > 1)** means the market is paying up for immediate
  protection. It is uncommon, it clusters, and it is when short puts get
  assigned en masse -- not one at a time, but the whole book at once, which
  in a cash-secured account means every dollar of collateral converts to
  stock simultaneously and the wheel stops turning.

The gate scales size rather than hard-stopping. Backwardation is not a
prediction of a crash; it is a statement that the distribution has widened,
and the correct response to a wider distribution is a smaller position, not
an empty one.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.paths import load_config, reference_dir

VOL_INDEX_FILE = "vol_indices.parquet"


@dataclass(frozen=True)
class RegimeReading:
    as_of: str | None
    vix: float
    vix9d: float
    vix3m: float
    vvix: float
    skew: float
    term_ratio: float           # VIX9D / VIX3M
    state: str                  # 'calm' | 'normal' | 'stressed' | 'unknown'
    size_multiplier: float
    tradable: bool
    headline: str
    detail: str

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of, "vix": self.vix, "vix9d": self.vix9d,
            "vix3m": self.vix3m, "vvix": self.vvix, "skew": self.skew,
            "term_ratio": self.term_ratio, "regime": self.state,
            "size_multiplier": self.size_multiplier, "tradable": self.tradable,
        }


def _load_frame() -> pd.DataFrame | None:
    path = reference_dir() / VOL_INDEX_FILE
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception:
        return None
    if df.empty:
        return None
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").set_index("date")
    return df


def _latest(df: pd.DataFrame, column: str) -> float:
    if column not in df.columns:
        return float("nan")
    series = df[column].dropna()
    return float(series.iloc[-1]) if len(series) else float("nan")


def current() -> RegimeReading:
    """Read the latest VIX complex and classify the regime.

    Returns a permissive 'unknown' reading when the reference data is missing,
    so a stale parquet degrades the gate rather than blocking every trade --
    but the headline says so plainly instead of pretending conditions are calm.
    """
    cfg = load_config().get("regime", {})
    contango_max = float(cfg.get("contango_ratio_max", 0.95))
    backward_min = float(cfg.get("backwardation_ratio_min", 1.05))
    stressed_mult = float(cfg.get("size_multiplier_backwardation", 0.5))

    df = _load_frame()
    if df is None:
        return RegimeReading(None, *([float("nan")] * 5), float("nan"),
                              "unknown", 1.0, True,
                              "Volatility regime unknown",
                              "data/reference/vol_indices.parquet is missing. "
                              "Run the reference refresh to enable the regime gate; "
                              "sizing is unrestricted until then.")

    as_of = str(df.index[-1].date()) if isinstance(df.index, pd.DatetimeIndex) else None
    vix = _latest(df, "VIX")
    vix9d = _latest(df, "VIX9D")
    vix3m = _latest(df, "VIX3M")
    vvix = _latest(df, "VVIX")
    skew = _latest(df, "SKEW")
    ratio = vix9d / vix3m if np.isfinite(vix9d) and np.isfinite(vix3m) and vix3m > 0 else float("nan")

    if not np.isfinite(ratio):
        return RegimeReading(as_of, vix, vix9d, vix3m, vvix, skew, ratio,
                              "unknown", 1.0, True,
                              "Volatility regime unknown",
                              "VIX9D or VIX3M is missing from the reference data.")

    if ratio <= contango_max:
        return RegimeReading(
            as_of, vix, vix9d, vix3m, vvix, skew, ratio, "calm", 1.0, True,
            f"Contango ({ratio:.2f}) - normal premium-selling conditions",
            f"9-day implied vol is {1 - ratio:.0%} below 3-month. Near-term risk is "
            f"priced cheaply relative to the horizon, which is the ordinary state "
            f"and the one short premium is built for. Full size.")

    if ratio < backward_min:
        return RegimeReading(
            as_of, vix, vix9d, vix3m, vvix, skew, ratio, "normal", 0.75, True,
            f"Flat curve ({ratio:.2f}) - transitional",
            f"The term structure has flattened. Not backwardation, but the cushion "
            f"is gone. Three-quarter size, and prefer strikes further out of the money "
            f"over larger positions.")

    return RegimeReading(
        as_of, vix, vix9d, vix3m, vvix, skew, ratio, "stressed", stressed_mult, False,
        f"Backwardation ({ratio:.2f}) - stand down",
        f"9-day implied vol is {ratio - 1:.0%} ABOVE 3-month. The market is paying up "
        f"for immediate protection. This is when short puts get assigned as a group, "
        f"not one at a time - in a cash-secured account that converts the whole book "
        f"to stock at once. Half size at most, and no new positions in names you would "
        f"not want to hold for a quarter.")


def apply_to_sizing(contracts: int, reading: RegimeReading | None = None) -> tuple[int, str]:
    """Scale a contract count by the regime multiplier.

    Returns (adjusted_contracts, explanation). Never silently changes size --
    the caller is expected to surface the explanation next to the number.
    """
    r = reading or current()
    if not load_config().get("regime", {}).get("enabled", True):
        return contracts, "regime gate disabled in config"
    adjusted = max(int(contracts * r.size_multiplier), 0)
    if adjusted == contracts:
        return contracts, r.headline
    return adjusted, f"{r.headline} - size reduced {contracts} -> {adjusted}"


def history(days: int = 250) -> pd.DataFrame:
    """Recent term-structure history, for the Command Center chart."""
    df = _load_frame()
    if df is None:
        return pd.DataFrame()
    out = df.tail(days).copy()
    if {"VIX9D", "VIX3M"}.issubset(out.columns):
        out["term_ratio"] = out["VIX9D"] / out["VIX3M"]
    return out
