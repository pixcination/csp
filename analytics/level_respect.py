"""
Level-respect study -- which moving averages has THIS stock actually held,
how often, how deep did it pierce first, and is that better than chance?
(Phase 10, roadmap §B.3.)

The naive version -- "price came near the 200W EMA and went up afterwards
95% of the time" -- is mostly an artifact of uptrends and small samples: in a
rising market almost any level below price "holds". So this is built as an
event study with a control group, and ranks levels by how much better they
did than the control, at the pessimistic end of the confidence interval.

EVENT (a "test" of level L)
    prior close > L  (approach from above)  and  today's low <= L + band x ATR
    ATR is the 14-day ATR at the PRIOR close (known before the test).
    Debounce: after a test, no new test until the close has stayed above
    L + rearm_atr x ATR for rearm_days consecutive sessions.

OUTCOME over each horizon h (sessions, test day included)
    broke     any close < L_s - tolerance x ATR          (L_s: the level that day)
    held      not broke
    bounced   the high reached L_s + bounce x ATR before the first break
    pierce    deepest (L_s - low_s) / ATR before the break (or in the window
              if it held) -- in ATR units, the cushion a strike needed
    A test within h sessions of the end of the data is censored for that
    horizon (excluded), never counted as "held".

HONESTY
    * Wilson 95% CI on every rate; n below `levels.min_n` -> "insufficient",
      no rate reported as if it meant something.
    * PLACEBO (block bootstrap of the stock's own returns). The same MAs,
      detector and outcomes on `placebo_replicates` synthetic price paths
      built by resampling blocks of `placebo_block_days` from this stock's
      own daily moves (close-to-close return with that day's high/low/open
      relative to close). The paths keep the stock's drift -- so an uptrend
      that makes everything below price "hold" is in the baseline too -- its
      volatility and its short-run clustering, but carry no memory of where
      the moving averages are. The question becomes: did the level hold more
      often than the SAME level on price paths that had no reason to respect
      it? Edge = hold rate - placebo hold rate, Newcombe (hybrid score) CI.
      On a random walk the edge is ~0 (tested); a series built to bounce off
      its 50-day MA shows a clearly positive edge (tested). A level only
      earns "strong" when the CI lower bound of its edge exceeds
      `levels.strong_min_edge_ci_lo`.

      WHY NOT THE ROADMAP'S OFFSET LEVELS. The first implementation used the
      level shifted by a random +/- 2-10%. On a pure random walk the real MAs
      then "held" 6 points LESS often than their offsets: price only reaches
      a level 8% below its MA after a sharp fall, and the MA then keeps
      falling toward price, carrying the offset level away from it. The
      offset placebo was biased against every real level, which read as
      "moving averages are anti-support". Measured, replaced, and the random
      walk check stays in the tests.
    * Split by level slope at the test (rising / falling over
      slope_lookback_days) -- a rising 200-day in a bull market and a falling
      one in a bear market are different questions.
    * A recency-weighted hold rate (half-life config) sits beside the raw one.

Levels: SMA and EMA at `levels.lengths`, daily ("D") and weekly ("W"). Weekly
levels come from `indicators.with_weekly`, i.e. the last COMPLETED week --
no lookahead -- and are tested against daily bars. Everything on the price
basis. Results cached in data/technicals.duckdb -> level_stats.
"""
from __future__ import annotations

import datetime as dt
import math
import zlib
from dataclasses import dataclass

import duckdb
import numpy as np
import pandas as pd

from core.paths import db_technicals, load_config

Z95 = 1.959963984540054
TABLE = "level_stats"


