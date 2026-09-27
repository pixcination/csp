"""
Phase 10: indicators, trend state, the level-respect event study, the RSI
extremes study, the support map and the cache.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics import indicators, level_respect as lr, oscillator_study, trend_state  # noqa: E402

P = lr.Params()          # code defaults: band 0.5, tol 1.0, bounce 1.5, rearm 1 ATR x 3 days


# --- Indicators ---------------------------------------------------------------

def _frame(close, spread=1.0, start="2020-01-01"):
    close = np.asarray(close, dtype=float)
    dates = pd.bdate_range(start, periods=len(close))
    return pd.DataFrame({"date": dates, "open": close, "high": close + spread,
                         "low": close - spread, "close": close, "volume": 1e6})


def test_sma_and_ema_match_hand_values():
    close = pd.Series([1.0, 2, 3, 4, 5])
    assert indicators.sma(close, 3).tolist()[2:] == [2.0, 3.0, 4.0]
    assert np.isnan(indicators.sma(close, 3).iloc[1])          # no partial window
    ema = indicators.ema(close, 3)                               # alpha = 0.5
    assert ema.iloc[2] == pytest.approx(2.25)                    # 1, 1.5, 2.25
    assert ema.iloc[4] == pytest.approx(4.0625)


def test_rsi_extremes_and_wilder_atr():
    up = pd.Series(np.arange(1, 40, dtype=float))
    assert indicators.rsi(up, 14).iloc[-1] == pytest.approx(100.0)
    down = pd.Series(np.arange(40, 1, -1, dtype=float))
    assert indicators.rsi(down, 14).iloc[-1] == pytest.approx(0.0)
    frame = _frame(np.full(60, 100.0), spread=1.0)               # true range always 2
    assert indicators.atr(frame, 14).iloc[-1] == pytest.approx(2.0)
    assert np.isnan(indicators.atr(frame, 14).iloc[12])


def test_adx_is_high_in_a_steady_trend_and_low_in_chop():
    trend = indicators.adx(_frame(np.linspace(100, 200, 300)), 14)
    rng = np.random.default_rng(1)
    chop = indicators.adx(_frame(100 + rng.normal(0, 1, 300).cumsum() * 0.1), 14)
    assert trend["adx"].iloc[-1] > 50
    assert chop["adx"].iloc[-1] < trend["adx"].iloc[-1]
    assert trend["plus_di"].iloc[-1] > trend["minus_di"].iloc[-1]


def test_weekly_indicators_never_look_ahead():
    rng = np.random.default_rng(3)
    daily = _frame(100 + rng.normal(0, 1, 400).cumsum(), start="2024-01-01")
    frame = indicators.with_weekly(daily)
    from analytics import bars
    weekly = indicators.compute(bars.weekly(daily).rename(columns={"week_end": "date"}), 52)
    wk = bars.weekly(daily)
    for i in (150, 151, 152, 153, 154, 300):
        day = pd.Timestamp(frame["date"].iloc[i])
        done = wk[pd.to_datetime(wk["scheduled_last_session"]) <= day]
        expected = weekly["sma_21"].iloc[len(done) - 1] if len(done) else np.nan
        got = frame["w_sma_21"].iloc[i]
        assert (np.isnan(got) and np.isnan(expected)) or got == pytest.approx(expected)
        # and it never equals a value computed from a week that has not closed
        assert pd.Timestamp(done["scheduled_last_session"].iloc[-1]) <= day


# --- Trend state ---------------------------------------------------------------

def test_trend_state_classifies_clean_series():
    rng = np.random.default_rng(5)
    up = indicators.compute(_frame(np.linspace(50, 150, 400) + rng.normal(0, 0.3, 400)))
    down = indicators.compute(_frame(np.linspace(150, 50, 400) + rng.normal(0, 0.3, 400)))
    flat = indicators.compute(_frame(100 + rng.normal(0, 0.8, 400)))
    assert trend_state.classify(up).iloc[-1] == "uptrend"
    assert trend_state.classify(down).iloc[-1] == "downtrend"
    assert trend_state.classify(flat).iloc[-1] == "range"
    assert trend_state.classify(up).iloc[50] is None           # EMA200 not warm yet


# --- The detector: planted tests ---------------------------------------------------

def _planted(n=320, dips=(50, 100, 150, 200, 250), near_miss=(75,), double=(101,),
             broke=(150,)):
    """Level fixed at 100, ATR fixed at 2, price parked at 110. Each planted
    dip touches 100.5 (inside the 0.5-ATR band); a near miss only reaches 101.5;
    a 'double' dips again the day after a test, before re-arming."""
    close = np.full(n, 110.0)
    low = np.full(n, 109.0)
    high = np.full(n, 111.0)
    for d in dips:
        low[d], close[d] = 100.5, 101.0
    for d in near_miss:
        low[d], close[d] = 101.5, 103.0
    for d in double:
        low[d], close[d] = 100.4, 101.5
    for d in broke:                        # after this test, closes sink below 98
        close[d + 1:d + 4] = 97.0
        low[d + 1:d + 4] = 96.0
        high[d + 1:d + 4] = 98.0
    level = np.full(n, 100.0)
    atr_prev = np.full(n, 2.0)
    return close, high, low, level, atr_prev


def test_detector_finds_exactly_the_planted_tests():
    close, high, low, level, atr_prev = _planted()
    events = lr.detect_tests(close, low, level, atr_prev, P)
    assert events.tolist() == [50, 100, 150, 200, 250]      # no near miss, no double


def test_approach_from_below_is_not_a_test():
    close, high, low, level, atr_prev = _planted(dips=(), near_miss=(), double=(), broke=())
    close[:40] = 95.0                                  # below the level ...
    low[:40] = 94.0
    low[40], close[40] = 99.5, 101.0                  # ... then crossing up through it
    assert 40 not in lr.detect_tests(close, low, level, atr_prev, P).tolist()


def test_outcomes_held_broke_bounce_and_pierce():
    close, high, low, level, atr_prev = _planted()
    events = lr.detect_tests(close, low, level, atr_prev, P)
    out = lr.outcomes(events, close, high, low, level, atr_prev, 10, P).set_index("idx")
    assert bool(out.loc[50, "held"]) and not bool(out.loc[150, "held"])
    # pierce: the level is 100, the lowest low before any break is 100.5 -> 0 ATR
    assert out.loc[50, "pierce_atr"] == pytest.approx(0.0)
    # 110 >= 100 + 1.5 x 2 = 103 the next day: bounced
    assert bool(out.loc[50, "bounced"])
    # the double dip on day 101 pierces 0 but is inside test 100's window
    assert out.loc[100, "pierce_atr"] == pytest.approx(0.0)


def test_pierce_depth_counts_a_dip_below_the_level_that_held():
    close, high, low, level, atr_prev = _planted(dips=(50,), near_miss=(), double=(), broke=())
    low[51], close[51] = 98.8, 99.5          # 0.6 ATR under the level, close above 98
    out = lr.outcomes(np.array([50]), close, high, low, level, atr_prev, 10, P)
    assert bool(out["held"].iloc[0])
    assert out["pierce_atr"].iloc[0] == pytest.approx(0.6)


def test_tests_near_the_end_are_censored_not_counted_as_held():
    close, high, low, level, atr_prev = _planted(dips=(50, 312), near_miss=(), double=(),
                                                 broke=())
    events = lr.detect_tests(close, low, level, atr_prev, P)
    assert events.tolist() == [50, 312]
    assert lr.outcomes(events, close, high, low, level, atr_prev, 5, P)["idx"].tolist() == [50, 312]
    assert lr.outcomes(events, close, high, low, level, atr_prev, 10, P)["idx"].tolist() == [50]


# --- Statistics ------------------------------------------------------------------------

def test_wilson_interval_matches_hand_values():
    lo, hi = lr.wilson(11, 12)
    assert lo == pytest.approx(0.6461, abs=1e-3) and hi == pytest.approx(0.9851, abs=1e-3)
    assert lr.wilson(0, 10)[0] == 0.0 and lr.wilson(10, 10)[1] == pytest.approx(1.0)


def test_newcombe_difference_contains_zero_for_equal_rates():
    d, lo, hi = lr.newcombe_diff(30, 50, 300, 500)
    assert d == pytest.approx(0.0) and lo < 0 < hi


def test_describe_matches_the_roadmap_format():
    row = {"level_id": "200W EMA", "status": "ok", "held": 11, "n": 12,
           "hold_rate": 11 / 12, "ci_lo": 0.6461, "ci_hi": 0.9851, "edge_vs_placebo": 0.31}
    assert lr.describe(row) == "200W EMA: 11 of 12 held (92%, CI 65-99%), +31 pts vs placebo"
    assert "insufficient" in lr.describe({**row, "status": "insufficient", "n": 3})


# --- Placebo on a random walk -------------------------------------------------------------

@pytest.fixture(scope="module")
def random_walk_study():
    rng = np.random.default_rng(2026)
    n = 8000
    returns = rng.normal(0.0, 0.012, n)
    close = 100 * np.exp(np.cumsum(returns))
    spread = np.abs(rng.normal(0, 0.008, n)) * close
    daily = pd.DataFrame({"date": pd.bdate_range("1994-01-03", periods=n),
                          "open": close, "high": close + spread, "low": close - spread,
                          "close": close, "volume": 1e6})
    frame = indicators.with_weekly(daily)
    return lr.study(frame, "RW", lr.Params(lookback_years=100))


def test_placebo_edge_is_about_zero_on_a_random_walk(random_walk_study):
    stats = random_walk_study
    head = stats[(stats["horizon"] == 10) & (stats["slope_regime"] == "all")
                 & (stats["status"] == "ok")]
    assert len(head) >= 12
    pooled = head["held"].sum() / head["n"].sum()
    placebo = (head["placebo_rate"] * head["placebo_n"]).sum() / head["placebo_n"].sum()
    assert abs(pooled - placebo) < 0.05
    # and a random walk produces no more "strong" levels than chance allows
    assert int((head["edge_ci_lo"] > 0).sum()) <= 2


def test_every_level_reports_its_sample_size(random_walk_study):
    assert (random_walk_study["n"] >= 0).all()
    assert set(random_walk_study["status"]) <= {"ok", "insufficient"}
    assert set(random_walk_study["slope_regime"]) == {"all", "rising", "falling"}


def _respecting_series(n=7000, length=50, seed=11):
    """A random walk that genuinely respects its 50-day SMA: every time the low
    comes within 0.3% of the SMA from above, the next five sessions rise."""
    rng = np.random.default_rng(seed)
    close = np.empty(n)
    close[0] = 100.0
    push = 0
    window_sum = 0.0
    for t in range(1, n):
        r = rng.normal(0.0, 0.011)
        if push:
            r = abs(r) + 0.004
            push -= 1
        close[t] = close[t - 1] * np.exp(r)
        if t >= length:
            sma_prev = close[t - length:t].mean()
            if close[t - 1] > sma_prev and close[t] * 0.994 <= sma_prev * 1.003 and not push:
                push = 5
    spread = close * 0.006
    return pd.DataFrame({"date": pd.bdate_range("1994-01-03", periods=n), "open": close,
                         "high": close + spread, "low": close - spread, "close": close,
                         "volume": 1e6})


def test_a_level_that_is_really_respected_shows_a_positive_edge():
    """Positive control: the study must be able to find respect when it exists."""
    frame = indicators.with_weekly(_respecting_series())
    stats = lr.study(frame, "RESPECT", lr.Params(lookback_years=100, lengths=(50, 200)))
    row = stats[(stats["level_id"] == "50D SMA") & (stats["horizon"] == 10)
                & (stats["slope_regime"] == "all")].iloc[0]
    assert row["status"] == "ok"
    assert row["edge_vs_placebo"] > 0.10
    assert row["edge_ci_lo"] > 0


def test_bootstrap_levels_match_the_indicator_module():
    """The placebo's lean level computation must equal indicators.compute."""
    from analytics import bars
    rng = np.random.default_rng(8)
    daily = _frame(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 2500))), start="2010-01-04")
    full = indicators.with_weekly(daily)
    weekly = bars.weekly(daily)
    arrays = lr._level_arrays(daily["close"].to_numpy(float), daily["high"].to_numpy(float),
                              daily["low"].to_numpy(float),
                              pd.to_datetime(daily["date"]).dt.to_period("W-FRI").factorize()[0],
                              bars.weekly_positions(daily, weekly), P)
    for column in ("sma_50", "ema_200", "w_sma_21", "w_ema_200"):
        np.testing.assert_allclose(arrays[column], full[column].to_numpy(float), equal_nan=True)
    np.testing.assert_allclose(arrays["atr_prev"][1:], full["atr"].to_numpy(float)[:-1],
                               equal_nan=True)


