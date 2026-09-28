"""
Probability engine -- three models, one repricing core (Phase 13, roadmap B.4).

    G  market-implied   GBM at the short leg's IV (risk-neutral drift at the
                        risk-free rate). What the option prices imply.
    H  historical       5-day block bootstrap of the stock's own daily log
                        returns (price basis, last `lookback_years`), blocks
                        drawn only from days whose trailing 20-day RV was
                        within +/-25% of today's. What this stock has done
                        at a similar volatility.
    T  technical        H restricted further to start days in a similar
                        technical state: the same trend state (Phase 10),
                        the same RSI bucket, and the same distance bucket
                        (in ATR) to today's strongest respected support.
                        When too few days match, the conditions are relaxed
                        in that order (support, then RSI, then trend) and
                        the relaxation is recorded; with still too few, T
                        is reported as "fell back to H" and left out of the
                        blend rather than counted twice.

On every path every leg is repriced DAILY with Black-Scholes: sticky strike
(each leg keeps its own IV), optionally mean-reverting toward realised vol
(`iv_reversion_half_life_days`, off by default), and multiplied by
(1 - earnings_crush) from an earnings date inside the trade onward. The
position's P&L per share on day j is its credit plus the signed value of
its legs, so it is generic over `strategies.base.Position`.

OUTPUTS per trade and model (and blended)
-----------------------------------------
* P(reach X% of max profit at ANY point by day d), X in the request's
  profit targets: a curve over d, the value by expiry, and the median
  calendar days to reach it among paths that do. "100%" is P(expire
  worthless) -- only realisable by holding to expiry.
* P(profit at expiry), P(touch the short strike) on daily closes (intraday
  touches are more frequent -- this understates), P(assignment) for a CSP
  (S_T below the strike), P(max loss) for a spread, and P(the short leg's
  delta goes beyond `management.defense.roll_when_delta_beyond`).
* Per management policy -- close at each target, hold to expiry, and where
  the trade has more than `time_stop_dte` days, a 21-DTE time stop and each
  target combined with it -- the expected P&L, P(profit), expected holding
  days and the annualised return on buying power, ALL NET OF FEES (entry,
  close, and assignment/exercise at expiry). A target whose net dollar gain
  when hit is under `management.exit.min_net_gain_to_close_early` is marked
  `below_min_gain`: at 7 DTE the table shows why early targets do not pay.

LOSS STOPS AND THE SHIPPED POLICY (Phase 17, review decision 3)
---------------------------------------------------------------
A trade given `loss_stop_multiple` k gets stop policies alongside the rest:
`stop_{k}x` (hold, but close once the loss reaches k x the credit -- k x the
debit for a debit trade), `close_{X}_stop_{k}x`, and with the time stop
`..._or_{T}dte`. `TradeSpec.managed` names the rules actually run (for a put
spread: management.spread -- target above the hold horizon, the 2x stop, the
21-DTE time stop; for a spec position: its `exit` block), and with
`headline_policy: shipped` that policy is the headline the sheet ranks on.
Two model fixes keep stops from looking artificially cheap or dear:

* **Stops trigger on marks, not closes.** Each step also carries the day's
  low and high: H and T bootstrap the real daily low/high (relative to the
  prior close) alongside the close; G draws them from the Brownian bridge
  between consecutive closes. A stop fires when the position's value at the
  day's worse extreme crosses the level, and fills AT the level -- or at the
  close when the close is already beyond it (a gap through the stop).
* **IV moves with spot** (H and T). Each leg's IV scales by
  exp(beta x log return), clipped to `iv_clip` x entry IV. beta is the index
  spot-vol beta (d ln VIX / d ln SPY, estimated from the reference data,
  about -5) scaled to the name by rho x sigma_SPY / sigma_name -- the part of
  the name's move the market explains. G stays sticky-strike: its marks must
  remain a martingale for G to be the zero-edge baseline.

The blend is a weighted mean of the models present (config weights, default
equal). It is NOT claimed to be better than any single model until
`scripts/validate_prob_engine.py` and paper-book calibration have scored it.

Reproducible: one seed per run (`seed`), antithetic normals for G.
Antithetics do not apply to the bootstraps (negating returns would destroy
the skew the historical models exist to capture).
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.special import ndtr

from core.paths import load_config

MODELS = ("G", "H", "T")


# --- Configuration ---------------------------------------------------------------

@dataclass
class EngineConfig:
    n_paths: int = 20_000
    seed: int = 20260927
    antithetic: bool = True
    block_days: int = 5
    lookback_years: int = 10
    rv_window: int = 20
    vol_band: float = 0.25
    min_starts: int = 250
    weights: dict = field(default_factory=lambda: {"G": 1 / 3, "H": 1 / 3, "T": 1 / 3})
    rate: float = 0.045
    iv_reversion_half_life_days: float | None = None
    earnings_crush: float = 0.30
    roll_delta: float = -0.45
    time_stop_dte: int = 21
    headline_policy: str = "auto"
    auto_hold_max_dte: int = 14
    min_net_gain: float = 5.0
    rsi_buckets: tuple = (30.0, 50.0, 70.0)
    support_atr_buckets: tuple = (1.0, 3.0)
    # Phase 17
    intraday_stops: bool = True
    spot_vol: bool = True
    spot_vol_index_beta: float | None = None     # None = estimate from VIX vs SPY
    spot_vol_lookback_years: int = 5
    spot_vol_name_years: int = 1
    iv_clip: tuple = (0.5, 3.0)

    @classmethod
    def from_config(cls) -> "EngineConfig":
        cfg = load_config()
        pe = dict(cfg.get("prob_engine", {}) or {})
        out = cls()
        for key, value in pe.items():
            if hasattr(out, key) and value is not None:
                setattr(out, key, tuple(value) if isinstance(value, list)
                        and (key.endswith("buckets") or key == "iv_clip") else value)
        out.rate = float((cfg.get("analytics", {}) or {}).get("risk_free_rate", out.rate))
        out.roll_delta = float(((cfg.get("management", {}) or {}).get("defense", {}) or {})
                               .get("roll_when_delta_beyond", out.roll_delta))
        out.min_net_gain = float(((cfg.get("management", {}) or {}).get("exit", {}) or {})
                                 .get("min_net_gain_to_close_early", out.min_net_gain))
        return out


# --- Black-Scholes on arrays -----------------------------------------------------------

def bs_price(spot, strike, tau, vol, rate, option_type: str):
    """Vectorised Black-Scholes. `tau` in years; tau <= 0 -> intrinsic."""
    spot = np.asarray(spot, dtype=float)
    tau = np.asarray(tau, dtype=float)
    vol = np.asarray(vol, dtype=float)
    intrinsic = np.maximum(strike - spot, 0.0) if option_type == "put" \
        else np.maximum(spot - strike, 0.0)
    live = (tau > 0) & (vol > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        sqrt_t = np.sqrt(np.where(live, tau, 1.0))
        v = np.where(live, vol, 1.0)
        d1 = (np.log(spot / strike) + (rate + 0.5 * v * v) * np.where(live, tau, 1.0)) / (v * sqrt_t)
        d2 = d1 - v * sqrt_t
        disc = np.exp(-rate * np.where(live, tau, 0.0))
        if option_type == "put":
            price = strike * disc * ndtr(-d2) - spot * ndtr(-d1)
        else:
            price = spot * ndtr(d1) - strike * disc * ndtr(d2)
    return np.where(live, price, intrinsic), np.where(live, d1, np.nan)


def bs_price_grid(spot: np.ndarray, strike: float, tau: np.ndarray, vol: np.ndarray,
                  rate: float, option_type: str,
                  log_spot: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Black-Scholes over a (paths, steps) grid where every column shares one
    `tau` and one `vol` (1-D, length steps). Only live columns are priced;
    tau <= 0 columns are intrinsic. Pass `log_spot` when the paths already
    hold log prices: np.log over the grid costs more than a normal CDF."""
    price = (np.maximum(strike - spot, 0.0) if option_type == "put"
             else np.maximum(spot - strike, 0.0))
    d1_out = np.full(spot.shape, np.nan)
    live = np.flatnonzero((tau > 0) & (vol > 0))
    if live.size:
        # Live columns are normally a leading block (only expiry has tau 0):
        # slicing avoids copying the grid the way fancy indexing does.
        cols = slice(0, live.size) if live[-1] == live.size - 1 else live
        s = spot[:, cols]
        t, v = tau[cols], vol[cols]
        sq = v * np.sqrt(t)
        d1 = (log_spot[:, cols] if log_spot is not None else np.log(s)) - math.log(strike)
        d1 += (rate + 0.5 * v * v) * t
        d1 /= sq
        disc = strike * np.exp(-rate * t)
        if option_type == "put":
            first = ndtr(sq - d1)                 # N(-d2)
            first *= disc
            second = ndtr(-d1)
            second *= s
            price[:, cols] = first - second
        else:
            first = ndtr(d1)
            first *= s
            second = ndtr(d1 - sq)
            second *= disc
            price[:, cols] = first - second
        d1_out[:, cols] = d1
    return price, d1_out


