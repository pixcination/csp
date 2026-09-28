"""
Expected move -- how far the options market expects the underlying to travel
by an expiration (Phase 12, roadmap B.5).

THREE METHODS, SHOWN SIDE BY SIDE
---------------------------------
* **tastytrade** (default -- what the tastytrade platform displays). Verified
  2026-09-27 against the tastytrade Help Center article "Expected Move in the
  tastytrade trading platform" (support.tastytrade.com, article 43000435415):

      EM = 0.6 x ATM straddle + 0.3 x 1st OTM strangle + 0.1 x 2nd OTM strangle

  Mids throughout. The ATM strike is the listed strike nearest the forward;
  the n-th OTM strangle is the n-th call above it plus the n-th put below it.
* **iv**: EM = S x IV x sqrt(DTE/365), IV = that expiration's ATM IV
  interpolated at the chain's implied forward (`surface.atm_vol`). A one
  standard deviation lognormal-ish move.
* **straddle**: EM = 0.85 x ATM straddle mid (the tastylive rule of thumb;
  the full straddle is reported too). The straddle is the expected ABSOLUTE
  move, ~0.8 sigma under a normal, so 0.85 x straddle lands near 0.68 sigma.

Every method is reported with 1x and 2x bands and a strike's distance in EM
units. `containment` measures how often past windows actually stayed inside
1x / 2x the EM, from stored IV when available and a trailing-RV proxy
otherwise -- the source is always labelled.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from core.paths import load_config

TASTY_WEIGHTS = (0.6, 0.3, 0.1)
STRADDLE_FACTOR = 0.85


# --- Formulas (pure) -------------------------------------------------------------

def iv_move(spot: float, iv: float, dte_days: float) -> float:
    """S x IV x sqrt(DTE/365)."""
    if not spot or not iv or iv <= 0 or spot <= 0:
        return float("nan")
    return float(spot) * float(iv) * math.sqrt(max(float(dte_days), 0.0) / 365.0)


def straddle_move(straddle_mid: float, factor: float = STRADDLE_FACTOR) -> float:
    return float(straddle_mid) * factor if straddle_mid and straddle_mid > 0 else float("nan")


def tasty_move(straddle: float, strangle1: float | None, strangle2: float | None,
               weights: tuple[float, float, float] = TASTY_WEIGHTS) -> float:
    """0.6 x straddle + 0.3 x 1st OTM strangle + 0.1 x 2nd OTM strangle. With
    a strangle missing (a thin chain) its weight is dropped and the rest
    renormalised -- flagged by the caller."""
    parts = [(weights[0], straddle), (weights[1], strangle1), (weights[2], strangle2)]
    usable = [(w, v) for w, v in parts if v is not None and np.isfinite(v) and v > 0]
    if not usable or usable[0][1] != straddle:
        return float("nan")
    total = sum(w for w, _ in usable)
    return sum(w * v for w, v in usable) / total


def distance_em(strike: float, spot: float, em: float) -> float:
    """(strike - spot) / EM: negative below spot. -1.0 = one expected move down."""
    if not em or not np.isfinite(em) or em <= 0:
        return float("nan")
    return (float(strike) - float(spot)) / float(em)


# --- From a chain ------------------------------------------------------------------

@dataclass
class ExpectedMove:
    expiration: str
    dte: int
    spot: float
    forward: float | None
    atm_strike: float | None
    atm_iv: float | None
    straddle: float | None
    strangle1: float | None
    strangle2: float | None
    em_tastytrade: float
    em_iv: float
    em_straddle: float
    method: str
    note: str = ""

    @property
    def em(self) -> float:
        """The configured method's EM, falling back to the next available."""
        order = [self.method] + [m for m in ("tastytrade", "iv", "straddle") if m != self.method]
        for m in order:
            value = getattr(self, f"em_{m}")
            if value is not None and np.isfinite(value) and value > 0:
                return float(value)
        return float("nan")

    def bands(self) -> dict:
        em = self.em
        return {"lower_2x": self.spot - 2 * em, "lower_1x": self.spot - em,
                "upper_1x": self.spot + em, "upper_2x": self.spot + 2 * em}

    def distance(self, strike: float) -> float:
        return distance_em(strike, self.spot, self.em)

    def to_dict(self) -> dict:
        out = asdict(self)
        out.update({"em": self.em, "em_pct": self.em / self.spot if self.spot else None,
                    **self.bands()})
        return out


def default_method() -> str:
    return (load_config().get("expected_move", {}) or {}).get("default_method", "tastytrade")


def _mid(row: pd.Series, side: str) -> float | None:
    """Quote mid (a zero bid is a real quote); the mark only without both sides."""
    bid, ask, mark = row.get(f"{side}_bid"), row.get(f"{side}_ask"), row.get(f"{side}_mark")
    if pd.notna(bid) and pd.notna(ask) and float(ask) > 0 and float(ask) >= float(bid) >= 0:
        return (float(bid) + float(ask)) / 2.0
    return float(mark) if pd.notna(mark) and float(mark) > 0 else None