def test_bootstrap_path_keeps_the_return_distribution():
    rng = np.random.default_rng(3)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.012, 3000)))
    high, low = close * 1.005, close * 0.995
    path, h, l = lr.bootstrap_path(close, high, low, np.random.default_rng(1), 20)
    real, synth = np.diff(np.log(close)), np.diff(np.log(path))
    assert len(path) == len(close) and path[0] == close[0]
    assert np.std(synth) == pytest.approx(np.std(real), rel=0.1)
    assert (h >= path - 1e-9).all() and (l <= path + 1e-9).all()


# --- RSI extremes ---------------------------------------------------------------------------

def test_rsi_episodes_count_crossings_not_days():
    rsi = pd.Series([50, 29, 25, 28, 40, 27, 26, 50, 75, 80, 60])
    assert oscillator_study.episode_starts(rsi, "below", 30).tolist() == [1, 5]
    assert oscillator_study.episode_starts(rsi, "above", 70).tolist() == [8]


def test_forward_returns_drop_censored_episodes():
    close = np.array([100.0, 101, 102, 103, 104, 105])
    out = oscillator_study.forward_returns(close, np.array([0, 3, 5]), 2)
    assert out.tolist() == pytest.approx([0.02, 105 / 103 - 1])


# --- Support map -------------------------------------------------------------------------------

