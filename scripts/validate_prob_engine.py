"""
Walk-forward validation of the probability engine (Phase 13).

    python scripts/validate_prob_engine.py                  # SPY QQQ IWM AAPL KO, 30 DTE
    python scripts/validate_prob_engine.py --tickers SPY --dte 7 --years 5 --step 5
    python scripts/validate_prob_engine.py --strategy pcs --width-pct 0.02   # spreads

For entry dates every `--step` trading days over the last `--years`:

1. Sell a synthetic short put at the strike where Black-Scholes delta =
   `--delta` (default -0.25), priced at an IV PROXY = trailing 20-day RV x
   `backtest.vol_risk_premium_multiplier`. There is no historical option
   data (TastyTrade has no historical chains), so every price here is
   synthetic -- the same caveat as `wheel_backtest.py`.
2. PREDICT with models G, H, T using only bars up to and including the
   entry date: P(reach 25% / 50% of max profit by expiry), P(expire
   worthless), P(touch the strike). T conditions on trend state and RSI
   only -- the support map is built from the full history, so using it here
   would look ahead.
3. OBSERVE the actual path over the next `dte` trading-day equivalent,
   repricing the put daily at the SAME IV assumption, and record what
   happened.

Output: calibration by probability bin (predicted vs observed frequency),
Brier score per model and target, written to data/validation/ (spreads: prob_engine_pcs_*, where the extra
`max_loss` target scores P(max loss)):
    prob_engine_trades.parquet    one row per entry x model x target
    prob_engine_calibration.parquet
    prob_engine_summary.json

HONEST CAVEATS (repeated in the summary JSON)
* Synthetic option prices: IV = RV x multiplier. Real IV carries a variance
  risk premium and skew that this proxy only approximates; model G is
  scored against paths priced with the same proxy it simulates with.
* Overlapping entries (step < horizon) are not independent; the effective
  sample is about (entries x step / horizon).
* One realised history per ticker: calibration errors of a few points are
  within noise at these sample sizes -- the table reports n per bin.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics import prob_engine as pe  # noqa: E402
from analytics.strategies.base import Leg, Position  # noqa: E402
from core.paths import load_config, validation_dir  # noqa: E402

TARGETS = [25, 50, 100]
CAVEATS = [
    "Option prices are synthetic: IV proxy = trailing 20-day RV x the backtest VRP multiplier.",
    "Model G is scored against paths repriced with the same IV proxy it simulates at.",
    "Entries overlap when step < horizon; effective n ~ entries x step / horizon.",
    "T conditions on trend state and RSI only (the support map would look ahead).",
]


def strike_for_delta(spot: float, vol: float, days: float, rate: float, delta: float) -> float:
    """Put strike with Black-Scholes delta = `delta` (negative)."""
    from scipy.special import ndtri
    t = days / 365.0
    d1 = ndtri(1.0 + delta)                  # put delta = N(d1) - 1
    return float(spot * math.exp(-(d1 * vol * math.sqrt(t)) + (rate + 0.5 * vol * vol) * t))


def observe(closes: np.ndarray, position: Position, dte_cal: int, rate: float) -> dict:
    """What actually happened to the synthetic position along the real path:
    every leg repriced daily at its own entry IV (the same assumption the
    entry price used)."""
    steps = len(closes)
    cal = dte_cal * np.arange(1, steps + 1) / steps
    tau = np.maximum(dte_cal - cal, 0.0) / 365.0
    tau[-1] = 0.0
    value = np.zeros(steps)
    for leg in position.legs:
        price, _ = pe.bs_price(closes, leg.strike, tau, np.full(steps, leg.iv), rate,
                               leg.option_type)
        value += leg.sign * leg.qty * price
    pnl = position.credit + value
    max_profit = position.max_profit
    short = max(l.strike for l in position.legs if l.side == "short")
    out = {"obs_hit_100": float(pnl[-1] >= max_profit - 1e-9),
           "obs_touch": float((closes <= short).any())}
    for x in (25, 50):
        out[f"obs_hit_{x}"] = float((pnl >= x / 100.0 * max_profit).any())
    if len(position.legs) > 1:
        out["obs_max_loss"] = float(pnl[-1] <= -position.max_loss + 1e-9)
    return out


def run(tickers: list[str], dte: int, years: int, step: int, delta: float,
        n_paths: int, strategy: str = "csp", width_pct: float = 0.02) -> dict:
    from analytics import indicators
    from data_sources.yfinance_sync import load_daily

    cfg = pe.EngineConfig.from_config()
    cfg.n_paths = n_paths
    multiplier = float(load_config().get("backtest", {}).get("vol_risk_premium_multiplier", 1.15))
    steps = max(round(dte * 252 / 365), 1)
    rows = []
    started = time.perf_counter()
    for ticker in tickers:
        daily = load_daily(ticker, basis="price").sort_values("date").reset_index(drop=True)
        daily["date"] = pd.to_datetime(daily["date"])
        tech = indicators.compute(daily)
        closes = daily["close"].astype(float).to_numpy()
        rv = pd.Series(np.log(closes)).diff().rolling(20).std().to_numpy() * math.sqrt(252)
        first = daily.index[daily["date"] >= daily["date"].max() - pd.DateOffset(years=years)][0]
        entries = range(max(first, 252 * cfg.lookback_years // 2), len(daily) - steps - 1, step)
        rng = np.random.default_rng(cfg.seed)
        for i in entries:
            if not np.isfinite(rv[i]) or rv[i] <= 0:
                continue
            spot, vol = closes[i], rv[i] * multiplier
            strike = strike_for_delta(spot, vol, dte, cfg.rate, delta)
            credit = float(pe.bs_price(np.array(spot), strike, np.array(dte / 365.0),
                                       np.array(vol), cfg.rate, "put")[0])
            legs = [Leg("put", "short", strike, "x", iv=vol)]
            if strategy == "pcs":
                # Same IV for both legs: the proxy has no skew, so the spread's
                # credit is the flat-vol difference.
                long_k = strike - max(round(width_pct * spot), 1.0)
                long_px = float(pe.bs_price(np.array(spot), long_k, np.array(dte / 365.0),
                                            np.array(vol), cfg.rate, "put")[0])
                legs.append(Leg("put", "long", long_k, "x", iv=vol))
                position = Position("pcs", ticker, legs, credit - long_px)
                bpr = position.max_loss * 100
            else:
                position = Position("csp", ticker, legs, credit,
                                    collateral_per_contract=strike * 100)
                bpr = strike * 100
            if position.credit <= 0.01:
                continue
            spec = pe.TradeSpec(position, spot, dte, steps, 1, bpr)
            history = daily.iloc[: i + 1]
            paths = pe.ticker_paths(history, steps, cfg, rng, tech.iloc[: i + 1], None)
            result = pe.run_trade(spec, paths, TARGETS, cfg)
            observed = observe(closes[i + 1: i + 1 + steps], position, dte, cfg.rate)
            for model, r in list(result["models"].items()) + [("blend", result["blend"])]:
                for target in TARGETS:
                    rows.append({"ticker": ticker, "entry": daily["date"].iloc[i].date(),
                                 "model": model, "target": target,
                                 "predicted": r.get(f"p_hit_{target}"),
                                 "observed": observed[f"obs_hit_{target}"]})
                rows.append({"ticker": ticker, "entry": daily["date"].iloc[i].date(),
                             "model": model, "target": "touch",
                             "predicted": r.get("p_touch_short"),
                             "observed": observed["obs_touch"]})
                if "obs_max_loss" in observed:
                    rows.append({"ticker": ticker, "entry": daily["date"].iloc[i].date(),
                                 "model": model, "target": "max_loss",
                                 "predicted": r.get("p_max_loss"),
                                 "observed": observed["obs_max_loss"]})
    trades = pd.DataFrame(rows).dropna(subset=["predicted"])
    trades["target"] = trades["target"].astype(str)
    return summarise(trades, tickers, dte, years, step, delta, n_paths,
                     time.perf_counter() - started, strategy, width_pct)


def summarise(trades: pd.DataFrame, tickers, dte, years, step, delta, n_paths,
              seconds, strategy: str = "csp", width_pct: float = 0.02) -> dict:
    bins = np.linspace(0, 1, 11)
    trades["bin"] = pd.cut(trades["predicted"], bins, include_lowest=True)
    calibration = (trades.groupby(["model", "target", "bin"], observed=True)
                   .agg(n=("observed", "size"), predicted=("predicted", "mean"),
                        observed=("observed", "mean")).reset_index())
    calibration["bin"] = calibration["bin"].astype(str)
    scores = (trades.assign(sq=(trades["predicted"] - trades["observed"]) ** 2)
              .groupby(["model", "target"])
              .agg(n=("sq", "size"), brier=("sq", "mean"), mean_predicted=("predicted", "mean"),
                   mean_observed=("observed", "mean")).reset_index())
    scores["gap"] = scores["mean_predicted"] - scores["mean_observed"]
    out_dir = validation_dir()
    # CSP output keeps the Phase 13 file names; spreads get their own.
    tag = "prob_engine" if strategy == "csp" else f"prob_engine_{strategy}"
    trades.drop(columns="bin").to_parquet(out_dir / f"{tag}_trades.parquet", index=False)
    calibration.to_parquet(out_dir / f"{tag}_calibration.parquet", index=False)
    summary = {"run_at": dt.datetime.now().isoformat(timespec="seconds"),
               "strategy": strategy,
               "width_pct": width_pct if strategy == "pcs" else None,
               "tickers": tickers, "dte": dte, "years": years, "step": step, "delta": delta,
               "n_paths": n_paths, "seconds": round(seconds, 1),
               "entries": int(trades.drop_duplicates(["ticker", "entry"]).shape[0]),
               "effective_entries_approx": int(trades.drop_duplicates(["ticker", "entry"]).shape[0]
                                               * step / max(round(dte * 252 / 365), 1)),
               "scores": scores.to_dict("records"), "caveats": CAVEATS}
    (out_dir / f"{tag}_summary.json").write_text(json.dumps(summary, indent=2, default=str),
                                                      encoding="utf-8")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Walk-forward validation of the probability engine")
    ap.add_argument("--tickers", default="SPY,QQQ,IWM,AAPL,KO")
    ap.add_argument("--dte", type=int, default=30)
    ap.add_argument("--years", type=int, default=8)
    ap.add_argument("--step", type=int, default=10, help="trading days between entries")
    ap.add_argument("--delta", type=float, default=-0.25)
    ap.add_argument("--paths", type=int, default=4000)
    ap.add_argument("--strategy", choices=["csp", "pcs"], default="csp",
                    help="pcs: a put spread with the long leg --width-pct of spot below")
    ap.add_argument("--width-pct", type=float, default=0.02)
    args = ap.parse_args()
    summary = run([t.strip().upper() for t in args.tickers.split(",") if t.strip()],
                  args.dte, args.years, args.step, args.delta, args.paths,
                  args.strategy, args.width_pct)
    print(f"{summary['entries']} entries (~{summary['effective_entries_approx']} effective) "
          f"in {summary['seconds']}s")
    frame = pd.DataFrame(summary["scores"])
    print(frame.to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
