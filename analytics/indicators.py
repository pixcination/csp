"""
Technical indicators, daily and weekly, on the price basis (Phase 10).

    SMA / EMA   9 21 50 100 200          (config indicators.ma_lengths)
    RSI-14      Wilder
    ATR-14      Wilder true range
    Bollinger   20, 2 sigma
    MACD        12 / 26 / 9
    ADX-14      with +DI / -DI (Wilder)
    52-week     high, low, position in range   (52 weekly bars on the weekly frame)
    volume      today / 50-session average

Everything is vectorised pandas and runs on `load_daily(basis="price")` --
the traded, split-adjusted series. Levels and strikes live in traded prices;
a dividend-adjusted moving average sits below the level the market actually
watched by the cumulative dividends since, which is the Phase 8 lesson.

WEEKLY ON A DAILY TIMELINE
--------------------------
`with_weekly(daily)` computes the same set on weekly bars and attaches each
weekly column (prefix `w_`) to the daily frame through
`bars.weekly_on_daily`, i.e. the value of the last COMPLETED week. A Wednesday
never sees the Friday close of its own week. Tested in test_phase10.

Wilder smoothing is `ewm(alpha=1/n, adjust=False)`, matching the platforms
the levels are compared against. Values before a full window are NaN, never
a partial-window estimate presented as real.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.paths import load_config


def _cfg() -> dict:
    return load_config().get("indicators", {}) or {}


def wilder(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(alpha=1.0 / n, min_periods=n, adjust=False).mean()


def sma(close: pd.Series, n: int) -> pd.Series:
    return close.rolling(n, min_periods=n).mean()


def ema(close: pd.Series, n: int) -> pd.Series:
    return close.ewm(span=n, min_periods=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    change = close.diff()
    gain, loss = change.clip(lower=0), -change.clip(upper=0)
    avg_gain, avg_loss = wilder(gain, n), wilder(loss, n)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - 100 / (1 + rs)
    # No losses in the window: RSI is 100, not undefined.
    return out.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)


def true_range(frame: pd.DataFrame) -> pd.Series:
    prev = frame["close"].shift(1)
    return pd.concat([frame["high"] - frame["low"], (frame["high"] - prev).abs(),
                      (frame["low"] - prev).abs()], axis=1).max(axis=1)


def atr(frame: pd.DataFrame, n: int = 14) -> pd.Series:
    return wilder(true_range(frame), n)


def adx(frame: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    up = frame["high"].diff()
    down = -frame["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = wilder(true_range(frame), n)
    plus_di = 100 * wilder(plus_dm, n) / tr
    minus_di = 100 * wilder(minus_dm, n) / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return pd.DataFrame({"adx": wilder(dx, n), "plus_di": plus_di, "minus_di": minus_di})


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, min_periods=signal, adjust=False).mean()
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def compute(frame: pd.DataFrame, periods_per_year: int = 252) -> pd.DataFrame:
    """All indicators for one OHLCV frame (daily, or weekly with
    periods_per_year=52). Returns the frame with indicator columns added."""
    cfg = _cfg()
    out = frame.copy().reset_index(drop=True)
    close = out["close"].astype(float)
    for n in cfg.get("ma_lengths", [9, 21, 50, 100, 200]):
        out[f"sma_{n}"] = sma(close, n)
        out[f"ema_{n}"] = ema(close, n)
    out[f"rsi_{cfg.get('rsi', 14)}"] = rsi(close, cfg.get("rsi", 14))
    out["rsi"] = out[f"rsi_{cfg.get('rsi', 14)}"]
    out["atr"] = atr(out, cfg.get("atr", 14))
    out["atr_pct"] = out["atr"] / close
    length, width = cfg.get("bollinger", [20, 2.0])
    mid, sd = sma(close, length), close.rolling(length, min_periods=length).std()
    out["bb_mid"], out["bb_upper"], out["bb_lower"] = mid, mid + width * sd, mid - width * sd
    out["bb_pct_b"] = (close - out["bb_lower"]) / (out["bb_upper"] - out["bb_lower"])
    fast, slow, signal = cfg.get("macd", [12, 26, 9])
    out = pd.concat([out, macd(close, fast, slow, signal)], axis=1)
    out = pd.concat([out, adx(out, cfg.get("adx", 14))], axis=1)
    year = periods_per_year
    out["high_52w"] = out["high"].rolling(year, min_periods=year).max()
    out["low_52w"] = out["low"].rolling(year, min_periods=year).min()
    rng = (out["high_52w"] - out["low_52w"]).replace(0.0, np.nan)
    out["range_position_52w"] = (close - out["low_52w"]) / rng
    out["pct_from_52w_high"] = close / out["high_52w"] - 1.0
    if "volume" in out:
        window = cfg.get("volume_ratio_window", 50)
        if periods_per_year == 52:
            window = max(window // 5, 4)
        avg = out["volume"].astype(float).rolling(window, min_periods=window).mean()
        out["volume_ratio"] = out["volume"].astype(float) / avg.replace(0.0, np.nan)
    return out


INDICATOR_COLUMNS_EXCLUDED = {"date", "open", "high", "low", "close", "volume",
                              "week_end", "last_session", "scheduled_last_session",
                              "sessions", "complete"}


def with_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    """Daily indicators plus every weekly indicator as `w_<name>`, aligned to
    the last completed week (no lookahead)."""
    from analytics import bars

    frame = compute(daily, 252)
    weekly = bars.weekly(daily)
    if weekly.empty:
        return frame
    wk = compute(weekly.rename(columns={"week_end": "date"}), 52)
    wk = wk.rename(columns={"date": "week_end"})
    positions = bars.weekly_positions(daily, weekly)
    columns = {}
    for column in wk.columns:
        if column in INDICATOR_COLUMNS_EXCLUDED:
            continue
        columns[f"w_{column}"] = bars.weekly_on_daily(
            daily, weekly_values=wk[column], weekly_frame=weekly,
            positions=positions).to_numpy()
    columns["w_close"] = bars.weekly_on_daily(daily, weekly_frame=weekly,
                                              positions=positions).to_numpy()
    frame = pd.concat([frame, pd.DataFrame(columns, index=frame.index)], axis=1)
    if daily.attrs.get("price_basis"):
        frame.attrs["price_basis"] = daily.attrs["price_basis"]
    return frame


def for_symbol(symbol: str) -> pd.DataFrame:
    from data_sources.yfinance_sync import load_daily
    daily = load_daily(symbol, basis="price")
    return with_weekly(daily) if not daily.empty else pd.DataFrame()


def latest(frame: pd.DataFrame) -> dict:
    """The last row as {column: value}, NaN -> None."""
    if frame is None or frame.empty:
        return {}
    row = frame.iloc[-1].to_dict()
    return {k: (None if isinstance(v, float) and np.isnan(v) else v) for k, v in row.items()}
