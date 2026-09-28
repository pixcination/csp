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

    @classmethod
    def from_config(cls) -> "EngineConfig":
        cfg = load_config()
        pe = dict(cfg.get("prob_engine", {}) or {})
        out = cls()
        for key, value in pe.items():
            if hasattr(out, key) and value is not None:
                setattr(out, key, tuple(value) if isinstance(value, list)
                        and key.endswith("buckets") else value)
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
    frame["rv"] = frame["ret"].rolling(20).std() * math.sqrt(252)
    if years:
        frame = frame[frame["date"] >= frame["date"].max() - pd.DateOffset(years=years)]
    return frame.dropna(subset=["ret"]).reset_index(drop=True)


def bootstrap(returns: np.ndarray, starts: np.ndarray, n_paths: int, n_steps: int,
              block: int, rng: np.random.Generator) -> np.ndarray:
    """Block bootstrap: each path strings together blocks of `block`
    consecutive daily returns beginning at randomly chosen `starts`."""
    n_blocks = -(-n_steps // block)
    picks = rng.choice(starts, size=(n_paths, n_blocks))
    index = picks[..., None] + np.arange(block)
    sample = returns[index].reshape(n_paths, n_blocks * block)[:, :n_steps]
    return np.cumsum(sample, axis=1)


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
    paths = bootstrap(returns, starts, cfg.n_paths, n_steps, cfg.block_days, rng)
    return (PathSet("H", paths, label, len(starts), len(starts) // cfg.block_days),
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
    paths = bootstrap(returns, starts, cfg.n_paths, n_steps, cfg.block_days, rng)
    return PathSet("T", paths, label, int(len(starts)), int(len(starts)) // cfg.block_days,
                   flag=f"relaxed {', '.join(dropped)}" if dropped else "")


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


def evaluate(spec: TradeSpec, log_returns: np.ndarray, targets: list[int],
             cfg: EngineConfig) -> dict:
    """Every output for one trade under one set of paths."""
    pos = spec.position
    n_paths, n_steps = log_returns.shape
    n = max(int(spec.contracts), 1)
    cal_elapsed = spec.dte_calendar * np.arange(1, n_steps + 1) / n_steps
    tau = np.maximum(spec.dte_calendar - cal_elapsed, 0.0) / 365.0
    tau[-1] = 0.0
    log_spot = math.log(spec.spot) + log_returns
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
        price, d1 = bs_price_grid(spot_paths, leg.strike, leg_tau, iv, cfg.rate,
                                  leg.option_type, log_spot)
        value += leg.sign * leg.qty * price
        if leg.side == "short" and short_delta is None and leg.option_type == "put":
            short_delta = np.where(np.isfinite(d1), ndtr(d1) - 1.0,
                                   np.where(spot_paths < leg.strike, -1.0, 0.0))

    pnl = pos.credit + value                         # per share, (paths, steps)
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
    def settle(exit_step: np.ndarray, closed_early: np.ndarray) -> dict:
        rows = np.arange(n_paths)
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
    policies: dict[str, dict] = {}
    hold = settle(np.full(n_paths, last), np.zeros(n_paths, bool))
    policies["hold"] = hold
    stop_step = None
    if spec.dte_calendar > cfg.time_stop_dte:
        remaining = spec.dte_calendar - cal_elapsed
        stop_step = int(np.argmax(remaining <= cfg.time_stop_dte))
        policies[f"time_stop_{cfg.time_stop_dte}"] = settle(np.full(n_paths, stop_step),
                                                            np.ones(n_paths, bool))
    for x, first in first_hit.items():
        hit = first <= last
        exit_step = np.where(hit, first, last)
        closed = hit & (exit_step < last)
        policy = settle(exit_step, closed)
        gain = (x / 100.0) * max_profit * 100.0 * n - entry - close_fee
        policy["net_gain_when_hit"] = gain
        policy["below_min_gain"] = bool(gain < cfg.min_net_gain)
        policies[f"close_{x}"] = policy
        if stop_step is not None:
            exit_step = np.where(first <= stop_step, first, stop_step)
            p2 = settle(exit_step, np.ones(n_paths, bool))
            p2["net_gain_when_hit"] = gain
            p2["below_min_gain"] = policy["below_min_gain"]
            policies[f"close_{x}_or_{cfg.time_stop_dte}dte"] = p2
    out["policies"] = policies
    out["curves"] = curves
    out["curve_days"] = cal_elapsed
    return out


def headline_policy(policies: dict, dte_calendar: int, cfg: EngineConfig) -> str:
    """`auto`: hold to expiry at or under auto_hold_max_dte (fees make early
    closes uneconomic -- exit_rules.py), else close at 50% (or the nearest
    target)."""
    if cfg.headline_policy != "auto" and cfg.headline_policy in policies:
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
    results["G"] = evaluate(spec, g, targets, cfg)
    meta["G"] = {"label": f"GBM at the short leg's IV {vol:.0%}", "effective_n": cfg.n_paths,
                 "flag": ""}
    for name in ("H", "T"):
        ps: PathSet = getattr(paths, name)
        meta[name] = {"label": ps.label, "effective_n": ps.effective_n, "flag": ps.flag,
                      "n_starts": ps.n_starts}
        if ps.log_returns is not None:
            results[name] = evaluate(spec, ps.log_returns, targets, cfg)
    return {"models": results, "meta": meta, "blend": blend(results, cfg)}


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
        for key in ("ev", "p_profit", "days", "annualised", "ev_per_day_bpr"):
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