# --- Paths -----------------------------------------------------------------------------

@dataclass
class PathSet:
    model: str
    log_returns: np.ndarray | None      # (paths, n_steps) cumulative log return from spot
    label: str
    n_starts: int = 0
    effective_n: int = 0
    flag: str = ""
    normals: np.ndarray | None = None   # G: cumulative standard normals (paths, n_steps)
    log_low: np.ndarray | None = None   # Phase 17: day's low / high, cumulative log vs spot
    log_high: np.ndarray | None = None

    @property
    def available(self) -> bool:
        return self.log_returns is not None or self.normals is not None


def g_normals(n_paths: int, n_steps: int, rng: np.random.Generator,
              antithetic: bool = True) -> np.ndarray:
    """Cumulative standard normals, scaled per trade by its vol (shared by
    every strike of a ticker and expiration)."""
    half = (n_paths + 1) // 2 if antithetic else n_paths
    z = rng.standard_normal((half, n_steps))
    if antithetic:
        z = np.concatenate([z, -z])[:n_paths]
    return np.cumsum(z, axis=1)


def g_log_returns(normals: np.ndarray, vol: float, rate: float, years: float) -> np.ndarray:
    """GBM log returns on each step with total variance vol^2 x years."""
    n_steps = normals.shape[1]
    step = years / n_steps
    t = step * np.arange(1, n_steps + 1)
    return (rate - 0.5 * vol * vol) * t + vol * math.sqrt(step) * normals