@dataclass(frozen=True)
class Params:
    band_atr: float = 0.5
    tolerance_atr: float = 1.0
    bounce_atr: float = 1.5
    rearm_atr: float = 1.0
    rearm_days: int = 3
    horizons: tuple = (5, 10, 20)
    headline_horizon: int = 10
    min_n: int = 8
    placebo_replicates: int = 20
    placebo_block_days: int = 20
    placebo_seed: int = 7
    slope_lookback_days: int = 10
    recency_half_life_years: float = 3.0
    lookback_years: float = 20
    lengths: tuple = (21, 50, 100, 200)
    strong_min_edge_ci_lo: float = 0.0

    @classmethod
    def from_config(cls) -> "Params":
        cfg = load_config().get("levels", {}) or {}
        known = {k: v for k, v in cfg.items() if k in cls.__dataclass_fields__}
        for key in ("horizons", "lengths"):
            if key in known:
                known[key] = tuple(known[key])
        return cls(**known)


# --- Statistics ------------------------------------------------------------

def wilson(successes: int, n: int, z: float = Z95) -> tuple[float, float]:
    if n <= 0:
        return (float("nan"), float("nan"))
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def newcombe_diff(s1: int, n1: int, s2: int, n2: int) -> tuple[float, float, float]:
    """p1 - p2 with the Newcombe hybrid-score 95% CI."""
    if n1 <= 0 or n2 <= 0:
        return (float("nan"),) * 3
    p1, p2 = s1 / n1, s2 / n2
    l1, u1 = wilson(s1, n1)
    l2, u2 = wilson(s2, n2)
    d = p1 - p2
    return (d, d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2),
            d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2))


# --- Detection and outcomes (arrays) -----------------------------------------

def detect_tests(close: np.ndarray, low: np.ndarray, level: np.ndarray,
                 atr_prev: np.ndarray, p: Params) -> np.ndarray:
    """Indices of test events, debounced. All inputs aligned 1-D arrays."""
    n = len(close)
    if n < 2:
        return np.array([], dtype=int)
    prev_close = np.concatenate([[np.nan], close[:-1]])
    prev_level = np.concatenate([[np.nan], level[:-1]])
    with np.errstate(invalid="ignore"):
        candidate = ((prev_close > prev_level) & (low <= level + p.band_atr * atr_prev)
                     & np.isfinite(level) & np.isfinite(atr_prev) & (atr_prev > 0))
        clear = close > level + p.rearm_atr * atr_prev
    # consecutive days above the re-arm line (vectorised run length)
    positions = np.arange(n)
    last_miss = np.maximum.accumulate(np.where(clear, -1, positions))
    run = positions - last_miss
    rearm = np.flatnonzero(run >= p.rearm_days)
    events: list[int] = []
    for c in np.flatnonzero(candidate):
        if not events:
            events.append(int(c))
            continue
        k = np.searchsorted(rearm, events[-1], side="right")
        if k < len(rearm) and rearm[k] < c:
            events.append(int(c))
    return np.array(events, dtype=int)


def outcomes(events: np.ndarray, close: np.ndarray, high: np.ndarray, low: np.ndarray,
             level: np.ndarray, atr_prev: np.ndarray, horizon: int,
             p: Params) -> pd.DataFrame:
    """Per-event outcome over `horizon` sessions (test day + horizon days).
    Censored events (window runs past the data) are dropped."""
    n = len(close)
    events = events[events + horizon < n]
    if len(events) == 0:
        return pd.DataFrame(columns=["idx", "held", "bounced", "pierce_atr"])
    idx = events[:, None] + np.arange(horizon + 1)[None, :]
    lvl, cl, hi, lo = level[idx], close[idx], high[idx], low[idx]
    a0 = atr_prev[events][:, None]
    broke_mask = cl < lvl - p.tolerance_atr * a0
    any_break = broke_mask.any(axis=1)
    first_break = np.where(any_break, broke_mask.argmax(axis=1), horizon + 1)
    bounce_mask = hi >= lvl + p.bounce_atr * a0
    first_bounce = np.where(bounce_mask.any(axis=1), bounce_mask.argmax(axis=1), horizon + 2)
    cols = np.arange(horizon + 1)[None, :]
    before_break = cols < first_break[:, None]
    depth = np.where(before_break, (lvl - lo) / a0, -np.inf)
    pierce = np.clip(depth.max(axis=1), 0.0, None)
    return pd.DataFrame({"idx": events, "held": ~any_break,
                         "bounced": first_bounce < first_break, "pierce_atr": pierce})


