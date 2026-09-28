"""Phase 20 -- Outlook v1: point-in-time features and labels, the ridge
logistic, walk-forward skill, the dials (shrink, confidence, bands), the
Screener filters and the recommender's trend condition."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from analytics import outlook


def _walk(seed: int, n: int = 2600, drift: float = 0.0003, vol_regimes: bool = True):
    rng = np.random.default_rng(seed)
    vol = np.full(n, 0.012)
    if vol_regimes:                       # volatility clusters: calm and stormy spells
        vol = 0.008 + 0.012 * (np.sin(np.arange(n) / 90.0) > 0.3)
    r = rng.normal(drift, vol)
    close = 100 * np.exp(np.cumsum(r))
    dates = pd.bdate_range("2012-01-02", periods=n)
    return pd.DataFrame({"date": dates, "open": close, "high": close * (1 + vol),
                         "low": close * (1 - vol), "close": close, "volume": 1e6})


DATA = {s: _walk(i) for i, s in enumerate(["SPY", "AAA", "BBB", "CCC"])}
CFG = {**outlook.cfg(), "horizons": [5, 21], "history_start": "2012-01-01",
       "test_start_year": 2018, "n_paths": 400, "base_window_days": 1500}


def loader(symbol):
    return DATA[symbol].copy()


# --- Features and labels -------------------------------------------------------------------

def test_features_are_point_in_time():
    full = outlook.features(DATA["AAA"], outlook.spy_features(DATA["SPY"]))
    cut = outlook.features(DATA["AAA"].iloc[:2000], outlook.spy_features(DATA["SPY"]))
    cols = list(outlook.FEATURES[:-1])
    pd.testing.assert_frame_equal(full.loc[:1999, cols].reset_index(drop=True),
                                  cut[cols].reset_index(drop=True))


def test_labels_and_base_rates():
    frame = outlook.features(DATA["AAA"])
    lab = outlook.labels(frame, 21)
    td = outlook.steps(21)
    assert td == 14
    r = np.log(frame["close"].shift(-td) / frame["close"])
    em = frame["rv20_level"] * np.sqrt(td / 252)
    i = 500
    assert lab["up"].iloc[i] == float(r.iloc[i] > em.iloc[i] / 4)
    assert lab["inside"].iloc[i] == float(abs(r.iloc[i]) < em.iloc[i])
    assert lab.iloc[-td:].isna().all().all()                     # the future is unknown
    base = outlook.base_rates(lab, 21, CFG)
    # The base rate at t only uses outcomes known by t (starts <= t - td).
    t = 1200
    known = lab["up"].iloc[max(0, t - td - 1500 + 1):t - td + 1]
    assert base["up"].iloc[t] == pytest.approx(known.mean())


def test_fit_logit_recovers_the_signal():
    rng = np.random.default_rng(3)
    x = pd.DataFrame({"a": rng.normal(size=4000), "b": rng.normal(size=4000)})
    p = 1 / (1 + np.exp(-(0.2 + 1.5 * x["a"])))
    y = (rng.random(4000) < p).astype(float)
    model = outlook.fit_logit(x, y.to_numpy(), l2=1.0, feature_names=["a", "b"])
    assert model.coef[0] > 1.0 and abs(model.coef[1]) < 0.15
    again = outlook.Logit(**model.to_dict())
    assert np.allclose(again.predict(x), model.predict(x))


def test_brier_skill():
    y = np.array([1, 0, 1, 0], float)
    base = np.full(4, 0.5)
    assert outlook.brier_skill(base, y, base)[0] == pytest.approx(0.0)
    assert outlook.brier_skill(y, y, base)[0] == pytest.approx(1.0)
    assert outlook.brier_skill(1 - y, y, base)[0] < 0


# --- Scores -------------------------------------------------------------------------------

def test_dial_mappings_shrink_and_levels():
    assert outlook.direction_raw(0.5, 0.5) == 5.0
    assert outlook.direction_raw(0.7, 0.1) == pytest.approx(8.0)
    assert outlook.range_raw(0.6, 0.6) == 5.0
    assert outlook.range_raw(1.0, 0.6) == 10.0 and outlook.range_raw(0.0, 0.6) == 0.0
    assert outlook.shrink_for(0.0, CFG) == 0.0 and outlook.shrink_for(0.05, CFG) == 1.0
    assert 0 < outlook.shrink_for(0.015, CFG) < 1
    assert outlook.level(0.001, 500, 0.0, CFG) == "none"
    assert outlook.level(0.05, 20, 0.0, CFG) == "none"               # too few outcomes
    assert outlook.level(0.05, 500, 0.0, CFG) == "high"
    assert outlook.level(0.05, 500, 0.20, CFG) == "low"              # models disagree
    assert [outlook.trend_class(v) for v in (6.5, 5.0, 3.0, None)] == \
        ["uptrend", "range", "downtrend", None]


# --- Walk-forward and the live dials -----------------------------------------------------------

@pytest.fixture(scope="module")
def validated(tmp_path_factory):
    folder = tmp_path_factory.mktemp("outlook")
    mp = pytest.MonkeyPatch()
    mp.setattr(outlook, "folder", lambda: folder)
    mp.setattr(outlook, "validation_dir", lambda: folder)
    result = outlook.validate(["SPY", "AAA", "BBB", "CCC"], loader=loader, c=CFG)
    yield result, folder
    mp.undo()


def test_walk_forward_skill_table(validated):
    result, folder = validated
    skill = result["skill"]
    assert {"ALL", "AAA"} <= set(skill["symbol"])
    assert set(skill["component"]) == {"blend", "logit", "hist"}
    assert set(skill["horizon"]) == {5, 21}
    pooled = skill[(skill["symbol"] == "ALL") & (skill["component"] == "blend")]
    direction = pooled[pooled["event"].isin(["up", "down"])]["bss"]
    assert direction.abs().max() < 0.05                  # a random walk: no direction skill
    inside = pooled[pooled["event"] == "inside"]["bss"]
    assert inside.min() > 0                              # clustered vol: range is predictable
    own = skill[(skill["symbol"] == "AAA") & (skill["component"] == "blend")].iloc[0]
    assert own["bss_shrunk"] != own["bss"] or own["n_eff"] == 0
    assert (folder / "model.json").exists() and (folder / "outlook_skill.parquet").exists()
    assert set(result["model"]) == {f"{h}|{e}" for h in (5, 21) for e in outlook.EVENTS}
    # Walk-forward: no prediction was fitted on its own year's outcomes.
    preds = result["predictions"]
    assert preds["date"].min() >= pd.Timestamp("2018-01-01")


def test_live_rows_shrink_direction_without_skill(validated):
    _, folder = validated
    metrics = {"iv_index": 0.25, "iv_index_15d": 0.22}
    frame = outlook.build(["AAA", "BBB"], c=CFG, loader=loader,
                          metrics_source=lambda s: metrics)
    assert len(frame) == 4 and set(frame["horizon"]) == {5, 21}
    for dial in outlook.DIALS:
        assert frame[dial].between(0, 10).all()
        assert (frame[f"{dial}_lo"] <= frame[dial]).all() and (frame[dial] <= frame[f"{dial}_hi"]).all()
    none = frame[frame["direction_conf"] == "none"]
    assert (none["direction"] == 5.0).all()              # no skill: the arrow sits at neutral
    assert set(frame["range_conf"]) <= set(outlook.LEVELS)
    row = frame.iloc[0].to_dict()
    assert row["iv"] == pytest.approx(0.22 + (0.25 - 0.22) * 0)        # 5d: the 15-day IV
    assert outlook.divergence_sentence(row)
    assert (folder / "latest.parquet").exists()


def test_at_interpolates_between_grid_horizons():
    table = pd.DataFrame({"ticker": ["X", "X"], "horizon": [7, 14], "direction": [4.0, 6.0],
                          "direction_conf": ["low", "high"], "trading_days": [5, 10]})
    mid = outlook.at(table, "X", 10.5)
    assert mid["direction"] == pytest.approx(5.0) and mid["direction_conf"] == "high"
    assert outlook.at(table, "X", 2)["direction"] == 4.0             # clamped
    assert outlook.at(table, "Y", 7) is None


def test_screener_annotate_and_filter():
    table = pd.DataFrame({"ticker": ["A"] * 2 + ["B"] * 2, "horizon": [14, 30] * 2,
                          "direction": [7.0, 7.0, 5.0, 5.0], "direction_conf": ["medium"] * 2 + ["none"] * 2,
                          "range": [4.0, 4.0, 8.0, 8.0], "range_conf": ["high"] * 4,
                          "volatility": [6.0] * 4, "volatility_conf": ["medium"] * 4})
    rows = pd.DataFrame({"ticker": ["A", "B", "B"], "dte_calendar": [14, 30, 60],
                         "trade_id": ["a", "b1", "b2"]})
    fixed = outlook.annotate(rows, table, horizon=14)
    bullish = outlook.filter_rows(fixed, direction=(6, 10), min_confidence="medium")
    assert list(bullish["trade_id"]) == ["a"]
    own = outlook.annotate(rows, table)
    ranged = outlook.filter_rows(own, range_=(7, 10), dte_window=(21, 45))
    assert list(ranged["trade_id"]) == ["b1"]                       # b2 is outside the window
    assert len(outlook.filter_rows(own)) == 3


def test_recommender_reads_the_outlook_direction():
    from analytics import recommender
    table = pd.DataFrame({"ticker": ["A", "A"], "horizon": [14, 30], "direction": [6.5, 6.5],
                          "direction_conf": ["medium", "medium"]})
    cond = recommender._conditions("A", dt.date(2026, 9, 28), {}, None, None, 30, None,
                                   table, 21)
    assert cond["trend"] == "uptrend" and cond["trend_source"] == "outlook"
    fallback = recommender._conditions("B", dt.date(2026, 9, 28), {}, None, None, 30, None,
                                       table, 21)
    assert fallback["trend_source"] == "trend_state"


def test_pipeline_has_an_outlook_stage():
    from pipeline import run
    assert ("outlook", "Outlook dials") in run.STAGES


def test_volatility_is_relative_richness_across_the_universe():
    """Tom 2026-09-28: the dial is the percentile of IV / forecast across the
    universe at that horizon; the ratio itself stays beside it."""
    frame = pd.DataFrame({"ticker": list("ABCDE") * 2, "horizon": [7] * 5 + [30] * 5,
                          "vol_ratio": [0.8, 1.0, 1.2, 1.4, 2.5, 1.1, 1.1, 1.1, 1.1, None]})
    frame["vol_ratio_lo"], frame["vol_ratio_hi"] = frame["vol_ratio"] * 0.9, frame["vol_ratio"] * 1.1
    out = outlook.relative_volatility(frame)
    seven = out[out["horizon"] == 7].set_index("ticker")["volatility"]
    assert list(seven) == pytest.approx([1.0, 3.0, 5.0, 7.0, 9.0])      # midrank percentiles
    thirty = out[out["horizon"] == 30].set_index("ticker")["volatility"]
    assert thirty[list("ABCD")].tolist() == [5.0] * 4 and np.isnan(thirty["E"])
    row = out.iloc[2]
    assert row["volatility_lo"] <= row["volatility"] <= row["volatility_hi"]


def test_no_edge_readings_are_blank_for_the_screener():
    table = pd.DataFrame({"ticker": ["A", "A"], "horizon": [14, 30], "direction": [5.0, 5.0],
                          "direction_conf": ["none", "none"], "range": [7.5, 7.5],
                          "range_conf": ["high", "high"], "volatility": [6.0, 6.0],
                          "volatility_conf": ["low", "low"]})
    rows = outlook.annotate(pd.DataFrame({"ticker": ["A"], "dte_calendar": [20]}), table)
    assert np.isnan(rows["outlook_direction"].iloc[0]) and rows["outlook_range"].iloc[0] == 7.5
    assert outlook.filter_rows(rows, direction=(0, 10)).empty           # never matches
    assert outlook.no_edge({"direction_conf": "none"}) and not outlook.no_edge(
        {"volatility_conf": "none"}, "volatility")
