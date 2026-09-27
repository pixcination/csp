"""
Technical context: moving averages, RSI, 52-week range, drawdown. Per
docs/PROJECT_SPEC.md, these are a *filter/sanity check* against selling puts into
a clear downtrend, not a primary ranking signal -- technical_health_flag()
below returns a small penalty/bonus term for the composite score, and the
underlying series feed the Ticker Detail price chart's moving-average
overlay.
"""
import numpy as np
import pandas as pd


def sma_series(daily: pd.DataFrame, window: int) -> pd.Series:
    return daily["close"].rolling(window).mean().rename(f"sma_{window}").set_axis(daily["date"])


def rsi_series(daily: pd.DataFrame, window: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = daily["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.rename(f"rsi_{window}").set_axis(daily["date"])


def distance_from_52wk_range(daily: pd.DataFrame) -> dict:
    """Latest close's distance from the trailing-252-day high/low, as a
    fraction (0.10 = 10% below the 52-week high)."""
    if len(daily) < 2:
        return {"pct_below_52wk_high": None, "pct_above_52wk_low": None}
    window = daily.tail(252)
    last = window["close"].iloc[-1]
    hi, lo = window["high"].max(), window["low"].min()
    return {
        "pct_below_52wk_high": float((hi - last) / hi) if hi else None,
        "pct_above_52wk_low": float((last - lo) / lo) if lo else None,
    }


def current_drawdown(daily: pd.DataFrame) -> float | None:
    """Latest close vs. its running peak -- negative fraction, 0 = at peak."""
    if daily.empty:
        return None
    running_max = daily["close"].cummax()
    dd = daily["close"] / running_max - 1.0
    return float(dd.iloc[-1])


def technical_health_flag(daily: pd.DataFrame, cfg: dict) -> dict:
    """
    Sanity-check flag, not a ranking signal: red-flags a name whose price is
    below its long SMA *and* whose RSI shows real downside momentum (not
    just "below average" -- both conditions per docs/PROJECT_SPEC.md's "clear
    downtrend" framing). Returns a dict with the boolean flag plus a
    [0, 1] health score (1 = healthy) for the composite score's small
    technical-health weight.
    """
    tech_cfg = cfg.get("analytics", {}).get("technical_health", {})
    sma_window = tech_cfg.get("downtrend_price_below_sma", 200)
    rsi_floor = tech_cfg.get("downtrend_rsi_below", 30)

    if len(daily) < sma_window:
        return {"downtrend_flag": None, "health_score": None, "reason": "insufficient_history"}

    sma = sma_series(daily, sma_window).iloc[-1]
    rsi = rsi_series(daily, cfg.get("analytics", {}).get("rsi_window_days", 14)).iloc[-1]
    last_close = daily["close"].iloc[-1]

    below_sma = pd.notna(sma) and last_close < sma
    weak_rsi = pd.notna(rsi) and rsi < rsi_floor
    downtrend = bool(below_sma and weak_rsi)

    # Health score: 1.0 healthy -> 0.0 both conditions met, with partial
    # credit for being below SMA (a mild caution) without weak RSI.
    if downtrend:
        health = 0.0
    elif below_sma:
        health = 0.5
    else:
        health = 1.0

    return {
        "downtrend_flag": downtrend,
        "health_score": health,
        "last_close": float(last_close),
        f"sma_{sma_window}": float(sma) if pd.notna(sma) else None,
        "rsi": float(rsi) if pd.notna(rsi) else None,
    }