def _slope(level: np.ndarray, lookback: int) -> np.ndarray:
    prior = np.concatenate([np.full(lookback, np.nan), level[:-lookback]])
    with np.errstate(invalid="ignore"):
        return np.where(level > prior, "rising", np.where(level < prior, "falling", "flat"))


# --- The study ---------------------------------------------------------------

def level_columns(p: Params) -> list[tuple[str, str, str]]:
    """[(level_id, timeframe, column)] e.g. ("200W EMA", "W", "w_ema_200")."""
    out = []
    for tf, prefix in (("D", ""), ("W", "w_")):
        for n in p.lengths:
            for kind in ("sma", "ema"):
                out.append((f"{n}{tf} {kind.upper()}", tf, f"{prefix}{kind}_{n}"))
    return out


def _events_table(frame: pd.DataFrame, level: np.ndarray, p: Params,
                  horizons, dates: np.ndarray) -> dict[int, pd.DataFrame]:
    """Detect once, score every horizon: {horizon: events with outcomes}."""
    close, high, low = (frame[c].to_numpy(float) for c in ("close", "high", "low"))
    atr_prev = frame["atr_prev"].to_numpy(float)
    events = detect_tests(close, low, level, atr_prev, p)
    slope = _slope(level, p.slope_lookback_days)
    out = {}
    for horizon in horizons:
        table = outcomes(events, close, high, low, level, atr_prev, horizon, p)
        if not table.empty:
            positions = table["idx"].to_numpy()
            table["date"] = dates[positions]
            table["slope"] = slope[positions]
        else:
            table = pd.DataFrame(columns=["idx", "held", "bounced", "pierce_atr",
                                          "date", "slope"])
        out[horizon] = table
    return out


def _summary(real: pd.DataFrame, placebo: pd.DataFrame, as_of: pd.Timestamp,
             p: Params) -> dict:
    n, held = len(real), int(real["held"].sum()) if len(real) else 0
    pn, pheld = len(placebo), int(placebo["held"].sum()) if len(placebo) else 0
    lo, hi = wilson(held, n)
    edge, elo, ehi = newcombe_diff(held, n, pheld, pn)
    held_rows = real[real["held"]] if n else real
    if n:
        age = (as_of - pd.to_datetime(real["date"])).dt.days.to_numpy() / 365.25
        w = 0.5 ** (age / p.recency_half_life_years)
        recency = float((w * real["held"].to_numpy()).sum() / w.sum())
    else:
        recency = float("nan")
    return {
        "n": n, "held": held, "hold_rate": held / n if n else float("nan"),
        "ci_lo": lo, "ci_hi": hi,
        "bounce_rate": float(real["bounced"].mean()) if n else float("nan"),
        "placebo_n": pn, "placebo_rate": pheld / pn if pn else float("nan"),
        "edge_vs_placebo": edge, "edge_ci_lo": elo, "edge_ci_hi": ehi,
        "median_pierce_atr": float(held_rows["pierce_atr"].median()) if len(held_rows) else float("nan"),
        "p80_pierce_atr": float(held_rows["pierce_atr"].quantile(0.8)) if len(held_rows) else float("nan"),
        "recency_hold_rate": recency,
        "last_test_date": pd.Timestamp(real["date"].max()).date() if n else None,
        "status": "ok" if n >= p.min_n else "insufficient",
    }


