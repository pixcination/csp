"""
Put-credit-spread rule backtest, with walk-forward (Phase 15, roadmap C.7).

WHAT IT SIMULATES
-----------------
One spread at a time per ticker, the way buying power actually works:

    enter at `dte` calendar days: short put at Black-Scholes delta
        `short_delta`, long put `width_pct` of spot below it (snapped to a
        listed-looking strike grid)
    every session after: reprice both legs and apply the management rules
        in the order `exit_rules.evaluate_put_spread` applies them --
        loss stop (loss >= k x credit), short strike breached (the close
        below it; live, this is "roll for a credit or close" -- the backtest
        closes), profit target (X% of the credit), time stop
        (`time_stop_dte` calendar days left, entries above it)
    otherwise settle at expiry on intrinsic value
    re-enter the next session

Every leg is charged the tastytrade schedule (`costs.legs_open`,
`legs_close`, `vertical_exit_fees` at expiry) and every open/close crosses
a modelled bid/ask: fill = mid -/+ `slippage_fraction` x the summed
half-spreads, the same rule as `costs.package_fill`.

HONEST LIMITS (repeated in the summary)
* **Option prices are synthetic.** There is no historical chain data
  (TastyTrade has none; roadmap B.8). IV = trailing 20-day RV x
  `vol_risk_premium`, re-read every day (sticky moneyness), with a linear
  put skew: IV x (1 + `skew_per_sd` x standard deviations OTM). Both
  multipliers are assumptions, and a spread's credit is a DIFFERENCE of
  two such prices -- far more sensitive to the skew assumption than a
  single put. Treat the level of returns as indicative; the comparison
  between rule sets on the same assumptions is the useful output.
* Bid/ask is modelled as a fraction of each leg's price with a floor, not
  observed.
* Rules fire on daily closes. Intraday touches of a target or a stop are
  not seen, so both fire later than they would live.
* No rolls (the loss stop closes instead) and no early assignment.
* Daily bars are on the PRICE basis (split-adjusted, not dividend-adjusted):
  strikes settle on traded prices, so the ex-dividend drops stay in.

WALK-FORWARD
------------
Each parameter set is simulated once over the full history; a fold then
scores the trades ENTERED inside its train window, picks the best set
there, and reports that set's trades entered inside the following test
window, beside a fixed baseline. Degradation and the baseline comparison
read exactly as in `walkforward.py`: if re-optimising cannot beat a fixed
sensible rule out of sample, the grid is measuring noise.
"""
from __future__ import annotations

import datetime as dt
import itertools
import math
from dataclasses import asdict, dataclass, replace

import numpy as np
import pandas as pd
from scipy.special import ndtri

from analytics import costs
from analytics.prob_engine import bs_price

TRADING_DAYS = 252.0
METRIC = "annualised_on_bpr"
CAVEATS = [
    "Option prices are synthetic: IV = trailing 20-day RV x the VRP multiplier, with a "
    "linear put skew. A spread's credit is the difference of two such prices.",
    "Bid/ask is modelled (a fraction of each leg's price with a floor), not observed.",
    "Rules fire on daily closes; intraday touches of targets and stops are not seen.",
    "No rolls and no early assignment; one spread at a time per ticker.",
]


@dataclass(frozen=True)
class PCSParams:
    short_delta: float = -0.20
    width_pct: float = 0.04                 # of spot at entry
    # Defaults = the shipped management.spread rules (the walk-forward baseline).
    dte: int = 45                           # calendar days at entry
    profit_target_pct: float | None = 50    # None = hold to expiry
    loss_stop_multiple: float | None = 2.0  # None = no stop
    time_stop_dte: int | None = 21          # None = no time stop
    close_on_breach: bool = False           # close when the close is below the short strike
    rv_window: int = 20
    vol_risk_premium: float = 1.15
    skew_per_sd: float = 0.05
    rate: float = 0.045
    contracts: int = 1
    slippage_fraction: float = 0.40
    leg_half_spread_pct: float = 0.03
    min_half_spread: float = 0.01
    min_credit: float = 0.10
    cash_settled: bool = False

    def label(self) -> str:
        target = f"{self.profit_target_pct:g}%" if self.profit_target_pct else "hold"
        stop = f"{self.loss_stop_multiple:g}x" if self.loss_stop_multiple else "no stop"
        ts = f"{self.time_stop_dte}d" if self.time_stop_dte else "no ts"
        breach = ", breach" if self.close_on_breach else ""
        return (f"{abs(self.short_delta):.2f}d, {self.width_pct:.1%} wide, {self.dte} DTE, "
                f"{target}, {stop}{breach}, {ts}")


