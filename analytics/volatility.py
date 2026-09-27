"""
Realized volatility estimators. Two families, per docs/PROJECT_SPEC.md:

- close-to-close: the classic estimator off daily closes alone.
- range-based (Parkinson, Garman-Klass): uses each day's O/H/L/C, which
  `daily_bars` already derives from the 1-minute data (see
  scripts/01_build_daily_summary.py), so this is already "tighter than
  daily closes alone" even before touching the 1-minute cache directly.
- intraday (true high-frequency realized variance -- sum of squared 1-minute
  log returns within the regular session, per day): the more direct reading
  of "using the 1-minute data for a tighter estimate," available once
  scripts/06_build_1m_cache.py has cached a ticker's 1-minute bars. This is
  the tightest of the three since it doesn't compress a whole session down
  to four price points before measuring variance.

All annualize with sqrt(252) (regular trading days/year), consistent with
the close-to-close estimator scripts/02_stage1_screen.py already uses.
"""
import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252
_LOG2 = np.log(2)


def close_to_close_series(daily: pd.DataFrame, window: int) -> pd.Series:
    """Rolling annualized close-to-close realized vol, indexed by date."""
    log_ret = np.log(daily["close"] / daily["close"].shift(1))
    rv = log_ret.rolling(window).std() * np.sqrt(TRADING_DAYS_PER_YEAR)
    return rv.rename(f"cc_rv_{window}d").set_axis(daily["date"])


def parkinson_series(daily: pd.DataFrame, window: int) -> pd.Series:
    """Rolling annualized Parkinson range-based estimator."""
    daily_var = np.log(daily["high"] / daily["low"]) ** 2 / (4 * _LOG2)
    rolling_var = daily_var.rolling(window).mean()
    rv = np.sqrt(rolling_var * TRADING_DAYS_PER_YEAR)
    return rv.rename(f"parkinson_rv_{window}d").set_axis(daily["date"])


def garman_klass_series(daily: pd.DataFrame, window: int) -> pd.Series:
    """Rolling annualized Garman-Klass range-based estimator."""
    log_hl = np.log(daily["high"] / daily["low"]) ** 2
    log_co = np.log(daily["close"] / daily["open"]) ** 2
    daily_var = 0.5 * log_hl - (2 * _LOG2 - 1) * log_co
    rolling_var = daily_var.rolling(window).mean().clip(lower=0)
    rv = np.sqrt(rolling_var * TRADING_DAYS_PER_YEAR)
    return rv.rename(f"gk_rv_{window}d").set_axis(daily["date"])


def intraday_rv_series(bars_1m: pd.DataFrame, window_days: int) -> pd.Series:
    """
    Rolling annualized realized vol from 1-minute regular-session bars: sum
    of squared consecutive log-returns within each trading day, then a
    rolling mean over `window_days` sessions, annualized. Requires
    bars_1m to already be regular_session-filtered (analytics.data_access
    .load_1m_bars defaults to that).
    """
    if bars_1m.empty:
        return pd.Series(dtype=float)
    df = bars_1m.copy()
    df["date"] = df["datetime"].dt.date
    df["log_ret"] = np.log(df["close"] / df.groupby("date")["close"].shift(1))
    daily_var = df.groupby("date")["log_ret"].apply(lambda r: (r.dropna() ** 2).sum())
    daily_var.index = pd.to_datetime(daily_var.index)
    rolling_var = daily_var.rolling(window_days).mean()
    rv = np.sqrt(rolling_var * TRADING_DAYS_PER_YEAR)
    return rv.rename(f"intraday_rv_{window_days}d")


def realized_vol_summary(daily: pd.DataFrame, windows: list[int],
                          bars_1m: pd.DataFrame | None = None) -> dict:
    """Latest (as-of-most-recent-bar) annualized RV for each estimator and
    window -- what the Scanner table and composite score read."""
    out = {"close_to_close": {}, "parkinson": {}, "garman_klass": {}}
    if bars_1m is not None and not bars_1m.empty:
        out["intraday"] = {}

    for w in windows:
        if len(daily) < w + 1:
            out["close_to_close"][w] = None
            out["parkinson"][w] = None
            out["garman_klass"][w] = None
            if "intraday" in out:
                out["intraday"][w] = None
            continue
        out["close_to_close"][w] = float(close_to_close_series(daily, w).iloc[-1])
        out["parkinson"][w] = float(parkinson_series(daily, w).iloc[-1])
        out["garman_klass"][w] = float(garman_klass_series(daily, w).iloc[-1])
        if bars_1m is not None and not bars_1m.empty:
            s = intraday_rv_series(bars_1m, w)
            out["intraday"][w] = float(s.iloc[-1]) if len(s) and pd.notna(s.iloc[-1]) else None

    return out