def _level_arrays(close: np.ndarray, high: np.ndarray, low: np.ndarray,
                  week_id: np.ndarray, positions: np.ndarray, p: Params) -> dict:
    """The studied MA levels and the prior-day ATR for one price path.

    A lean re-implementation of the indicator columns the study needs, so a
    bootstrap replicate costs milliseconds: daily SMA/EMA, weekly SMA/EMA on
    the weekly closes (grouped by the REAL calendar's week ids) aligned
    through the real `positions` (last completed week -- no lookahead), and
    Wilder ATR. Must agree with indicators.compute -- tested."""
    from analytics.indicators import ema as _ema, sma as _sma
    c = pd.Series(close)
    out = {}
    n_weeks = int(week_id.max()) + 1
    last_in_week = pd.Series(np.arange(len(close))).groupby(week_id).max().to_numpy()
    weekly_close = pd.Series(close[last_in_week])
    for n in p.lengths:
        out[f"sma_{n}"] = _sma(c, n).to_numpy()
        out[f"ema_{n}"] = _ema(c, n).to_numpy()
        for kind, fn in (("sma", _sma), ("ema", _ema)):
            weekly = fn(weekly_close, n).to_numpy()
            weekly = np.concatenate([weekly, np.full(max(n_weeks - len(weekly), 0), np.nan)])
            out[f"w_{kind}_{n}"] = np.where(positions >= 0,
                                            weekly[np.clip(positions, 0, None)], np.nan)
    prev = np.concatenate([[np.nan], close[:-1]])
    tr = np.nanmax(np.vstack([high - low, np.abs(high - prev), np.abs(low - prev)]), axis=0)
    atr = pd.Series(tr).ewm(alpha=1 / 14, min_periods=14, adjust=False).mean().to_numpy()
    out["atr_prev"] = np.concatenate([[np.nan], atr[:-1]])
    return out