def test_support_map_orders_levels_below_spot_and_flags_strength():
    rng = np.random.default_rng(9)
    daily = _frame(np.linspace(50, 150, 1200) + rng.normal(0, 0.5, 1200), start="2020-01-01")
    frame = indicators.with_weekly(daily)
    stats = pd.DataFrame([
        {"level_id": lid, "horizon": 10, "slope_regime": regime, "status": "ok",
         "n": 20, "held": 15, "hold_rate": 0.75, "ci_lo": 0.53, "ci_hi": 0.89,
         "placebo_rate": 0.55, "edge_vs_placebo": 0.20,
         "edge_ci_lo": 0.05 if lid == "50D EMA" else -0.05,
         "median_pierce_atr": 0.3, "p80_pierce_atr": 0.6, "recency_hold_rate": 0.7,
         "last_test_date": None}
        for lid, _, _ in lr.level_columns(P) for regime in ("all", "rising", "falling")])
    smap = lr.support_map("TEST", frame=frame, stats=stats, p=P, iv=0.25)
    spot = frame["close"].iloc[-1]
    assert (smap["level"] < spot).all()
    assert smap["level"].is_monotonic_decreasing                  # nearest first
    assert smap.set_index("level_id").loc["50D EMA", "strong"]
    assert not smap.set_index("level_id").loc["200D SMA", "strong"]
    assert (smap["distance_em"] > 0).all()