def strike_increment(spot: float) -> float:
    if spot < 25:
        return 0.5
    if spot < 1000:
        return 1.0
    return 5.0


def snap(value: float, increment: float) -> float:
    return round(value / increment) * increment


def strike_for_delta(spot: float, delta: float, days: float, vol: float, rate: float) -> float:
    """Put strike with Black-Scholes delta `delta` (negative), calendar days."""
    t = days / 365.0
    d1 = ndtri(1.0 + delta)
    return float(spot * math.exp(-(d1 * vol * math.sqrt(t)) + (rate + 0.5 * vol * vol) * t))


def skewed_vol(atm: np.ndarray, spot: np.ndarray, strike: float, tau: np.ndarray,
               skew_per_sd: float) -> np.ndarray:
    """IV for a put at `strike`: ATM x (1 + skew x SDs out of the money)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        sd = np.log(spot / strike) / (atm * np.sqrt(np.maximum(tau, 1e-6)))
    sd = np.clip(np.nan_to_num(sd), -3.0, 3.0)
    return atm * np.maximum(1.0 + skew_per_sd * sd, 0.5)


def _prepare(daily: pd.DataFrame, rv_window: int) -> pd.DataFrame:
    frame = daily.copy()
    frame.columns = [str(c).lower() for c in frame.columns]
    if "date" not in frame.columns:
        frame = frame.reset_index()
        frame.columns = [str(c).lower() for c in frame.columns]
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    log_ret = np.log(frame["close"].astype(float)).diff()
    frame["rv"] = log_ret.rolling(rv_window).std() * math.sqrt(TRADING_DAYS)
    return frame


def simulate(daily: pd.DataFrame, ticker: str, params: PCSParams | None = None,
             prepared: pd.DataFrame | None = None) -> pd.DataFrame:
    """Every spread the rules would have traded, one row per trade."""
    p = params or PCSParams()
    frame = prepared if prepared is not None else _prepare(daily, p.rv_window)
    close = frame["close"].to_numpy(dtype=float)
    rv = frame["rv"].to_numpy(dtype=float)
    dates = frame["date"].dt.date.to_numpy()
    n_days = len(close)
    steps = max(int(round(p.dte * TRADING_DAYS / 365.0)), 1)
    n = max(int(p.contracts), 1)
    entry_fee = costs.legs_open([("sell", n), ("buy", n)]).total
    close_fee = costs.legs_close([("buy", n), ("sell", n)]).total
    expiry_fees = costs.vertical_exit_fees(n, p.cash_settled)
    cal = p.dte * np.arange(0, steps + 1) / steps              # calendar days elapsed
    tau = np.maximum(p.dte - cal, 0.0) / 365.0
    tau[-1] = 0.0
    days_left = p.dte - cal
    time_stop_live = p.time_stop_dte is not None and p.dte > p.time_stop_dte

    rows = []
    i = p.rv_window + 1
    while i < n_days - steps:
        vol = rv[i]
        if not np.isfinite(vol) or vol <= 0:
            i += 1
            continue
        spot = close[i]
        atm = vol * p.vol_risk_premium
        inc = strike_increment(spot)
        short = snap(strike_for_delta(spot, p.short_delta, p.dte, atm, p.rate), inc)
        width = max(snap(p.width_pct * spot, inc), inc)
        long = short - width
        if long <= 0:
            i += 1
            continue

        path = close[i: i + steps + 1]
        vols = rv[i: i + steps + 1] * p.vol_risk_premium
        vols = np.where(np.isfinite(vols) & (vols > 0), vols, atm)
        vs = skewed_vol(vols, path, short, tau, p.skew_per_sd)
        vl = skewed_vol(vols, path, long, tau, p.skew_per_sd)
        ps, _ = bs_price(path, short, tau, vs, p.rate, "put")
        pl, _ = bs_price(path, long, tau, vl, p.rate, "put")
        half = (np.maximum(p.leg_half_spread_pct * ps, p.min_half_spread)
                + np.maximum(p.leg_half_spread_pct * pl, p.min_half_spread))
        mid = ps - pl
        credit = float(mid[0] - p.slippage_fraction * half[0])
        if credit < p.min_credit or credit >= width:
            i += 1
            continue

        # Rules on each later close, in exit_rules order.
        live = np.arange(1, steps)                       # before expiry
        exit_step, reason = steps, "expiry"
        m = mid[live]
        stop_hit = (m - credit >= p.loss_stop_multiple * credit) if p.loss_stop_multiple \
            else np.zeros(len(live), bool)
        target_hit = ((credit - m) / credit >= p.profit_target_pct / 100.0) \
            if p.profit_target_pct else np.zeros(len(live), bool)
        time_hit = (days_left[live] <= p.time_stop_dte) if time_stop_live \
            else np.zeros(len(live), bool)
        breach_hit = (path[live] < short) if p.close_on_breach             else np.zeros(len(live), bool)
        for name, hits in (("loss_stop", stop_hit), ("breach", breach_hit),
                           ("target", target_hit), ("time_stop", time_hit)):
            if hits.any():
                step = int(live[np.argmax(hits)])
                if step < exit_step or (step == exit_step and reason == "expiry"):
                    exit_step, reason = step, name

        if exit_step < steps:
            debit = float(mid[exit_step] + p.slippage_fraction * half[exit_step])
            fees = entry_fee + close_fee
        else:
            s_t = path[-1]
            debit = float(max(short - s_t, 0.0) - max(long - s_t, 0.0))
            itm = int(s_t < short) + int(s_t < long)
            fees = entry_fee + (expiry_fees["max_loss"] if itm == 2 else
                                expiry_fees["short_itm"] if itm == 1 else 0.0)
            if itm == 2:
                reason = "max_loss"
        pnl = (credit - debit) * 100.0 * n - fees
        bpr = (width - credit) * 100.0 * n
        held_cal = float(cal[exit_step])
        rows.append({
            "ticker": ticker, "entry_date": dates[i], "exit_date": dates[i + exit_step],
            "spot": spot, "short_strike": short, "long_strike": long, "width": width,
            "iv_entry": float(vs[0]), "credit": credit, "credit_width": credit / width,
            "exit_debit": debit, "exit_reason": reason, "pnl": pnl, "fees": fees,
            "bpr": bpr, "days_held": max(held_cal, 1.0),
            "return_on_bpr": pnl / bpr if bpr else float("nan"),
            "max_profit_pct": float(np.max((credit - mid[1:exit_step + 1]) / credit))
            if exit_step >= 1 else float("nan"),
        })
        i += exit_step + 1
    return pd.DataFrame(rows)


def summarise(trades: pd.DataFrame) -> dict:
    if trades is None or trades.empty:
        return {"n_trades": 0}
    bpr_days = float((trades["bpr"] * trades["days_held"]).sum())
    cumulative = trades["pnl"].cumsum()
    reasons = trades["exit_reason"].value_counts(normalize=True)
    return {
        "n_trades": int(len(trades)),
        "win_rate": float((trades["pnl"] > 0).mean()),
        "mean_pnl": float(trades["pnl"].mean()),
        "total_pnl": float(trades["pnl"].sum()),
        METRIC: float(trades["pnl"].sum() / bpr_days * 365.0) if bpr_days else float("nan"),
        "mean_days_held": float(trades["days_held"].mean()),
        "mean_credit_width": float(trades["credit_width"].mean()),
        "pct_target": float(reasons.get("target", 0.0)),
        "pct_loss_stop": float(reasons.get("loss_stop", 0.0)),
        "pct_time_stop": float(reasons.get("time_stop", 0.0)),
        "pct_breach": float(reasons.get("breach", 0.0)),
        "pct_expiry": float(reasons.get("expiry", 0.0)),
        "pct_max_loss": float(reasons.get("max_loss", 0.0)),
        "worst_trade": float(trades["pnl"].min()),
        "worst_drawdown": float((cumulative - cumulative.cummax()).min()),
        "total_fees": float(trades["fees"].sum()),
    }


def run(daily: pd.DataFrame, ticker: str, params: PCSParams | None = None) -> dict:
    params = params or PCSParams()
    trades = simulate(daily, ticker, params)
    return {"ticker": ticker, "params": asdict(params), "label": params.label(),
            "summary": summarise(trades), "trades": trades}


# --- Grid ---------------------------------------------------------------------------------

DEFAULT_GRID = {
    "dte": (30, 45),
    "short_delta": (-0.15, -0.20, -0.25, -0.30),
    "width_pct": (0.01, 0.02, 0.04),
    "profit_target_pct": (25, 50, 75, None),
    "loss_stop_multiple": (2.0, None),
    "close_on_breach": (False, True),
    "time_stop_dte": (21, None),
}


def grid(base: PCSParams | None = None, **axes) -> list[PCSParams]:
    base = base or PCSParams()
    spec = {**DEFAULT_GRID, **{k: v for k, v in axes.items() if v is not None}}
    keys = list(spec)
    return [replace(base, **dict(zip(keys, values)))
            for values in itertools.product(*(spec[k] for k in keys))]


def sweep(daily: pd.DataFrame, ticker: str, params_list: list[PCSParams],
          reporter=None) -> tuple[pd.DataFrame, dict[int, pd.DataFrame]]:
    """Simulate every parameter set once. Returns the summary table (one row
    per set, `set_id` indexes `params_list`) and the trades per set."""
    prepared = _prepare(daily, params_list[0].rv_window)
    rows, trades = [], {}
    for set_id, params in enumerate(params_list):
        table = simulate(daily, ticker, params, prepared=prepared)
        trades[set_id] = table
        rows.append({"ticker": ticker, "set_id": set_id, "label": params.label(),
                     **{k: getattr(params, k) for k in DEFAULT_GRID},
                     **summarise(table)})
        if reporter:
            reporter.advance(1, note=f"{ticker} {params.label()}")
    frame = pd.DataFrame(rows)
    if not frame.empty and METRIC in frame:
        frame = frame.sort_values(METRIC, ascending=False)
    return frame.reset_index(drop=True), trades


def _score(table: pd.DataFrame, start, end) -> tuple[float, int]:
    if table is None or table.empty:
        return float("nan"), 0
    entry = pd.to_datetime(table["entry_date"])
    window = table[(entry >= start) & (entry < end)]
    if window.empty:
        return float("nan"), 0
    return float(summarise(window)[METRIC]), int(len(window))


def walk_forward(daily: pd.DataFrame, ticker: str, params_list: list[PCSParams],
                 baseline: PCSParams | None = None, train_years: float = 5.0,
                 test_years: float = 1.0, step_years: float = 1.0,
                 min_train_trades: int = 20, min_test_trades: int = 5,
                 trades: dict[int, pd.DataFrame] | None = None) -> dict:
    """Roll a train/test window through history, re-choosing the rule set on
    each train window and scoring it on the following test window."""
    baseline = baseline or PCSParams()
    if trades is None:
        _, trades = sweep(daily, ticker, params_list)
    base_trades = simulate(daily, ticker, baseline)
    dates = pd.to_datetime(daily["date"] if "date" in daily else daily.index)
    first, last = dates.min(), dates.max()
    train_span = pd.DateOffset(months=int(train_years * 12))
    test_span = pd.DateOffset(months=int(test_years * 12))
    step = pd.DateOffset(months=int(step_years * 12))

    folds = []
    train_start, index = first, 0
    while train_start + train_span + test_span <= last:
        train_end = train_start + train_span
        test_end = train_end + test_span
        best, best_score = None, -np.inf
        for set_id, table in trades.items():
            score, count = _score(table, train_start, train_end)
            if count >= min_train_trades and np.isfinite(score) and score > best_score:
                best, best_score = set_id, score
        if best is not None:
            oos, count = _score(trades[best], train_end, test_end)
            base_oos, _ = _score(base_trades, train_end, test_end)
            if count >= min_test_trades and np.isfinite(oos):
                chosen = params_list[best]
                folds.append({
                    "ticker": ticker, "fold": index,
                    "train_start": str(train_start.date()), "train_end": str(train_end.date()),
                    "test_end": str(test_end.date()), "set_id": best,
                    "chosen": chosen.label(),
                    **{f"chosen_{k}": getattr(chosen, k) for k in DEFAULT_GRID},
                    "in_sample": best_score, "out_of_sample": oos,
                    "baseline_out_of_sample": base_oos, "test_trades": count})
        train_start += step
        index += 1
    return _wf_summary(ticker, pd.DataFrame(folds), baseline)


def _wf_summary(ticker: str, folds: pd.DataFrame, baseline: PCSParams) -> dict:
    if folds.empty:
        return {"ticker": ticker, "n_folds": 0, "folds": folds,
                "verdict": "not enough history for a walk-forward test"}
    mean_is = float(folds["in_sample"].mean())
    mean_oos = float(folds["out_of_sample"].mean())
    mean_base = float(folds["baseline_out_of_sample"].mean())
    ratio = (mean_is - mean_oos) / abs(mean_is) if mean_is else float("nan")
    beat = float((folds["out_of_sample"] > folds["baseline_out_of_sample"]).mean())
    stability = float(folds["set_id"].value_counts().max() / len(folds))
    return {"ticker": ticker, "n_folds": int(len(folds)), "mean_in_sample": mean_is,
            "mean_out_of_sample": mean_oos, "mean_baseline_oos": mean_base,
            "degradation_ratio": ratio, "beat_baseline_rate": beat,
            "parameter_stability": stability, "baseline": baseline.label(),
            "verdict": verdict(ratio, beat, mean_oos, mean_base), "folds": folds}


def verdict(ratio: float, beat: float, mean_oos: float, mean_base: float) -> str:
    if not np.isfinite(ratio):
        head = "Inconclusive."
    elif ratio > 0.60:
        head = "SEVERE degradation: the grid is selecting noise."
    elif ratio > 0.33:
        head = "Substantial degradation: expect the out-of-sample number, not the in-sample one."
    elif ratio > 0.15:
        head = "Mild degradation, within what a real edge shows."
    else:
        head = "Holds up out of sample."
    if beat < 0.5 or mean_oos <= mean_base:
        tail = (f" Re-choosing the rules beat the fixed baseline in {beat:.0%} of folds "
                f"({mean_oos:.1%} vs {mean_base:.1%}): keep one sensible rule set.")
    else:
        tail = (f" Re-choosing beat the baseline in {beat:.0%} of folds, by "
                f"{mean_oos - mean_base:.1%} a year on average.")
    return head + tail


def across_universe(tickers: list[str], params_list: list[PCSParams] | None = None,
                    baseline: PCSParams | None = None, years: int = 20,
                    loader=None, reporter=None) -> dict:
    """Sweep and walk-forward every ticker; pool the verdict."""
    from core.progress import NullReporter
    if loader is None:
        from data_sources.yfinance_sync import load_daily as loader
    params_list = params_list or grid()
    baseline = baseline or PCSParams()
    reporter = reporter or NullReporter()
    sweeps, fold_frames, rows = [], [], []
    with reporter.stage("pcs_backtest", "PCS backtest", total=len(tickers)):
        for ticker in tickers:
            try:
                daily = loader(ticker, basis="price")
                if daily is None or daily.empty:
                    reporter.advance(1, note=f"{ticker}: no bars")
                    continue
                daily = daily[pd.to_datetime(daily["date"])
                              >= pd.to_datetime(daily["date"]).max() - pd.DateOffset(years=years)]
                table, trades = sweep(daily, ticker, params_list)
                sweeps.append(table)
                result = walk_forward(daily, ticker, params_list, baseline, trades=trades)
                folds = result.pop("folds")
                if not folds.empty:
                    fold_frames.append(folds)
                rows.append(result)
                reporter.advance(1, note=f"{ticker}: {result['n_folds']} folds")
            except Exception as exc:
                reporter.advance(1, note=f"{ticker}: {type(exc).__name__}")
                reporter.log(f"{ticker}: {exc}")
    sweep_frame = pd.concat(sweeps, ignore_index=True) if sweeps else pd.DataFrame()
    folds = pd.concat(fold_frames, ignore_index=True) if fold_frames else pd.DataFrame()
    per_ticker = pd.DataFrame(rows)
    pooled: dict = {"tickers": int(len(per_ticker)), "caveats": CAVEATS,
                    "baseline": baseline.label(), "sets": len(params_list),
                    "run_at": dt.datetime.now().isoformat(timespec="seconds")}
    if not folds.empty:
        mean_is = float(folds["in_sample"].mean())
        mean_oos = float(folds["out_of_sample"].mean())
        mean_base = float(folds["baseline_out_of_sample"].mean())
        ratio = (mean_is - mean_oos) / abs(mean_is) if mean_is else float("nan")
        beat = float((folds["out_of_sample"] > folds["baseline_out_of_sample"]).mean())
        pooled.update({"folds": int(len(folds)), "mean_in_sample": mean_is,
                       "mean_out_of_sample": mean_oos, "mean_baseline_oos": mean_base,
                       "degradation_ratio": ratio, "beat_baseline_rate": beat,
                       "verdict": verdict(ratio, beat, mean_oos, mean_base)})
    if not sweep_frame.empty:
        # The SHAPE across the grid, pooled over tickers: mean annualised per
        # value of each axis. This is what a sweep is good for.
        shape = {}
        for axis in DEFAULT_GRID:
            col = sweep_frame[axis].astype(object).where(sweep_frame[axis].notna(), "none")
            shape[axis] = (sweep_frame.assign(_axis=col.astype(str))
                           .groupby("_axis")[[METRIC, "win_rate", "pct_max_loss"]]
                           .mean().round(4).reset_index().rename(columns={"_axis": axis})
                           .to_dict("records"))
        pooled["shape"] = shape
        top = (sweep_frame.groupby("label")[[METRIC, "win_rate", "worst_trade"]].mean()
               .sort_values(METRIC, ascending=False).head(10).reset_index())
        pooled["top_sets_in_sample"] = top.round(4).to_dict("records")
    return {"pooled": pooled, "per_ticker": per_ticker, "sweep": sweep_frame, "folds": folds}