def for_expiration(frame: pd.DataFrame, spot: float, dte: int,
                   method: str | None = None) -> ExpectedMove:
    """Expected move for ONE expiration (one root) of a chain snapshot."""
    from analytics import surface

    method = method or default_method()
    frame = frame.sort_values("strike_price").reset_index(drop=True)
    expiration = str(pd.Timestamp(frame["expiration"].iloc[0]).date()) if len(frame) else ""
    years = max(dte, 1) / 365.0
    notes = []

    forward = None
    atm_iv = None
    try:
        if {"call_bid", "call_ask", "put_bid", "put_ask"} <= set(frame.columns):
            forward = surface.implied_forward(frame, years)
            if forward:
                surf = surface.otm_surface(frame, forward, years)
                value = surface.atm_vol(surf, forward)
                atm_iv = value if np.isfinite(value) else None
    except Exception:
        pass
    anchor = forward or spot
    if forward is None:
        notes.append("no implied forward (one-sided quotes); ATM taken at spot")

    strikes = frame["strike_price"].astype(float).to_numpy()
    straddle = strangle1 = strangle2 = None
    atm = None
    if len(strikes):
        i = int(np.argmin(np.abs(strikes - anchor)))
        atm = float(strikes[i])
        call, put = _mid(frame.iloc[i], "call"), _mid(frame.iloc[i], "put")
        if call is not None and put is not None:
            straddle = call + put
        for n in (1, 2):
            up, down = i + n, i - n
            if down >= 0 and up < len(frame):
                c, p = _mid(frame.iloc[up], "call"), _mid(frame.iloc[down], "put")
                value = c + p if c is not None and p is not None else None
                if n == 1:
                    strangle1 = value
                else:
                    strangle2 = value
    if straddle is not None and (strangle1 is None or strangle2 is None):
        notes.append("an OTM strangle is missing; tastytrade weights renormalised")

    return ExpectedMove(
        expiration=expiration, dte=int(dte), spot=float(spot), forward=forward,
        atm_strike=atm, atm_iv=atm_iv, straddle=straddle, strangle1=strangle1,
        strangle2=strangle2,
        em_tastytrade=tasty_move(straddle, strangle1, strangle2) if straddle else float("nan"),
        em_iv=iv_move(anchor, atm_iv, dte) if atm_iv else float("nan"),
        em_straddle=straddle_move(straddle) if straddle else float("nan"),
        method=method, note="; ".join(notes))


def for_chain(chain: pd.DataFrame, spot: float, today, method: str | None = None) -> pd.DataFrame:
    """Expected move for every expiration (and root) in a chain snapshot."""
    if chain.empty:
        return pd.DataFrame()
    frame = chain.copy()
    frame["expiration"] = pd.to_datetime(frame["expiration"])
    keys = ["expiration"] + (["root_symbol"] if "root_symbol" in frame
                             and frame["root_symbol"].notna().any() else [])
    rows = []
    for key, group in frame.groupby(keys):
        exp = key[0] if isinstance(key, tuple) else key
        dte = (pd.Timestamp(exp).date() - today).days
        if dte < 0:
            continue
        em = for_expiration(group, spot, dte, method)
        row = em.to_dict()
        if len(keys) > 1:
            row["root_symbol"] = key[1]
        rows.append(row)
    return pd.DataFrame(rows)


# --- Historical containment --------------------------------------------------------

@dataclass
class Containment:
    horizon_days: int
    n_windows: int
    within_1x: float
    within_2x: float
    source: str
    expected_1x: float = 0.6827            # a normal distribution's share
    expected_2x: float = 0.9545

    def to_dict(self) -> dict:
        return asdict(self)


def containment(daily: pd.DataFrame, horizon_trading_days: int,
                iv_history: pd.Series | None = None, rv_window: int = 20,
                lookback_years: int | None = 10) -> Containment | None:
    """How often |close-to-close move over the horizon| stayed within 1x / 2x
    the expected move set at entry.

    The EM at each entry is S x vol x sqrt(h/252) on trading days, with vol =
    the stored IV on that date when `iv_history` (date-indexed, a fraction)
    has it, else the trailing `rv_window`-day realised vol -- a proxy, and
    labelled as one. Realised vol usually sits below implied, so an RV-proxy
    EM is narrower and containment reads LOWER than an IV EM would.
    """
    from analytics.moves import attach_vol_regime, build_windows

    windows = build_windows(daily, horizon_trading_days)
    if windows.empty:
        return None
    if lookback_years:
        cutoff = windows["entry_date"].max() - pd.DateOffset(years=lookback_years)
        windows = windows[windows["entry_date"] >= cutoff]
    windows = attach_vol_regime(daily, windows, rv_window=rv_window)
    vol = windows["rv_at_entry"].copy()
    used_iv = 0
    if iv_history is not None and not iv_history.empty:
        iv = pd.Series(iv_history.values, index=pd.to_datetime(iv_history.index))
        mapped = pd.to_datetime(windows["entry_date"]).map(iv)
        used_iv = int(mapped.notna().sum())
        vol = mapped.where(mapped.notna(), vol)
    windows = windows.assign(vol=vol).dropna(subset=["vol"])
    if windows.empty:
        return None
    em_pct = windows["vol"] * math.sqrt(horizon_trading_days / 252.0)
    move = windows["terminal_return"].abs()
    if used_iv == 0:
        source = f"trailing {rv_window}-day RV proxy (no stored IV)"
    elif used_iv < len(windows):
        source = f"stored IV on {used_iv} of {len(windows)} windows, RV proxy otherwise"
    else:
        source = "stored IV"
    return Containment(horizon_days=int(horizon_trading_days), n_windows=int(len(windows)),
                       within_1x=float((move <= em_pct).mean()),
                       within_2x=float((move <= 2 * em_pct).mean()), source=source)
