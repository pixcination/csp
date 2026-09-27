"""
Empirical move distributions -- probability of touch and probability of breach.

This generalises `weekly_move_analysis/move_analysis.py` from "one ticker,
Friday-to-Friday, run it by hand" into the probability engine the scanner
scores on. It is the answer to finding F-05: Black-Scholes probability-OTM is
a lognormal assumption that systematically overstates short-premium win rates,
and you already have direct evidence of it -- the AAL backtest came in at
74.2% against a theoretical 82.2%.

WHAT CHANGES VERSUS THE ORIGINAL SCRIPT
---------------------------------------
* **Every start date, not just Fridays.** Rolling overlapping windows give
  roughly 250 observations per year per horizon instead of 52. Tail
  percentiles stop being noise. (Overlap induces autocorrelation, so the
  *effective* sample is smaller than the nominal count -- `effective_n`
  reports it honestly rather than quoting an inflated confidence.)
* **Touch as well as terminal.** A cash-secured put is only assigned on a
  terminal close below the strike, but it is *managed* -- rolled, defended,
  panicked over -- when price touches it intraday. Both probabilities matter
  and they are very different numbers.
* **Volatility conditioning.** A stock's 7-day downside distribution when
  trailing vol sits in its top decile is not the same distribution as when it
  sits in its bottom decile. Sampling the unconditional history when today is
  visibly turbulent is how you get surprised. `vol_conditioned=True` restricts
  the sample to historically comparable volatility regimes.

All horizons are in TRADING days, because that is what option decay actually
follows. Calendar-day DTE is converted at the boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

DEFAULT_HORIZONS = (1, 2, 3, 5, 7, 10, 14, 21)
DEFAULT_PERCENTILES = (1, 5, 10, 20, 25, 50, 75, 90, 95, 99)


# --- Core window construction ---------------------------------------------

def _as_frame(daily: pd.DataFrame) -> pd.DataFrame:
    """Normalise column names and sort ascending. Accepts either the project's
    lowercase schema or a yfinance-style capitalised one."""
    df = daily.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
    else:
        df.index = pd.to_datetime(df.index)
        try:
            df.index = df.index.tz_localize(None)
        except (TypeError, AttributeError):
            pass
    needed = {"open", "high", "low", "close"}
    missing = needed - set(df.columns)
    if missing:
        raise ValueError(f"daily bars missing columns: {sorted(missing)}")
    return df[sorted(needed)].dropna(subset=["close"]).sort_index()


def build_windows(daily: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """One row per possible entry date: what happened over the next `horizon`
    trading days.

    Anchored on the entry day's CLOSE, which is when a weekly put is actually
    sold. Excursions look only at days t+1..t+horizon, so nothing on the entry
    bar itself leaks into the outcome.
    """
    df = _as_frame(daily)
    if len(df) < horizon + 2:
        return pd.DataFrame()

    close = df["close"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    n = len(close)
    last = n - horizon
    if last <= 0:
        return pd.DataFrame()

    idx = np.arange(last)
    entry = close[idx]

    # Rolling forward min/max over the window t+1 .. t+horizon.
    win_low = np.empty(last)
    win_high = np.empty(last)
    for offset in range(last):
        sl = slice(offset + 1, offset + 1 + horizon)
        win_low[offset] = low[sl].min()
        win_high[offset] = high[sl].max()

    terminal = close[idx + horizon]

    out = pd.DataFrame({
        "entry_date": df.index[:last],
        "exit_date": df.index[horizon:horizon + last],
        "entry_close": entry,
        "exit_close": terminal,
        "terminal_return": terminal / entry - 1.0,
        "mae": win_low / entry - 1.0,     # worst intraday drawdown, <= 0
        "mfe": win_high / entry - 1.0,    # best intraday advance,  >= 0
    })
    out["range_pct"] = out["mfe"] - out["mae"]
    return out


def attach_vol_regime(daily: pd.DataFrame, windows: pd.DataFrame,
                       rv_window: int = 20) -> pd.DataFrame:
    """Tag each window with the trailing realized vol known at entry.

    Strictly backward-looking: the value on the entry row uses only returns up
    to and including that day, so conditioning on it introduces no lookahead.
    """
    df = _as_frame(daily)
    log_ret = np.log(df["close"]).diff()
    rv = log_ret.rolling(rv_window).std() * np.sqrt(252.0)
    rv.name = "rv_at_entry"
    merged = windows.merge(rv.rename("rv_at_entry"), left_on="entry_date",
                            right_index=True, how="left")
    merged["rv_percentile"] = merged["rv_at_entry"].rank(pct=True)
    return merged


# --- Statistics ------------------------------------------------------------

@dataclass(frozen=True)
class MoveStats:
    ticker: str
    horizon_days: int
    n_observations: int
    effective_n: int
    lookback_label: str
    vol_conditioned: bool
    start_date: str
    end_date: str
    terminal_percentiles: dict
    mae_percentiles: dict
    mfe_percentiles: dict
    median_terminal: float
    mean_terminal: float
    downside_semi_deviation: float

    def to_dict(self) -> dict:
        return asdict(self)


def _pct_dict(series: pd.Series, percentiles) -> dict:
    clean = series.dropna()
    if clean.empty:
        return {f"p{p}": float("nan") for p in percentiles}
    values = np.percentile(clean.to_numpy(), percentiles)
    return {f"p{p}": float(v) for p, v in zip(percentiles, values)}


def _effective_n(nominal: int, horizon: int) -> int:
    """Overlapping windows share days, so they are not independent draws.

    A window of length H shares data with its 2H-1 neighbours; the standard
    correction for the variance of a mean of overlapping H-day windows scales
    the sample by roughly 1/H. Reporting this keeps confidence claims honest
    rather than quoting 6,000 'observations' that behave like 300.
    """
    return max(int(nominal / max(horizon, 1)), 1)


def compute_stats(daily: pd.DataFrame, ticker: str, horizon: int,
                   lookback_years: int = 0, percentiles=DEFAULT_PERCENTILES,
                   vol_conditioned: bool = False, rv_tolerance: float = 0.25,
                   current_rv: float | None = None,
                   min_observations: int = 60) -> MoveStats | None:
    """Empirical move statistics for one ticker at one horizon.

    lookback_years: 0 means all available history.
    vol_conditioned: restrict the sample to entry days whose trailing 20-day
        realized vol was within `rv_tolerance` (relative) of `current_rv`.
    """
    windows = build_windows(daily, horizon)
    if windows.empty:
        return None

    label = "all history" if not lookback_years else f"last {lookback_years}y"
    if lookback_years:
        cutoff = windows["entry_date"].max() - pd.DateOffset(years=lookback_years)
        windows = windows[windows["entry_date"] >= cutoff]

    if vol_conditioned:
        windows = attach_vol_regime(daily, windows)
        if current_rv is None:
            current_rv = float(windows["rv_at_entry"].dropna().iloc[-1]) \
                if windows["rv_at_entry"].notna().any() else None
        if current_rv:
            lo, hi = current_rv * (1 - rv_tolerance), current_rv * (1 + rv_tolerance)
            conditioned = windows[(windows["rv_at_entry"] >= lo)
                                   & (windows["rv_at_entry"] <= hi)]
            # Fall back to the unconditional sample rather than reporting a
            # statistic built on a dozen points.
            if len(conditioned) >= min_observations:
                windows = conditioned
                label += f" @ RV~{current_rv:.0%}"
            else:
                vol_conditioned = False

    if len(windows) < min_observations:
        return None

    downside = windows.loc[windows["terminal_return"] < 0, "terminal_return"]
    semi_dev = float(downside.std()) if len(downside) > 1 else float("nan")

    return MoveStats(
        ticker=ticker,
        horizon_days=horizon,
        n_observations=len(windows),
        effective_n=_effective_n(len(windows), horizon),
        lookback_label=label,
        vol_conditioned=vol_conditioned,
        start_date=str(windows["entry_date"].min().date()),
        end_date=str(windows["entry_date"].max().date()),
        terminal_percentiles=_pct_dict(windows["terminal_return"], percentiles),
        mae_percentiles=_pct_dict(windows["mae"], percentiles),
        mfe_percentiles=_pct_dict(windows["mfe"], percentiles),
        median_terminal=float(windows["terminal_return"].median()),
        mean_terminal=float(windows["terminal_return"].mean()),
        downside_semi_deviation=semi_dev,
    )


# --- The numbers the scanner actually consumes ----------------------------

@dataclass(frozen=True)
class BreachProbabilities:
    """Everything needed to price assignment risk for one candidate strike."""
    ticker: str
    horizon_days: int
    moneyness: float          # (strike - spot) / spot, negative for an OTM put
    prob_touch: float         # P(low over the window <= strike) -- management risk
    prob_terminal: float      # P(close at expiry <= strike) -- assignment risk
    prob_otm_empirical: float  # 1 - prob_terminal
    expected_loss_if_breached: float   # mean shortfall below strike, as a fraction of spot
    conditional_tail_loss: float       # mean of the worst 5% of breaches (CVaR-style)
    n_observations: int
    effective_n: int
    sample_label: str

    def to_dict(self) -> dict:
        return asdict(self)


def breach_probabilities(daily: pd.DataFrame, ticker: str, spot: float,
                          strike: float, horizon: int,
                          lookback_years: int = 0,
                          vol_conditioned: bool = True,
                          current_rv: float | None = None,
                          min_observations: int = 60) -> BreachProbabilities | None:
    """Empirical assignment and touch probabilities for a specific strike.

    This is the direct replacement for `options_math.probability_otm` in the
    composite score. It makes no distributional assumption -- it counts how
    often this stock has actually done this, at a comparable volatility.
    """
    if spot <= 0 or strike <= 0:
        return None
    windows = build_windows(daily, horizon)
    if windows.empty:
        return None

    label = "all history" if not lookback_years else f"last {lookback_years}y"
    if lookback_years:
        cutoff = windows["entry_date"].max() - pd.DateOffset(years=lookback_years)
        windows = windows[windows["entry_date"] >= cutoff]

    if vol_conditioned:
        windows = attach_vol_regime(daily, windows)
        rv_now = current_rv
        if rv_now is None and windows["rv_at_entry"].notna().any():
            rv_now = float(windows["rv_at_entry"].dropna().iloc[-1])
        if rv_now:
            lo, hi = rv_now * 0.75, rv_now * 1.25
            cond = windows[(windows["rv_at_entry"] >= lo) & (windows["rv_at_entry"] <= hi)]
            if len(cond) >= min_observations:
                windows = cond
                label += f" @ RV~{rv_now:.0%}"

    if len(windows) < min_observations:
        return None

    moneyness = strike / spot - 1.0            # negative for an OTM put
    touched = windows["mae"] <= moneyness
    breached = windows["terminal_return"] <= moneyness

    if breached.any():
        shortfall = (moneyness - windows.loc[breached, "terminal_return"])
        expected_loss = float(shortfall.mean())
        worst = shortfall.nlargest(max(int(len(shortfall) * 0.05), 1))
        tail_loss = float(worst.mean())
    else:
        expected_loss = 0.0
        tail_loss = 0.0

    return BreachProbabilities(
        ticker=ticker,
        horizon_days=horizon,
        moneyness=float(moneyness),
        prob_touch=float(touched.mean()),
        prob_terminal=float(breached.mean()),
        prob_otm_empirical=float(1.0 - breached.mean()),
        expected_loss_if_breached=expected_loss,
        conditional_tail_loss=tail_loss,
        n_observations=len(windows),
        effective_n=_effective_n(len(windows), horizon),
        sample_label=label,
    )


def strike_for_target_probability(daily: pd.DataFrame, spot: float, horizon: int,
                                   target_prob_otm: float = 0.85,
                                   lookback_years: int = 0) -> float | None:
    """Invert the empirical distribution: what strike has historically stayed
    OTM `target_prob_otm` of the time over this horizon?

    The empirical counterpart to picking a delta. Useful as a sanity check on
    whether the chain's 20-delta strike is actually a 20%-assignment strike
    for *this* stock, or whether the market is pricing it very differently.
    """
    windows = build_windows(daily, horizon)
    if windows.empty:
        return None
    if lookback_years:
        cutoff = windows["entry_date"].max() - pd.DateOffset(years=lookback_years)
        windows = windows[windows["entry_date"] >= cutoff]
    if windows.empty:
        return None
    quantile = float(np.percentile(windows["terminal_return"],
                                    (1.0 - target_prob_otm) * 100.0))
    return spot * (1.0 + quantile)


def recovery_statistics(daily: pd.DataFrame, drop_pct: float = 0.05,
                         max_days: int = 252, horizon: int = 7) -> dict:
    """If you were assigned, how long would your capital stay under water?

    This is the wheel's real holding-period risk. Assignment is not a loss --
    it is capital immobilised until the stock reclaims your basis. A name that
    historically takes 12 sessions to recover a 5% drop is a very different
    wheel candidate from one that takes 90, at identical win rates.

    The simulation mirrors what actually happens: for every entry day, assume
    a put struck `drop_pct` below spot with `horizon` trading days to run. If
    it finishes in the money, you are assigned at the strike -- then count the
    sessions until price closes back at or above that strike.

    Anchoring on each entry day (rather than on a running peak or a single
    fixed level) is what makes the number comparable to the assignment
    probabilities above: the same trades, measured a different way.
    """
    df = _as_frame(daily)
    close = df["close"].to_numpy(dtype=float)
    n = len(close)
    if n < horizon + 30:
        return {}

    durations: list[int] = []
    unrecovered = 0
    assignments = 0
    entries = 0

    for i in range(n - horizon):
        strike = close[i] * (1.0 - drop_pct)
        entries += 1
        expiry = i + horizon
        if close[expiry] >= strike:
            continue  # expired OTM, no assignment, no capital trapped
        assignments += 1
        k = expiry + 1
        limit = min(n, expiry + 1 + max_days)
        while k < limit and close[k] < strike:
            k += 1
        if k < limit:
            durations.append(k - expiry)
        else:
            unrecovered += 1

    if assignments == 0:
        return {"drop_pct": drop_pct, "horizon_days": horizon, "entries": entries,
                "assignments": 0, "assignment_rate": 0.0}

    arr = np.array(durations) if durations else np.array([float("nan")])
    return {
        "drop_pct": drop_pct,
        "horizon_days": horizon,
        "entries": entries,
        "assignments": assignments,
        "assignment_rate": assignments / entries,
        "recovered_within_max_days": len(durations),
        "unrecovered_rate": unrecovered / assignments,
        "median_sessions_to_recover": float(np.nanmedian(arr)),
        "p75_sessions_to_recover": float(np.nanpercentile(arr, 75)),
        "p90_sessions_to_recover": float(np.nanpercentile(arr, 90)),
        # The number that decides whether a wheel is worth running on a name:
        # capital-days consumed per assignment, versus the horizon you planned.
        "capital_days_multiple": float(np.nanmedian(arr)) / max(horizon, 1),
    }

# --- The call side ---------------------------------------------------------

@dataclass(frozen=True)
class UpsideProbabilities:
    """The mirror of BreachProbabilities, for a covered call.

    A short call is assigned when price finishes ABOVE the strike, and being
    called away is not a loss -- it is the wheel completing. So the useful
    numbers are different from the put side: `prob_called_away` is an outcome
    you often want, and the risk being measured is opportunity cost (the move
    you gave up above the strike), not shortfall.
    """
    ticker: str
    horizon_days: int
    moneyness: float            # (strike - spot) / spot, positive for an OTM call
    prob_touch: float           # P(high over the window >= strike)
    prob_called_away: float     # P(close at expiry >= strike)
    prob_expires_worthless: float
    expected_upside_forgone: float   # mean move above strike, as a fraction of spot
    conditional_tail_forgone: float  # mean of the largest 5% of overshoots
    n_observations: int
    effective_n: int
    sample_label: str

    def to_dict(self) -> dict:
        return asdict(self)


def upside_probabilities(daily: pd.DataFrame, ticker: str, spot: float,
                          strike: float, horizon: int,
                          lookback_years: int = 0,
                          vol_conditioned: bool = True,
                          current_rv: float | None = None,
                          min_observations: int = 60) -> UpsideProbabilities | None:
    """Empirical call-assignment odds and the upside you would forgo.

    Same machinery as `breach_probabilities`, reflected. Used by the
    covered-call selector to answer "how likely am I to be called away, and
    how much of the move am I capping?" without a lognormal assumption.
    """
    if spot <= 0 or strike <= 0:
        return None
    windows = build_windows(daily, horizon)
    if windows.empty:
        return None

    label = "all history" if not lookback_years else f"last {lookback_years}y"
    if lookback_years:
        cutoff = windows["entry_date"].max() - pd.DateOffset(years=lookback_years)
        windows = windows[windows["entry_date"] >= cutoff]

    if vol_conditioned:
        windows = attach_vol_regime(daily, windows)
        rv_now = current_rv
        if rv_now is None and windows["rv_at_entry"].notna().any():
            rv_now = float(windows["rv_at_entry"].dropna().iloc[-1])
        if rv_now:
            lo, hi = rv_now * 0.75, rv_now * 1.25
            cond = windows[(windows["rv_at_entry"] >= lo) & (windows["rv_at_entry"] <= hi)]
            if len(cond) >= min_observations:
                windows = cond
                label += f" @ RV~{rv_now:.0%}"

    if len(windows) < min_observations:
        return None

    moneyness = strike / spot - 1.0        # positive for an OTM call
    touched = windows["mfe"] >= moneyness
    called = windows["terminal_return"] >= moneyness

    if called.any():
        overshoot = windows.loc[called, "terminal_return"] - moneyness
        forgone = float(overshoot.mean())
        worst = overshoot.nlargest(max(int(len(overshoot) * 0.05), 1))
        tail = float(worst.mean())
    else:
        forgone = tail = 0.0

    return UpsideProbabilities(
        ticker=ticker, horizon_days=horizon, moneyness=float(moneyness),
        prob_touch=float(touched.mean()),
        prob_called_away=float(called.mean()),
        prob_expires_worthless=float(1.0 - called.mean()),
        expected_upside_forgone=forgone,
        conditional_tail_forgone=tail,
        n_observations=len(windows),
        effective_n=_effective_n(len(windows), horizon),
        sample_label=label,
    )
