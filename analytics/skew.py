"""
Skew and single-name term structure -- two signals hiding in stored snapshots.

Both come free -- the chain captures already hold every call and put across
every expiration inside the DTE window, and nothing had ever read them for
shape.

Both are rebuilt from the snapshot's *quotes* rather than from the IV columns
on the greeks feed. Those columns each carry the vol computed at that option
symbol's own last update, which in a closed session is scattered across the
previous trading day; skew is a difference between two wings, so wings sampled
hours apart cannot measure it. `analytics/surface.py` carries the evidence and
does the reconstruction. The practical consequence is that every reading here
also carries the interval its bid/ask permits, and declines to classify when
that interval spans more than one category.

PUT SKEW
--------
The 25-delta put IV minus the 25-delta call IV. It measures what the market
charges for downside protection relative to upside, and for a put seller it is
close to a direct read on how well the trade is paid:

* **Rich skew** means crash insurance is expensive. You are being paid a large
  premium for the same delta -- often the best environment for the strategy.
* **Flat or inverted skew** is unusual in equities and means the downside is
  priced cheaply. Selling puts into it is selling the wrong side of a market
  that does not currently fear a fall.

Crucially, skew is *not* the same information as IV rank or IV/RV. A name can
sit at the middle of its own IV range while its skew is at an extreme, because
skew is a cross-sectional relationship within one expiration rather than a
comparison against that ticker's own history. It needs no accumulated data.

SINGLE-NAME TERM STRUCTURE
--------------------------
The VIX gate in `regime.py` does this for the index. The same logic applies per
ticker, and the chain already contains the inputs: near-dated IV over
further-dated IV. Backwardation in a single name usually means something
specific and dateable is expected -- an earnings print, a court ruling, a trial
readout. Sometimes the earnings calendar has already caught it. Often, for the
non-scheduled events, it has not, and the term structure is the only warning
you get.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from analytics.surface import (atm_vol, implied_forward, leg_at_delta,
                               otm_surface)

TARGET_DELTA = 0.25
#: Reject the measurement when the two legs are not roughly symmetric about the
#: forward. At 5 DTE the smile is steep enough that one point of moneyness
#: asymmetry flips the sign of the reading. Measured about the forward rather
#: than spot: a dividend payer with an ex-date inside the window has a forward
#: meaningfully below spot, and anchoring on spot would call that legitimate
#: reading asymmetric while passing a genuinely lopsided one.
MAX_MONEYNESS_ASYMMETRY = 0.015


@dataclass
class SkewReading:
    ticker: str
    expiration: dt.date
    dte: int
    put_iv: float
    call_iv: float
    atm_iv: float
    skew: float                  # put IV - call IV, in vol points
    skew_ratio: float            # put IV / call IV
    normalised_skew: float       # skew / ATM IV -- comparable across names
    put_strike: float
    call_strike: float
    moneyness_asymmetry: float   # |put_otm + call_otm|; 0 = perfectly symmetric
    forward: float               # from put-call parity, not spot
    skew_low: float              # narrowest skew the bid/ask permit
    skew_high: float             # widest skew the bid/ask permit
    measurable: bool             # False when the quote band contains zero
    classification: str
    favourable: bool
    note: str

    def to_dict(self) -> dict:
        out = asdict(self)
        out["expiration"] = str(self.expiration)
        return out

    @property
    def score(self) -> float:
        """0-1 component. Anchored absolutely, not percentile-ranked.

        Skew has a meaningful zero -- flat means the market charges the same
        for both tails. Ranking it within the universe would hide a day when
        everything is priced flat, which is exactly the day worth noticing.
        """
        if not self.measurable or not np.isfinite(self.normalised_skew):
            return float("nan")
        # -0.05 -> 0.0, 0.10 -> 0.5, 0.25 -> 1.0
        return float(np.clip((self.normalised_skew + 0.05) / 0.30, 0.0, 1.0))


def _interpolate_iv_at_delta(frame: pd.DataFrame, delta_col: str, iv_col: str,
                              strike_col: str, target: float
                              ) -> tuple[float, float] | None:
    """IV and strike at exactly |target| delta, by interpolation.

    WHY NOT THE NEAREST LISTED STRIKE. That was the first implementation, and
    on the real universe it reported 24 of 61 names with inverted skew -- a
    result that should be nearly impossible in equities, and was. Snapping each
    wing to its nearest strike lands the legs at *unequal moneyness*: measured
    across that run, the put leg averaged 2.9% out of the money against the
    call leg's 3.9%. At 5 DTE the smile is at its steepest, so one point of
    asymmetry is more than enough to invert the sign. The signal was reading
    strike-grid discretisation, not skew.

    Interpolating to the exact delta removes the discretisation. Extrapolation
    is refused: if the chain does not bracket the target delta, the answer is
    None rather than a guess off the end of the curve.
    """
    rows = frame[frame[delta_col].notna() & frame[iv_col].notna()].copy()
    if len(rows) < 2:
        return None
    rows["abs_delta"] = rows[delta_col].abs()
    rows = rows[(rows["abs_delta"] > 0.01) & (rows["abs_delta"] < 0.99)]
    rows = rows[rows[iv_col] > 0]
    rows = rows.sort_values("abs_delta")
    if len(rows) < 2:
        return None

    deltas = rows["abs_delta"].to_numpy(dtype=float)
    if not (deltas.min() <= target <= deltas.max()):
        return None

    ivs = rows[iv_col].to_numpy(dtype=float)
    strikes = rows[strike_col].astype(float).to_numpy()
    return (float(np.interp(target, deltas, ivs)),
            float(np.interp(target, deltas, strikes)))


def _choose_expiration(frame: pd.DataFrame, today: dt.date) -> dt.date | None:
    """Prefer an expiration inside the trading window, not merely the nearest.

    The nearest can be one day out, where quotes are thin and IV is dominated
    by minimum-tick effects -- SPY, MSFT, AMZN and IBIT all measured at 1 DTE
    in the first run. Reading the window the strategy actually trades gives a
    number that means something.
    """
    from core.paths import load_config
    entry = load_config().get("management", {}).get("entry", {})
    lo = entry.get("dte_min", 5)
    hi = entry.get("dte_max", 10) + 4

    dtes = sorted({(d - today).days for d in frame["expiration"].dt.date.unique()
                   if (d - today).days > 0})
    if not dtes:
        return None
    inside = [d for d in dtes if lo <= d <= hi]
    chosen = inside[0] if inside else min(dtes, key=lambda d: abs(d - lo))
    return today + dt.timedelta(days=chosen)


def _bucket(normalised: float) -> str:
    """The label a given normalised skew earns. Shared by the reading and by
    both ends of its uncertainty band, so 'measurable' means exactly: the
    quotes cannot move this reading into a different bucket."""
    if not np.isfinite(normalised):
        return "unknown"
    if normalised >= 0.15:
        return "rich"
    if normalised >= 0.05:
        return "normal"
    if normalised >= -0.02:
        return "flat"
    return "inverted"


def measure(chain: pd.DataFrame, ticker: str, spot: float,
             expiration: dt.date | None = None) -> SkewReading | None:
    """Put skew for one expiration, rebuilt from the snapshot's own quotes.

    Every input comes from this one snapshot: the forward from put-call parity,
    the IVs solved from bid/ask, the deltas from those IVs. Both wings
    therefore share one instant, which is the whole point -- skew is a
    difference between wings, so a feed that samples them at different times
    cannot measure it. See `analytics/surface.py` for the evidence.
    """
    if chain.empty or not spot:
        return None
    frame = chain.copy()
    if not {"put_bid", "put_ask", "call_bid", "call_ask", "strike_price"}.issubset(frame.columns):
        return None

    frame["expiration"] = pd.to_datetime(frame["expiration"])
    today = dt.date.today()
    if expiration is None:
        expiration = _choose_expiration(frame, today)
    if expiration is None:
        return None
    slice_ = frame[frame["expiration"].dt.date == expiration].copy()
    if slice_.empty:
        return None

    dte = (expiration - today).days
    years = max(dte, 1) / 365.0

    forward = implied_forward(slice_, years)
    if forward is None:
        return None
    surface = otm_surface(slice_, forward, years)
    if surface.empty:
        return None

    put = leg_at_delta(surface, "put", TARGET_DELTA)
    call = leg_at_delta(surface, "call", TARGET_DELTA)
    if put is None or call is None:
        return None
    if not (np.isfinite(put.iv) and np.isfinite(call.iv)) or min(put.iv, call.iv) <= 0:
        return None

    # Symmetry guard, now measured about the forward rather than spot. A
    # dividend payer with an ex-date inside the window has a forward well below
    # spot, and anchoring on spot would have called that legitimate reading
    # asymmetric while passing a genuinely lopsided one.
    asymmetry = abs((put.strike / forward - 1.0) + (call.strike / forward - 1.0))
    if asymmetry > MAX_MONEYNESS_ASYMMETRY:
        return None

    atm_iv = atm_vol(surface, forward)
    if not np.isfinite(atm_iv) or atm_iv <= 0:
        return None

    skew = put.iv - call.iv
    normalised = skew / atm_iv

    # What the quotes actually permit. The widest inversion is the lowest put
    # against the highest call, and vice versa.
    low = (put.iv_low - call.iv_high) / atm_iv
    high = (put.iv_high - call.iv_low) / atm_iv

    # Measurable means the quotes cannot move the reading into a different
    # bucket -- NOT that the band excludes zero. Those are different tests, and
    # the second one is wrong: a genuinely flat skew has a band straddling zero
    # by construction, so requiring exclusion would make "flat" unreportable
    # forever, which is the one reading the strategy most needs to hear.
    measurable = bool(np.isfinite(low) and np.isfinite(high)
                      and _bucket(low) == _bucket(high))

    if not measurable:
        span = (f"{low:+.0%} to {high:+.0%}" if np.isfinite(low) and np.isfinite(high)
                else "unbounded -- one wing has no bid")
        label, favourable = "unmeasurable", False
        note = (f"The bid/ask on these strikes allows a skew anywhere from {span} "
                f"of ATM vol, so the quotes cannot even fix the sign. The mid says "
                f"{normalised:+.0%}, but that is the midpoint of a spread, not a "
                f"measurement. Re-read during regular trading hours.")
    elif _bucket(normalised) == "rich":
        label, favourable = "rich", True
        note = (f"Downside is priced {normalised:.0%} of ATM vol above the upside "
                f"(quotes allow {low:+.0%} to {high:+.0%}). Put sellers are being paid "
                f"well for the same delta -- this is the environment the strategy is "
                f"built for.")
    elif _bucket(normalised) == "normal":
        label, favourable = "normal", True
        note = (f"Ordinary equity skew ({normalised:.0%} of ATM, quotes allow "
                f"{low:+.0%} to {high:+.0%}). Nothing unusual either way.")
    elif _bucket(normalised) == "flat":
        label, favourable = "flat", False
        note = (f"Skew is flat ({normalised:+.0%} of ATM) and the quotes are tight "
                f"enough to say so ({low:+.0%} to {high:+.0%}). The market is not "
                f"charging for downside protection, so you are not being paid for "
                f"taking it.")
    else:
        label, favourable = "inverted", False
        note = (f"Skew is INVERTED ({normalised:+.0%} of ATM, quotes allow {low:+.0%} "
                f"to {high:+.0%}) -- calls are priced above puts. Rare in equities and "
                f"here the spread is tight enough that it is not a quoting artifact. "
                f"Usually an expected upside event.")

    return SkewReading(
        ticker=ticker, expiration=expiration, dte=dte,
        put_iv=put.iv, call_iv=call.iv, atm_iv=atm_iv,
        skew=skew, skew_ratio=put.iv / call.iv,
        normalised_skew=normalised,
        put_strike=put.strike, call_strike=call.strike,
        moneyness_asymmetry=asymmetry,
        forward=forward, skew_low=low, skew_high=high, measurable=measurable,
        classification=label, favourable=favourable, note=note)


# --- Single-name term structure -------------------------------------------

@dataclass
class TermStructure:
    ticker: str
    near_dte: int
    far_dte: int
    near_iv: float
    far_iv: float
    ratio: float
    state: str                  # 'contango' | 'flat' | 'backwardation'
    event_suspected: bool
    note: str

    def to_dict(self) -> dict:
        return asdict(self)


def term_structure(chain: pd.DataFrame, ticker: str, spot: float) -> TermStructure | None:
    """Near-dated ATM IV against further-dated ATM IV, for one name.

    `regime.py` does this for the index with VIX9D/VIX3M. Doing it per ticker
    catches the single-name equivalent: a dateable event the market knows about
    and the earnings calendar may not list -- a court date, a readout, a
    regulatory decision.
    """
    if chain.empty or not spot:
        return None
    frame = chain.copy()
    if not {"put_bid", "put_ask", "call_bid", "call_ask", "strike_price"}.issubset(frame.columns):
        return None
    frame["expiration"] = pd.to_datetime(frame["expiration"])
    today = dt.date.today()
    frame["dte"] = (frame["expiration"].dt.date - today).apply(lambda d: d.days)
    frame = frame[frame["dte"] > 0]
    if frame["dte"].nunique() < 2:
        return None

    # ATM vol per expiration, solved from that expiration's own quotes against
    # its own parity-implied forward. Reading the feed's IV column here would
    # be inconsistent with `measure` above, and for the same reason: each
    # expiration's forward differs (a dividend inside one window and not the
    # other moves it), so a single spot-based reference distorts the ratio
    # this function exists to compute.
    atm_by_expiry = {}
    for dte, group in frame.groupby("dte"):
        years = max(int(dte), 1) / 365.0
        forward = implied_forward(group, years)
        if forward is None:
            continue
        surface = otm_surface(group, forward, years)
        if surface.empty:
            continue
        value = atm_vol(surface, forward)
        if np.isfinite(value) and value > 0:
            atm_by_expiry[int(dte)] = float(value)

    if len(atm_by_expiry) < 2:
        return None

    near_dte = min(atm_by_expiry)
    far_dte = max(atm_by_expiry)
    near_iv, far_iv = atm_by_expiry[near_dte], atm_by_expiry[far_dte]
    if far_iv <= 0:
        return None
    ratio = near_iv / far_iv

    if ratio >= 1.10:
        state, suspected = "backwardation", True
        note = (f"Near-dated IV is {ratio - 1:.0%} ABOVE the {far_dte}-day. Something "
                f"dateable is expected inside {near_dte} days -- an earnings print, a "
                f"ruling, a readout. Check the calendar; if it shows nothing, the "
                f"market knows something it does not list.")
    elif ratio >= 1.02:
        state, suspected = "flat", False
        note = (f"Term structure is flat ({ratio:.2f}). Mild near-term uncertainty.")
    else:
        state, suspected = "contango", False
        note = (f"Normal contango ({ratio:.2f}) -- near-dated vol below "
                f"{far_dte}-day. Ordinary conditions for selling the front week.")

    return TermStructure(ticker, near_dte, far_dte, near_iv, far_iv, ratio,
                          state, suspected, note)


# --- Universe sweep --------------------------------------------------------

def universe_skew(tickers: list[str] | None = None, reporter=None) -> pd.DataFrame:
    """Skew and term structure across the universe, from stored snapshots."""
    from core.paths import load_universe
    from core.progress import NullReporter
    from data_sources import chains

    tickers = tickers or load_universe()
    reporter = reporter or NullReporter()
    rows = []

    with reporter.stage("skew", "Skew and term structure", total=len(tickers)):
        for ticker in tickers:
            try:
                chain, under = chains.load_chain(ticker)
                spot = chains.spot_from_underlying(under)
                if chain.empty or not spot:
                    reporter.advance(1, note=f"{ticker} no snapshot")
                    continue
                reading = measure(chain, ticker, spot)
                structure = term_structure(chain, ticker, spot)
                record = {"ticker": ticker, "spot": spot}
                if reading:
                    record.update(reading.to_dict())
                    record["skew_score"] = reading.score
                if structure:
                    record.update({f"ts_{k}": v for k, v in structure.to_dict().items()
                                    if k != "ticker"})
                if len(record) > 2:
                    rows.append(record)
                    reporter.advance(1, note=(f"{ticker} "
                                               f"{reading.classification if reading else '-'}"))
                else:
                    reporter.advance(1, note=f"{ticker} no IV in snapshot")
            except Exception as exc:
                reporter.advance(1, note=f"{ticker} error")
                reporter.log(f"{ticker}: {type(exc).__name__}: {exc}")

    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    if "normalised_skew" in frame.columns:
        frame = frame.sort_values("normalised_skew", ascending=False)
    return frame.reset_index(drop=True)