# --- Cache ---------------------------------------------------------------------------------------

def test_study_cache_round_trip(tmp_path, monkeypatch):
    from analytics import technical_study
    db = tmp_path / "technicals.duckdb"
    for module in (technical_study, lr, oscillator_study):
        monkeypatch.setattr(module, "db_technicals", lambda: db)
    rng = np.random.default_rng(4)
    daily = _frame(100 + rng.normal(0, 1, 800).cumsum() * 0.3 + np.linspace(0, 40, 800))
    monkeypatch.setattr(indicators, "for_symbol",
                        lambda symbol: indicators.with_weekly(daily))
    result = technical_study.study_symbol("SYN")
    assert result["trend_state"] in trend_state.STATES
    latest = technical_study.load_latest("SYN")
    assert latest["close"].iloc[0] == pytest.approx(daily["close"].iloc[-1])
    assert len(lr.load_stats("SYN")) == len(lr.level_columns(lr.Params.from_config())) * 3 * 3
    assert not oscillator_study.load_stats("SYN").empty
    technical_study.study_symbol("SYN")                    # re-run replaces, not appends
    assert len(lr.load_stats("SYN")) == len(lr.level_columns(lr.Params.from_config())) * 3 * 3


def test_pipeline_runs_technicals_in_the_data_stages():
    import pipeline.run as run
    keys = [k for k, _ in run.STAGES]
    assert keys.index("stage1") < keys.index("technicals") < keys.index("chains")
