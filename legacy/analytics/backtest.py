"""
Backtest harness -- replays history for a given delta/DTE selection rule
and measures actual win rate (expired OTM), assignment frequency, average
realized return, and worst drawdown. High priority per docs/PROJECT_SPEC.md:
corrects for Black-Scholes' tendency to overstate real-world win rates for
short premium strategies.

Methodology (flagged as a real assumption in the build plan, config.yaml's
`backtest` section, and README): real historical option chains aren't
available (TastyTrade has no historical chain API), so this simulates
historical option pricing rather than replaying real quotes --

  1. On each entry date (weekly, entry_weekday from config -- Friday by
     default), solve for the strike that would have priced at the
     configured target delta under Black-Scholes, using trailing realized
     vol (rv_window_days) scaled by vol_risk_premium_multiplier as the IV
     proxy -- both config.yaml `backtest` knobs, not hardcoded.
  2. Price the simulated entry premium at that strike via Black-Scholes.
  3. Walk forward to the actual historical close nearest (at or before)
     the target expiration date and check assignment
     (assignment_rule: close_below_strike_at_expiration).

No lookahead bias: the vol proxy at each entry date only uses realized-vol
data up to and including that date (rolling window looks backward by
construction).
"""
import numpy as np
import pandas as pd
from scipy.stats import norm

from analytics.config import load_config
from legacy.analytics.data_access import load_daily_bars
from analytics.volatility import close_to_close_series
from analytics.options_math import bs_price_greeks


def solve_strike_for_delta(spot: float, target_delta: float, dte_days: float,
                            vol: float, rate: float) -> float:
    """Inverts the Black-Scholes put-delta formula (delta = N(d1) - 1) to
    find the strike that prices at `target_delta` -- avoids an iterative
    solve since put delta is monotonic in strike."""
    T = dte_days / 365.0
    d1 = norm.ppf(target_delta + 1.0)
    ln_s_over_k = d1 * vol * np.sqrt(T) - (rate + 0.5 * vol ** 2) * T
    return spot / np.exp(ln_s_over_k)


def run_backtest(ticker: str, cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    bt_cfg = cfg["backtest"]
    thr = cfg["stage3_thresholds"]
    rate = cfg["analytics"]["risk_free_rate"]

    target_delta = thr["target_delta"]
    target_dte = bt_cfg.get("target_dte", 10)
    rv_window = bt_cfg["rv_window_days"]
    vrp = bt_cfg["vol_risk_premium_multiplier"]
    entry_weekday = bt_cfg["entry_weekday"]

    daily = load_daily_bars(ticker)
    if len(daily) < rv_window + target_dte + 5:
        return {"trades": pd.DataFrame(), "summary": {"error": "insufficient price history"}}

    daily = daily.reset_index(drop=True)
    rv_series = close_to_close_series(daily, rv_window)  # backward-looking only, no lookahead
    dates = daily["date"]

    trades = []
    for i in range(rv_window, len(daily)):
        entry_date = dates.iloc[i]
        if entry_date.weekday() != entry_weekday:
            continue

        vol_proxy = rv_series.iloc[i]
        if pd.isna(vol_proxy) or vol_proxy <= 0:
            continue
        vol_proxy *= vrp

        spot = float(daily["close"].iloc[i])
        strike = solve_strike_for_delta(spot, target_delta, target_dte, vol_proxy, rate)
        premium = bs_price_greeks(spot, strike, target_dte, vol_proxy, rate, "put").price
        if premium <= 0:
            continue

        target_exp_date = entry_date + pd.Timedelta(days=target_dte)
        future = daily[(dates > entry_date) & (dates <= target_exp_date)]
        if future.empty or future["date"].iloc[-1] < target_exp_date - pd.Timedelta(days=3):
            continue  # not enough forward data to know the outcome (recent tail of history)

        exit_close = float(future["close"].iloc[-1])
        assigned = exit_close < strike
        pnl_per_share = premium - max(strike - exit_close, 0.0)
        realized_return = pnl_per_share / strike

        trades.append({
            "entry_date": entry_date, "spot": spot, "strike": strike,
            "vol_proxy": vol_proxy, "premium": premium, "exit_date": future["date"].iloc[-1],
            "exit_close": exit_close, "assigned": assigned,
            "pnl_dollars": pnl_per_share * 100, "realized_return": realized_return,
        })

    trades_df = pd.DataFrame(trades)
    if trades_df.empty:
        return {"trades": trades_df, "summary": {"error": "no trades generated (insufficient history for this DTE/cadence)"}}

    cum_pnl = trades_df["pnl_dollars"].cumsum()
    running_max = cum_pnl.cummax()
    drawdown = cum_pnl - running_max

    summary = {
        "n_trades": len(trades_df),
        "win_rate_expired_otm": float((~trades_df["assigned"]).mean()),
        "assignment_frequency": float(trades_df["assigned"].mean()),
        "avg_realized_return": float(trades_df["realized_return"].mean()),
        "avg_realized_return_annualized": float(trades_df["realized_return"].mean() * (365.0 / target_dte)),
        "worst_drawdown_dollars": float(drawdown.min()),
        "total_pnl_dollars": float(cum_pnl.iloc[-1]),
        "target_delta": target_delta, "target_dte": target_dte,
        "vol_risk_premium_multiplier": vrp,
    }
    return {"trades": trades_df, "summary": summary}