def _daily_log_returns(daily: pd.DataFrame, years: int) -> pd.DataFrame:
    frame = daily.sort_values("date").reset_index(drop=True).copy()
    frame["date"] = pd.to_datetime(frame["date"])
    close = frame["close"].astype(float)
    frame["ret"] = np.log(close / close.shift(1))
    if {"low", "high"} <= set(frame.columns):
        frame["lo"] = np.log(frame["low"].astype(float) / close.shift(1))
        frame["hi"] = np.log(frame["high"].astype(float) / close.shift(1))
    frame["rv"] = frame["ret"].rolling(20).std() * math.sqrt(252)
    if years:
        frame = frame[frame["date"] >= frame["date"].max() - pd.DateOffset(years=years)]
    return frame.dropna(subset=["ret"]).reset_index(drop=True)


def bootstrap(returns: np.ndarray, starts: np.ndarray, n_paths: int, n_steps: int,
              block: int, rng: np.random.Generator,
              extremes: tuple[np.ndarray, np.ndarray] | None = None):
    """Block bootstrap: each path strings together blocks of `block`
    consecutive daily returns beginning at randomly chosen `starts`.

    With `extremes` = (low, high) per day as log moves from the prior close,
    the same days' extremes are returned too: (closes, lows, highs), each
    cumulative from spot. The random draws are identical either way."""
    n_blocks = -(-n_steps // block)
    picks = rng.choice(starts, size=(n_paths, n_blocks))
    index = picks[..., None] + np.arange(block)
    index = index.reshape(n_paths, n_blocks * block)[:, :n_steps]
    closes = np.cumsum(returns[index], axis=1)
    if extremes is None:
        return closes
    prior = np.zeros_like(closes)
    prior[:, 1:] = closes[:, :-1]
    return closes, prior + extremes[0][index], prior + extremes[1][index]


def _extremes(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray] | None:
    """Per day, the low and high as log moves from the prior close, bounded
    so the close lies between them (bad prints happen)."""
    if not {"lo", "hi"} <= set(frame.columns):
        return None
    ret = frame["ret"].to_numpy(float)
    lo = np.minimum(np.nan_to_num(frame["lo"].to_numpy(float), nan=0.0), np.minimum(ret, 0.0))
    hi = np.maximum(np.nan_to_num(frame["hi"].to_numpy(float), nan=0.0), np.maximum(ret, 0.0))
    return lo, hi


def bridge_extremes(log_closes: np.ndarray, step_vol: float, rng: np.random.Generator
                    ) -> tuple[np.ndarray, np.ndarray]:
    """G: the minimum and maximum of a Brownian bridge between consecutive
    log closes (exact for GBM): m = (a + b - sqrt((b - a)^2 - 2 s^2 ln U)) / 2."""
    prior = np.zeros_like(log_closes)
    prior[:, 1:] = log_closes[:, :-1]
    diff2 = (log_closes - prior) ** 2
    var = step_vol * step_vol
    u = rng.random((2,) + log_closes.shape)
    low = (prior + log_closes - np.sqrt(diff2 - 2.0 * var * np.log(u[0]))) / 2.0
    high = (prior + log_closes + np.sqrt(diff2 - 2.0 * var * np.log(u[1]))) / 2.0
    return low, high


def h_paths(daily: pd.DataFrame, n_steps: int, cfg: EngineConfig,
            rng: np.random.Generator) -> tuple[PathSet, pd.DataFrame, np.ndarray]:
    frame = _daily_log_returns(daily, cfg.lookback_years)
    if len(frame) < cfg.block_days * 20:
        return PathSet("H", None, "not enough history"), frame, np.array([], int)
    returns = frame["ret"].to_numpy(float)
    last_start = len(frame) - cfg.block_days
    candidates = np.arange(0, last_start + 1)
    rv = frame["rv"].to_numpy(float)
    rv_now = rv[-1] if np.isfinite(rv[-1]) else np.nanmedian(rv)
    label = f"last {cfg.lookback_years}y"
    lo, hi = rv_now * (1 - cfg.vol_band), rv_now * (1 + cfg.vol_band)
    conditioned = candidates[(rv[candidates] >= lo) & (rv[candidates] <= hi)]
    if len(conditioned) >= cfg.min_starts:
        starts = conditioned
        label += f" @ RV~{rv_now:.0%}"
    else:
        starts = candidates
        label += f" (RV~{rv_now:.0%} matched only {len(conditioned)} days; unconditioned)"
    ext = _extremes(frame) if cfg.intraday_stops else None
    drawn = bootstrap(returns, starts, cfg.n_paths, n_steps, cfg.block_days, rng, ext)
    paths, low, high = drawn if ext is not None else (drawn, None, None)
    return (PathSet("H", paths, label, len(starts), len(starts) // cfg.block_days,
                    log_low=low, log_high=high),
            frame, starts)


def technical_state(frame: pd.DataFrame, cfg: EngineConfig,
                    support_column: str | None) -> pd.DataFrame:
    """Per day: trend state, RSI bucket, support-distance bucket (ATR units)."""
    from analytics import trend_state
    out = pd.DataFrame({"date": pd.to_datetime(frame["date"])})
    out["trend"] = trend_state.classify(frame).to_numpy()
    out["rsi_bucket"] = np.digitize(frame["rsi"].to_numpy(float), cfg.rsi_buckets)
    if support_column and support_column in frame:
        distance = (frame["close"] - frame[support_column]) / frame["atr"]
        bucket = np.digitize(distance.to_numpy(float), cfg.support_atr_buckets)
        bucket = np.where(distance.to_numpy(float) < 0, -1, bucket)   # below the level
        out["support_bucket"] = np.where(np.isfinite(distance), bucket, -9)
    else:
        out["support_bucket"] = 0
    return out


def t_paths(returns_frame: pd.DataFrame, h_starts: np.ndarray, state: pd.DataFrame,
            n_steps: int, cfg: EngineConfig, rng: np.random.Generator) -> PathSet:
    """H's vol-conditioned start days, further restricted to today's state."""
    if state is None or state.empty or not len(h_starts):
        return PathSet("T", None, "no technical state", flag="fell back to H")
    merged = returns_frame[["date"]].merge(state, on="date", how="left")
    today = state.iloc[-1]
    checks = [("trend", today["trend"]), ("rsi_bucket", today["rsi_bucket"]),
              ("support_bucket", today["support_bucket"])]
    dropped: list[str] = []
    while True:
        mask = np.ones(len(h_starts), bool)
        for column, value in checks:
            mask &= (merged[column].to_numpy()[h_starts] == value)
        starts = h_starts[mask]
        if len(starts) >= cfg.min_starts or not checks:
            break
        dropped.append(checks.pop()[0])
    returns = returns_frame["ret"].to_numpy(float)
    if len(starts) < cfg.min_starts or not checks:
        # With every condition relaxed T IS H; counting it again would
        # double H's weight in the blend.
        return PathSet("T", None, "too few similar days", n_starts=int(len(starts)),
                       flag="fell back to H")
    kept = ", ".join(f"{c}={v}" for c, v in checks)
    label = f"state {kept}" + (f" (relaxed: {', '.join(dropped)})" if dropped else "")
    ext = _extremes(returns_frame) if cfg.intraday_stops else None
    drawn = bootstrap(returns, starts, cfg.n_paths, n_steps, cfg.block_days, rng, ext)
    paths, low, high = drawn if ext is not None else (drawn, None, None)
    return PathSet("T", paths, label, int(len(starts)), int(len(starts)) // cfg.block_days,
                   flag=f"relaxed {', '.join(dropped)}" if dropped else "",
                   log_low=low, log_high=high)


# --- Evaluation ------------------------------------------------------------------------

@dataclass
class TradeSpec:
    """What the engine needs about one trade (built from a sheet row)."""
    position: object                    # strategies.base.Position
    spot: float
    dte_calendar: int
    dte_trading: int
    contracts: int
    bpr: float                          # dollars, all contracts
    cash_settled: bool = False
    event_day: int | None = None        # calendar days to an earnings date inside the trade
    rv: float | None = None             # realised vol target for IV mean reversion
    # Phase 17
    loss_stop_multiple: float | None = None   # stop policies at k x the credit (or debit)
    managed: dict | None = None         # the shipped rules: {target, stop, time_stop}
    spot_vol_beta: float | None = None  # d ln IV / d ln S for this name (H and T)


def managed_policy_name(managed: dict | None, dte_calendar: int) -> str | None:
    """The policy key the shipped rules map to, or None without rules.
    `managed` = {target: % of max profit or None, stop: k or None,
    time_stop: calendar DTE or None}; a time stop only exists for a trade
    entered above it."""
    if not managed:
        return None
    target, stop, time_stop = managed.get("target"), managed.get("stop"), managed.get("time_stop")
    time_stop = int(time_stop) if time_stop and dte_calendar > int(time_stop) else None
    name = f"close_{int(target)}" if target else ""
    if stop:
        name += f"{'_' if name else ''}stop_{float(stop):g}x"
    if time_stop:
        name = f"{name}_or_{time_stop}dte" if name else f"time_stop_{time_stop}"
    return name or "hold"


def _fees(spec: TradeSpec) -> tuple[float, float, np.ndarray]:
    """(entry fees, close fees, per-leg expiry fee) in dollars for the position."""
    from analytics import costs
    n = max(int(spec.contracts), 1)
    legs = spec.position.legs
    options = [l for l in legs if not getattr(l, "is_stock", False)]
    entry = costs.legs_open([("sell" if l.side == "short" else "buy", n * l.qty)
                             for l in options]).total
    # Phase 16: buying the shares of a covered position is an entry cost too.
    entry += sum(costs.stock_buy(100 * n * l.qty).total
                 for l in legs if getattr(l, "is_stock", False) and l.side == "long")
    close = costs.legs_close([("buy" if l.side == "short" else "sell", n * l.qty)
                              for l in options]).total
    per_leg = np.array([0.0 if getattr(l, "is_stock", False)
                        else costs.assignment(n * l.qty).total for l in legs])
    return entry, close, per_leg


def _position_value(pos, spec: TradeSpec, cfg: EngineConfig, log_spot: np.ndarray,
                    cal_elapsed: np.ndarray, tau: np.ndarray, iv_scale: np.ndarray | None,
                    want_delta: bool = False):
    """Signed value of every leg per share over a (paths, steps) grid of log
    spot. `iv_scale` (paths, steps) multiplies each leg's IV path (spot-vol
    dynamics); None = sticky strike. Returns (value, short put delta or None)."""
    n_steps = log_spot.shape[1]
    spot_paths = np.exp(log_spot)
    value = np.zeros_like(spot_paths)
    short_delta = None
    offsets = pos.expiry_offsets() if hasattr(pos, "expiry_offsets") else [0] * len(pos.legs)
    for leg, offset in zip(pos.legs, offsets):
        if getattr(leg, "is_stock", False):
            # Phase 16: stock legs, net of the risk-free return the share
            # capital would have earned in cash -- otherwise G's risk-neutral
            # drift shows up as "edge" on every covered call.
            carry = spec.spot * (np.exp(cfg.rate * cal_elapsed / 365.0) - 1.0)
            value += leg.sign * leg.qty * (spot_paths - carry)
            continue
        iv0 = leg.iv if leg.iv and leg.iv > 0 else 0.3
        iv = np.full(n_steps, iv0)
        if cfg.iv_reversion_half_life_days and spec.rv:
            decay = np.exp(-math.log(2) * cal_elapsed / cfg.iv_reversion_half_life_days)
            iv = spec.rv + (iv0 - spec.rv) * decay
        if spec.event_day is not None and cfg.earnings_crush:
            iv = np.where(cal_elapsed >= spec.event_day, iv * (1 - cfg.earnings_crush), iv)
        # A leg expiring after the front keeps `offset` more days (calendars);
        # when the front expires it is valued at the forward vol the entry
        # term structure implies (strategies.base.Position.later_leg_vol).
        leg_tau = tau + offset / 365.0 if offset else tau
        if offset:
            iv = np.array(iv, dtype=float, copy=True)
            iv[-1] = pos.later_leg_vol(leg, offset, spec.dte_calendar) * (iv[-1] / iv0)
        if iv_scale is None:
            price, d1 = bs_price_grid(spot_paths, leg.strike, leg_tau, iv, cfg.rate,
                                      leg.option_type, log_spot)
        else:
            price, d1 = bs_price(spot_paths, leg.strike, leg_tau[None, :],
                                 iv[None, :] * iv_scale, cfg.rate, leg.option_type)
        value += leg.sign * leg.qty * price
        if want_delta and leg.side == "short" and short_delta is None \
                and leg.option_type == "put":
            short_delta = np.where(np.isfinite(d1), ndtr(d1) - 1.0,
                                   np.where(spot_paths < leg.strike, -1.0, 0.0))
    return value, short_delta


def _iv_scale(log_moves: np.ndarray, beta: float | None, cfg: EngineConfig):
    if not beta:
        return None
    lo, hi = cfg.iv_clip
    return np.clip(np.exp(beta * log_moves), lo, hi)


def evaluate(spec: TradeSpec, log_returns: np.ndarray, targets: list[int],
             cfg: EngineConfig, log_low: np.ndarray | None = None,
             log_high: np.ndarray | None = None, spot_vol_beta: float | None = None) -> dict:
    """Every output for one trade under one set of paths.

    `log_low` / `log_high` (Phase 17): each day's extremes, cumulative log vs
    spot -- stops then trigger on them; without them, on closes.
    `spot_vol_beta`: leg IVs follow spot (None = sticky strike)."""
    pos = spec.position
    n_paths, n_steps = log_returns.shape
    n = max(int(spec.contracts), 1)
    cal_elapsed = spec.dte_calendar * np.arange(1, n_steps + 1) / n_steps
    tau = np.maximum(spec.dte_calendar - cal_elapsed, 0.0) / 365.0
    tau[-1] = 0.0
    log_spot = math.log(spec.spot) + log_returns
    spot_paths = np.exp(log_spot)

    value, short_delta = _position_value(pos, spec, cfg, log_spot, cal_elapsed, tau,
                                         _iv_scale(log_returns, spot_vol_beta, cfg),
                                         want_delta=True)
    pnl = pos.credit + value                         # per share, (paths, steps)
    offsets = pos.expiry_offsets() if hasattr(pos, "expiry_offsets") else [0] * len(pos.legs)
    max_profit = pos.max_profit
    s_t = spot_paths[:, -1]
    entry, close_fee, leg_fee = _fees(spec)
    options = [(l, o) for l, o in zip(pos.legs, offsets) if not getattr(l, "is_stock", False)]
    # At the front expiry: expiring legs in the money are exercised/assigned;
    # later legs are closed (a close commission each).
    itm = np.stack([(l.intrinsic(s_t) > 0) & (o == 0) if not getattr(l, "is_stock", False)
                    else np.zeros(n_paths, bool) for l, o in zip(pos.legs, offsets)], axis=1)
    expiry_fees = itm.astype(float) @ leg_fee
    later = [l for l, o in options if o > 0]
    if later:
        from analytics import costs
        n_c = max(int(spec.contracts), 1)
        expiry_fees = expiry_fees + costs.legs_close(
            [("buy" if l.side == "short" else "sell", n_c * l.qty) for l in later]).total

    out: dict = {"n_paths": n_paths}
    out["pop"] = float(np.mean(pnl[:, -1] > 0))
    shorts = [l for l, _ in options if l.side == "short"]
    longs = [l for l, _ in options if l.side == "long"]
    if shorts:
        # Touch / ITM on either side: puts below their highest short strike,
        # calls above their lowest (an iron condor or strangle has both).
        put_k = [l.strike for l in shorts if l.option_type == "put"]
        call_k = [l.strike for l in shorts if l.option_type == "call"]
        crossed = np.zeros_like(spot_paths, dtype=bool)
        if put_k:
            crossed |= spot_paths <= max(put_k)
        if call_k:
            crossed |= spot_paths >= min(call_k)
        out["p_touch_short"] = float(np.mean(crossed.any(axis=1)))
        out["p_short_itm"] = float(np.mean(crossed[:, -1]))
    multi = bool(getattr(pos, "multi_expiry", False))
    if longs and not multi:
        out["p_max_loss"] = float(np.mean(pnl[:, -1] <= -pos.max_loss + 1e-9))
    else:
        out["p_assign"] = out.get("p_short_itm")
    if short_delta is not None:
        out["p_roll_trigger"] = float(np.mean((short_delta <= cfg.roll_delta).any(axis=1)))

    curves: dict[int, np.ndarray] = {}
    first_hit: dict[int, np.ndarray] = {}
    for x in targets:
        if x >= 100:
            hit = pnl[:, -1] >= max_profit - 1e-9
            out["p_hit_100"] = float(np.mean(hit))
            curve = np.zeros(n_steps)
            curve[-1] = out["p_hit_100"]
            curves[100] = curve
            continue
        reached = pnl >= (x / 100.0) * max_profit
        any_hit = reached.any(axis=1)
        first = np.where(any_hit, reached.argmax(axis=1), n_steps)
        first_hit[x] = first
        curve = np.cumsum(np.bincount(first, minlength=n_steps + 1)[:n_steps]) / n_paths
        curves[x] = curve
        out[f"p_hit_{x}"] = float(curve[-1])
        out[f"median_days_{x}"] = float(np.median(cal_elapsed[first[any_hit]])) \
            if any_hit.any() else None

    # --- Policies (net of fees, all contracts) ---
    def settle(exit_step: np.ndarray, closed_early: np.ndarray,
               per_share: np.ndarray | None = None) -> dict:
        rows = np.arange(n_paths)
        if per_share is None:
            per_share = pnl[rows, exit_step]
        dollars = per_share * 100.0 * n - entry
        dollars -= np.where(closed_early, close_fee, expiry_fees)
        days = cal_elapsed[exit_step]
        mean_days = float(np.mean(days))
        ev = float(np.mean(dollars))
        return {"ev": ev, "p_profit": float(np.mean(dollars > 0)), "days": mean_days,
                "annualised": ev / spec.bpr * 365.0 / max(mean_days, 1.0) if spec.bpr else None,
                "ev_per_day_bpr": ev / spec.bpr / max(mean_days, 1.0) if spec.bpr else None}

    last = n_steps - 1
    never = np.full(n_paths, n_steps)

    def time_step(dte_stop: int | None):
        if not dte_stop or spec.dte_calendar <= dte_stop:
            return None
        remaining = spec.dte_calendar - cal_elapsed
        return int(np.argmax(remaining <= dte_stop))

    # Loss stop (Phase 17): first day the position's value at the day's worse
    # extreme is k x the credit (debit) under water; filled at the level, or
    # at the close when the close is already through it.
    stop_ks = sorted({float(k) for k in [spec.loss_stop_multiple,
                                        (spec.managed or {}).get("stop")] if k})
    stops: dict[float, tuple[np.ndarray, np.ndarray]] = {}
    if stop_ks:
        basis = abs(float(pos.credit))
        worst = pnl
        if log_low is not None and log_high is not None and basis > 0:
            puts_only = all(getattr(l, "is_stock", False) or l.option_type == "put"
                            for l in pos.legs)
            for extreme, needed in ((log_low, True), (log_high, not puts_only)):
                if not needed:
                    continue
                ext_log = math.log(spec.spot) + extreme
                v, _ = _position_value(pos, spec, cfg, ext_log, cal_elapsed, tau,
                                       _iv_scale(extreme, spot_vol_beta, cfg))
                worst = np.minimum(worst, pos.credit + v)
        for k in stop_ks:
            level = -k * basis
            crossed = worst <= level + 1e-12 if basis > 0 else np.zeros_like(pnl, dtype=bool)
            step = np.where(crossed.any(axis=1), crossed.argmax(axis=1), n_steps)
            rows = np.arange(n_paths)
            at = np.minimum(step, last)
            fill = np.where(pnl[rows, at] <= level, pnl[rows, at], level)
            stops[k] = (step, fill)

    def policy(first_target: np.ndarray | None, stop: float | None,
               t_step: int | None) -> dict:
        target_step = first_target if first_target is not None else never
        stop_step, stop_fill = stops[stop] if stop else (never, None)
        timed = np.full(n_paths, t_step if t_step is not None else n_steps)
        chosen = np.minimum(np.minimum(target_step, stop_step), timed)
        exit_step = np.minimum(chosen, last)
        via_stop = (stop_step <= np.minimum(target_step, timed)) & (stop_step < n_steps)
        closed = (exit_step < last) | via_stop | ((timed < n_steps) & (exit_step == timed))
        per_share = None
        if stop:
            rows = np.arange(n_paths)
            per_share = np.where(via_stop, stop_fill, pnl[rows, exit_step])
        result = settle(exit_step, closed, per_share)
        if stop:
            result["p_stopped"] = float(np.mean(via_stop))
        return result

    policies: dict[str, dict] = {}
    policies["hold"] = policy(None, None, None)
    stop_step_t = time_step(cfg.time_stop_dte)
    if stop_step_t is not None:
        policies[f"time_stop_{cfg.time_stop_dte}"] = policy(None, None, stop_step_t)
    for x, first in first_hit.items():
        gain = (x / 100.0) * max_profit * 100.0 * n - entry - close_fee
        variants = [(f"close_{x}", None, None)]
        if stop_step_t is not None:
            variants.append((f"close_{x}_or_{cfg.time_stop_dte}dte", None, stop_step_t))
        for k in stop_ks:
            variants.append((f"close_{x}_stop_{k:g}x", k, None))
            if stop_step_t is not None:
                variants.append((f"close_{x}_stop_{k:g}x_or_{cfg.time_stop_dte}dte", k,
                                 stop_step_t))
        for name, k, t_step in variants:
            result = policy(first, k, t_step)
            result["net_gain_when_hit"] = gain
            result["below_min_gain"] = bool(gain < cfg.min_net_gain)
            policies[name] = result
    for k in stop_ks:
        policies[f"stop_{k:g}x"] = policy(None, k, None)
        if stop_step_t is not None:
            policies[f"stop_{k:g}x_or_{cfg.time_stop_dte}dte"] = policy(None, k, stop_step_t)
    # The shipped rules, when they need a combination not generated above
    # (e.g. a spec's own time stop).
    managed = managed_policy_name(spec.managed, spec.dte_calendar)
    if managed and managed not in policies:
        m = spec.managed or {}
        target = int(m["target"]) if m.get("target") else None
        if target and target not in first_hit:
            reached = pnl >= (target / 100.0) * max_profit
            first_t = np.where(reached.any(axis=1), reached.argmax(axis=1), n_steps)
        else:
            first_t = first_hit.get(target) if target else None
        policies[managed] = policy(first_t, float(m["stop"]) if m.get("stop") else None,
                                   time_step(m.get("time_stop")))
    if managed:
        out["managed_policy"] = managed
    out["policies"] = policies
    out["curves"] = curves
    out["curve_days"] = cal_elapsed
    return out


def headline_policy(policies: dict, dte_calendar: int, cfg: EngineConfig,
                    managed: str | None = None) -> str:
    """`shipped` (Phase 17): the rules actually run -- `managed`, the trade's
    own policy name -- when it has one, else `auto`. `auto`: hold to expiry
    at or under auto_hold_max_dte (fees make early closes uneconomic --
    exit_rules.py), else close at 50% (or the nearest target)."""
    if cfg.headline_policy == "shipped" and managed and managed in policies:
        return managed
    if cfg.headline_policy not in ("auto", "shipped") and cfg.headline_policy in policies:
        return cfg.headline_policy
    if dte_calendar <= cfg.auto_hold_max_dte:
        return "hold"
    targets = sorted(int(k.split("_")[1]) for k in policies
                     if k.startswith("close_") and k.count("_") == 1)
    if not targets:
        return "hold"
    # Close at the target nearest 50%. The 21-DTE time stop is reported but
    # not the headline: measured 2026-09-27 on SPY/QQQ 30-45 DTE spreads it
    # lowered EV under H and T (it pays extrinsic still priced at IV > RV).
    return f"close_{min(targets, key=lambda t: abs(t - 50))}"


# --- Spot-vol beta (Phase 17) -------------------------------------------------------------

_INDEX_BETA: dict[int, float | None] = {}


def index_spot_vol_beta(cfg: EngineConfig) -> float | None:
    """d ln VIX / d ln SPY over `spot_vol_lookback_years` (reference data),
    unless `spot_vol_index_beta` fixes it. Cached per lookback."""
    if cfg.spot_vol_index_beta is not None:
        return float(cfg.spot_vol_index_beta)
    years = int(cfg.spot_vol_lookback_years)
    if years in _INDEX_BETA:
        return _INDEX_BETA[years]
    beta = None
    try:
        from core.paths import reference_dir
        from data_sources.yfinance_sync import load_daily
        vix = pd.read_parquet(reference_dir() / "vol_indices.parquet", columns=["date", "VIX"])
        spy = load_daily("SPY", basis="price")[["date", "close"]]
        vix["date"], spy["date"] = pd.to_datetime(vix["date"]), pd.to_datetime(spy["date"])
        m = vix.merge(spy, on="date").sort_values("date")
        m = m[m["date"] >= m["date"].max() - pd.DateOffset(years=years)]
        dv, r = np.log(m["VIX"]).diff(), np.log(m["close"]).diff()
        ok = dv.notna() & r.notna()
        if ok.sum() > 100:
            beta = float(np.polyfit(r[ok], dv[ok], 1)[0])
    except Exception:
        beta = None
    _INDEX_BETA[years] = beta
    return beta


def name_spot_vol_beta(daily: pd.DataFrame, cfg: EngineConfig,
                       spy: pd.DataFrame | None = None) -> float | None:
    """The index beta scaled to one name by the part of its move the market
    explains: beta_index x rho(name, SPY) x sigma_SPY / sigma_name, over
    `spot_vol_name_years`. None when disabled or not estimable."""
    if not cfg.spot_vol:
        return None
    index_beta = index_spot_vol_beta(cfg)
    if index_beta is None or daily is None or len(daily) < 60:
        return None
    try:
        if spy is None:
            from data_sources.yfinance_sync import load_daily
            spy = load_daily("SPY", basis="price")
        a = daily[["date", "close"]].copy()
        b = spy[["date", "close"]].rename(columns={"close": "spy"})
        a["date"], b["date"] = pd.to_datetime(a["date"]), pd.to_datetime(b["date"])
        m = a.merge(b, on="date").sort_values("date")
        m = m[m["date"] >= m["date"].max() - pd.DateOffset(years=int(cfg.spot_vol_name_years))]
        r_name, r_spy = np.log(m["close"]).diff().dropna(), np.log(m["spy"]).diff().dropna()
        if len(r_name) < 60 or r_name.std() <= 0:
            return None
        rho = float(np.corrcoef(r_name, r_spy)[0, 1])
        return float(index_beta * rho * r_spy.std() / r_name.std())
    except Exception:
        return None


# --- Per-ticker driver ----------------------------------------------------------------

@dataclass
class TickerPaths:
    """Paths shared by every trade of one ticker and horizon (n_steps)."""
    H: PathSet
    T: PathSet
    normals: np.ndarray


def ticker_paths(daily: pd.DataFrame, n_steps: int, cfg: EngineConfig,
                 rng: np.random.Generator, tech_frame: pd.DataFrame | None,
                 support_column: str | None) -> TickerPaths:
    normals = g_normals(cfg.n_paths, n_steps, rng, cfg.antithetic)
    h, frame, starts = h_paths(daily, n_steps, cfg, rng)
    if h.available and tech_frame is not None and not tech_frame.empty:
        state = technical_state(tech_frame, cfg, support_column)
        t = t_paths(frame, starts, state, n_steps, cfg, rng)
    else:
        t = PathSet("T", None, "no technical frame", flag="fell back to H")
    return TickerPaths(h, t, normals)


def run_trade(spec: TradeSpec, paths: TickerPaths, targets: list[int],
              cfg: EngineConfig) -> dict:
    """All three models for one trade, plus the blend."""
    years = spec.dte_calendar / 365.0
    short = next((l for l in spec.position.legs if l.side == "short"), spec.position.legs[0])
    vol = short.iv if short.iv and short.iv > 0 else 0.3
    # Phase 16: short puts AND calls (condors, strangles) -- one lognormal
    # cannot fit both wings of a skew, so G uses the mean short-leg IV
    # rather than favouring one side.
    shorts = [l for l in spec.position.legs if l.side == "short"
              and not getattr(l, "is_stock", False) and l.iv and l.iv > 0]
    if len({l.option_type for l in shorts}) > 1:
        vol = float(np.mean([l.iv for l in shorts]))
    results: dict[str, dict] = {}
    meta: dict[str, dict] = {}
    g = g_log_returns(paths.normals, vol, cfg.rate, years)
    wants_stops = bool(spec.loss_stop_multiple or (spec.managed or {}).get("stop"))
    g_low = g_high = None
    if wants_stops and cfg.intraday_stops:
        rng = np.random.default_rng(cfg.seed + g.shape[1])
        g_low, g_high = bridge_extremes(g, vol * math.sqrt(years / g.shape[1]), rng)
    # G stays sticky strike: its marks must remain a martingale (zero edge).
    results["G"] = evaluate(spec, g, targets, cfg, g_low, g_high)
    meta["G"] = {"label": f"GBM at the short leg's IV {vol:.0%}", "effective_n": cfg.n_paths,
                 "flag": ""}
    for name in ("H", "T"):
        ps: PathSet = getattr(paths, name)
        meta[name] = {"label": ps.label, "effective_n": ps.effective_n, "flag": ps.flag,
                      "n_starts": ps.n_starts}
        if ps.log_returns is not None:
            beta = spec.spot_vol_beta if cfg.spot_vol else None
            results[name] = evaluate(spec, ps.log_returns, targets, cfg,
                                     ps.log_low if wants_stops else None,
                                     ps.log_high if wants_stops else None, beta)
    if beta_note := (spec.spot_vol_beta if cfg.spot_vol else None):
        for name in ("H", "T"):
            if name in meta and name in results:
                meta[name]["label"] += f"; IV beta {beta_note:+.1f}"
    out = {"models": results, "meta": meta, "blend": blend(results, cfg)}
    managed = managed_policy_name(spec.managed, spec.dte_calendar)
    if managed:
        out["blend"]["managed_policy"] = managed
    return out


SCALARS = ("pop", "p_touch_short", "p_short_itm", "p_max_loss", "p_assign", "p_roll_trigger")


def blend(results: dict[str, dict], cfg: EngineConfig) -> dict:
    """Weighted mean over the models present (renormalised)."""
    weights = {m: float(cfg.weights.get(m, 0.0)) for m in results}
    total = sum(weights.values())
    if not total:
        return {}
    w = {m: v / total for m, v in weights.items()}
    out: dict = {"weights": w}
    keys = set()
    for r in results.values():
        keys |= {k for k in r if k in SCALARS or k.startswith(("p_hit_", "median_days_"))}
    for key in keys:
        values = [(w[m], r.get(key)) for m, r in results.items() if r.get(key) is not None]
        if values:
            weight = sum(a for a, _ in values)
            out[key] = sum(a * v for a, v in values) / weight
    policies = {}
    for name in next(iter(results.values()))["policies"]:
        policy = {}
        for key in ("ev", "p_profit", "days", "annualised", "ev_per_day_bpr", "p_stopped"):
            values = [(w[m], r["policies"][name].get(key)) for m, r in results.items()
                      if r["policies"].get(name, {}).get(key) is not None]
            if values:
                weight = sum(a for a, _ in values)
                policy[key] = sum(a * v for a, v in values) / weight
        first = next(iter(results.values()))["policies"][name]
        for key in ("net_gain_when_hit", "below_min_gain"):
            if key in first:
                policy[key] = first[key]
        policies[name] = policy
    out["policies"] = policies
    return out