def bootstrap_path(close: np.ndarray, high: np.ndarray, low: np.ndarray,
                   rng: np.random.Generator, block: int) -> tuple:
    """A synthetic OHLC path from blocks of the real path's daily moves.

    Each day contributes (log close-to-close return, log high/close, log
    low/close) jointly, so intraday range stays consistent with the move.
    Moving-block bootstrap with block length `block`; starts at the real
    first close."""
    n = len(close)
    ret = np.diff(np.log(close))
    hi_rel = np.log(high[1:] / close[1:])
    lo_rel = np.log(low[1:] / close[1:])
    m = len(ret)
    starts = rng.integers(0, max(m - block, 1), size=m // block + 2)
    picks = (starts[:, None] + np.arange(block)[None, :]).ravel()[:m]
    path = close[0] * np.exp(np.concatenate([[0.0], np.cumsum(ret[picks])]))
    synth_high = np.concatenate([[high[0]], path[1:] * np.exp(hi_rel[picks])])
    synth_low = np.concatenate([[low[0]], path[1:] * np.exp(lo_rel[picks])])
    return path[:n], synth_high[:n], synth_low[:n]


def study(frame: pd.DataFrame, symbol: str = "", p: Params | None = None) -> pd.DataFrame:
    """Level stats for one symbol. `frame` = `indicators.with_weekly(daily)`
    (full history, so the MAs are warm); the study window is the last
    `lookback_years`."""
    p = p or Params.from_config()
    if frame is None or frame.empty:
        return pd.DataFrame()
    from analytics import bars

    frame = frame.copy().reset_index(drop=True)
    frame["atr_prev"] = frame["atr"].shift(1)
    all_dates = pd.to_datetime(frame["date"])
    cutoff = all_dates.max() - pd.DateOffset(years=p.lookback_years)
    # Bootstrap span: the study window plus warm-up for the longest weekly MA.
    warmup_days = max(p.lengths) * 5 + 30
    first = max(int(np.searchsorted(all_dates.to_numpy(), cutoff.to_datetime64())) - warmup_days, 0)
    span = frame.iloc[first:].reset_index(drop=True)
    in_window = (pd.to_datetime(span["date"]) >= cutoff).to_numpy()
    weekly = bars.weekly(span[["date", "open", "high", "low", "close", "volume"]])
    positions = bars.weekly_positions(span, weekly)
    week_id = pd.to_datetime(span["date"]).dt.to_period(bars.WEEK_RULE).factorize()[0]

    window = span[in_window].reset_index(drop=True)
    dates = pd.to_datetime(window["date"]).to_numpy()
    as_of = pd.Timestamp(dates[-1])

    close, high, low = (span[c].to_numpy(float) for c in ("close", "high", "low"))
    rng = np.random.default_rng(zlib.crc32(f"{p.placebo_seed}|{symbol}".encode()))
    replicates = []
    for _ in range(p.placebo_replicates):
        c, h, l = bootstrap_path(close, high, low, rng, p.placebo_block_days)
        arrays = _level_arrays(c, h, l, week_id, positions, p)
        synth = pd.DataFrame({"close": c[in_window], "high": h[in_window],
                              "low": l[in_window],
                              "atr_prev": arrays["atr_prev"][in_window]})
        replicates.append((synth, {k: v[in_window] for k, v in arrays.items()}))

    empty = pd.DataFrame(columns=["idx", "held", "bounced", "pierce_atr", "date", "slope"])
    rows = []
    for level_id, timeframe, column in level_columns(p):
        if column not in window:
            continue
        level = window[column].to_numpy(float)
        real_all = _events_table(window, level, p, p.horizons, dates)
        placebo_all = [_events_table(synth, arrays[column], p, p.horizons, dates)
                       for synth, arrays in replicates]
        for horizon in p.horizons:
            real = real_all[horizon]
            parts = [x[horizon] for x in placebo_all if not x[horizon].empty]
            placebo = pd.concat(parts, ignore_index=True) if parts else empty
            for regime in ("all", "rising", "falling"):
                r = real if regime == "all" else real[real["slope"] == regime]
                pl = placebo if regime == "all" else placebo[placebo["slope"] == regime]
                rows.append({"symbol": symbol, "level_id": level_id, "timeframe": timeframe,
                             "column": column, "horizon": horizon, "slope_regime": regime,
                             **_summary(r, pl, as_of, p),
                             "level_now": float(level[-1]) if np.isfinite(level[-1]) else None})
    out = pd.DataFrame(rows)
    out["as_of"] = as_of.date()
    return out


def test_events(frame: pd.DataFrame, column: str, horizon: int | None = None,
                p: Params | None = None) -> pd.DataFrame:
    """Every test of one level with its outcome -- for charts and audits."""
    p = p or Params.from_config()
    frame = frame.copy()
    frame["atr_prev"] = frame["atr"].shift(1)
    cutoff = pd.to_datetime(frame["date"]).max() - pd.DateOffset(years=p.lookback_years)
    frame = frame[pd.to_datetime(frame["date"]) >= cutoff].reset_index(drop=True)
    dates = pd.to_datetime(frame["date"]).to_numpy()
    horizon = horizon or p.headline_horizon
    out = _events_table(frame, frame[column].to_numpy(float), p, [horizon], dates)[horizon]
    if not out.empty:
        out["level"] = frame[column].to_numpy(float)[out["idx"].to_numpy()]
    return out


def describe(row: dict | pd.Series) -> str:
    """ "200W EMA: 11 of 12 held (92%, CI 65-99%), +31 pts vs placebo" """
    if row["status"] != "ok":
        return f"{row['level_id']}: only {row['n']} test(s) -- insufficient"
    return (f"{row['level_id']}: {row['held']} of {row['n']} held "
            f"({row['hold_rate']:.0%}, CI {row['ci_lo'] * 100:.0f}-{row['ci_hi'] * 100:.0f}%), "
            f"{row['edge_vs_placebo'] * 100:+.0f} pts vs placebo")


# --- Cache -------------------------------------------------------------------

def store(symbol: str, stats: pd.DataFrame) -> None:
    if stats is None or stats.empty:
        return
    stats = stats.copy()
    stats["computed_at"] = dt.datetime.now()
    con = duckdb.connect(str(db_technicals()))
    try:
        con.register("incoming", stats)
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        if TABLE not in tables:
            con.execute(f"CREATE TABLE {TABLE} AS SELECT * FROM incoming WHERE false")
        con.execute(f"DELETE FROM {TABLE} WHERE symbol = ?", [symbol])
        con.execute(f"INSERT INTO {TABLE} BY NAME SELECT * FROM incoming")
        con.unregister("incoming")
    finally:
        con.close()


def load_stats(symbol: str | None = None) -> pd.DataFrame:
    path = db_technicals()
    if not path.exists():
        return pd.DataFrame()
    con = duckdb.connect(str(path), read_only=True)
    try:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        if TABLE not in tables:
            return pd.DataFrame()
        if symbol:
            return con.execute(f"SELECT * FROM {TABLE} WHERE symbol = ?", [symbol]).fetchdf()
        return con.execute(f"SELECT * FROM {TABLE}").fetchdf()
    finally:
        con.close()


# --- Support map ---------------------------------------------------------------

def support_map(symbol: str, frame: pd.DataFrame | None = None,
                stats: pd.DataFrame | None = None, p: Params | None = None,
                iv: float | None = None) -> pd.DataFrame:
    """Every studied level below spot, nearest first, with its strength.

    distance in %, in ATR and in expected-move units (spot x IV x
    sqrt(em_dte_days/365), IV = TastyTrade IV index when not given).
    Strength = CI lower bound of the edge over placebo at the headline
    horizon, for the level's CURRENT slope regime when that has enough tests,
    otherwise all tests. `strong` = status ok and strength above the floor.
    """
    from analytics import indicators
    p = p or Params.from_config()
    frame = frame if frame is not None else indicators.for_symbol(symbol)
    if frame.empty:
        return pd.DataFrame()
    stats = stats if stats is not None else load_stats(symbol)
    last = frame.iloc[-1]
    spot, atr_now = float(last["close"]), float(last["atr"])
    if iv is None:
        try:
            from data_sources import tasty_metrics
            iv = (tasty_metrics.for_symbol(symbol) or {}).get("iv_index")
        except Exception:
            iv = None
    em_days = load_config().get("levels", {}).get("em_dte_days", 30)
    em = spot * float(iv) * math.sqrt(em_days / 365.0) if iv else None

    rows = []
    for level_id, timeframe, column in level_columns(p):
        if column not in frame or not np.isfinite(last[column]) or last[column] >= spot:
            continue
        level = float(last[column])
        series = frame[column].to_numpy(float)
        slope = _slope(series, p.slope_lookback_days)[-1]
        chosen = None
        if stats is not None and not stats.empty:
            candidates = stats[(stats["level_id"] == level_id)
                               & (stats["horizon"] == p.headline_horizon)]
            for regime in (slope, "all"):
                match = candidates[candidates["slope_regime"] == regime]
                if not match.empty and match.iloc[0]["status"] == "ok":
                    chosen = match.iloc[0]
                    break
            if chosen is None and not candidates.empty:
                chosen = candidates[candidates["slope_regime"] == "all"].iloc[0]
        row = {"symbol": symbol, "level_id": level_id, "timeframe": timeframe,
               "level": level, "slope_now": slope,
               "distance_pct": level / spot - 1.0,
               "distance_atr": (spot - level) / atr_now if atr_now else None,
               "distance_em": (spot - level) / em if em else None}
        if chosen is not None:
            row.update({k: chosen[k] for k in (
                "slope_regime", "n", "held", "hold_rate", "ci_lo", "ci_hi",
                "placebo_rate", "edge_vs_placebo", "edge_ci_lo", "median_pierce_atr",
                "p80_pierce_atr", "recency_hold_rate", "last_test_date", "status")})
            row["strength"] = chosen["edge_ci_lo"]
            row["strong"] = bool(chosen["status"] == "ok"
                                 and chosen["edge_ci_lo"] > p.strong_min_edge_ci_lo)
            row["summary"] = describe(chosen)
        else:
            row.update({"status": "not studied", "strong": False})
        rows.append(row)
    out = pd.DataFrame(rows)
    return out.sort_values("level", ascending=False).reset_index(drop=True) if not out.empty else out
