"""
Phase 13: the probability engine (models G, H, T), policies net of fees,
the blend, ranking, and the walk-forward validation helpers.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics import prob_engine as pe  # noqa: E402
from analytics import probabilities  # noqa: E402
from analytics.scan_request import ScanRequest  # noqa: E402
from analytics.strategies.base import Leg, Position  # noqa: E402


def _cfg(**kw) -> pe.EngineConfig:
    cfg = pe.EngineConfig()
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def _csp(strike=95.0, iv=0.25, credit=1.0):
    return Position("csp", "T", [Leg("put", "short", strike, "x", iv=iv)], credit,
                    collateral_per_contract=strike * 100)


def _pcs(short=100.0, long=95.0, iv=0.25, credit=1.5):
    return Position("pcs", "T", [Leg("put", "short", short, "x", iv=iv),
                                 Leg("put", "long", long, "x", iv=iv)], credit)


def _daily(n=3000, seed=4, sigma=0.012):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0003, sigma, n)))
    return pd.DataFrame({"date": pd.bdate_range("2012-01-02", periods=n), "open": close,
                         "high": close * 1.006, "low": close * 0.994, "close": close,
                         "volume": 1e6})


# --- Model G against Black-Scholes -------------------------------------------------------

@pytest.mark.parametrize("strike, vol, dte", [(95, 0.25, 30), (100, 0.20, 7), (90, 0.40, 45),
                                               (105, 0.15, 60)])
def test_g_matches_n_d2_within_three_standard_errors(strike, vol, dte):
    cfg = _cfg()
    steps = max(round(dte * 252 / 365), 1)
    spec = pe.TradeSpec(_csp(strike, vol), 100.0, dte, steps, 1, strike * 100)
    normals = pe.g_normals(cfg.n_paths, steps, np.random.default_rng(cfg.seed))
    result = pe.evaluate(spec, pe.g_log_returns(normals, vol, cfg.rate, dte / 365), [100], cfg)
    t = dte / 365
    d2 = (math.log(100 / strike) + (cfg.rate - vol * vol / 2) * t) / (vol * math.sqrt(t))
    p = result["p_hit_100"]                      # short put expires worthless = S_T >= K
    se = math.sqrt(p * (1 - p) / cfg.n_paths)
    assert abs(p - norm.cdf(d2)) < 3 * se


def test_grid_pricer_matches_the_reference_and_parity():
    rng = np.random.default_rng(1)
    spot = 100 * np.exp(rng.normal(0, 0.02, (500, 20)).cumsum(1))
    tau = np.linspace(20, 0, 20) / 365
    vol = np.full(20, 0.3)
    ref, _ = pe.bs_price(spot, 97.0, tau[None, :], vol[None, :], 0.04, "put")
    grid, _ = pe.bs_price_grid(spot, 97.0, tau, vol, 0.04, "put", np.log(spot))
    assert np.abs(ref - grid).max() < 1e-10
    call, _ = pe.bs_price_grid(spot, 97.0, tau, vol, 0.04, "call")
    parity = call[:, :-1] - grid[:, :-1] - (spot[:, :-1] - 97.0 * np.exp(-0.04 * tau[:-1]))
    assert np.abs(parity).max() < 1e-9
    assert np.allclose(grid[:, -1], np.maximum(97.0 - spot[:, -1], 0))   # expiry = intrinsic


# --- Bootstraps ------------------------------------------------------------------------------

def test_block_bootstrap_draws_whole_blocks_from_the_allowed_starts():
    returns = np.arange(100, dtype=float)
    paths = pe.bootstrap(returns, np.array([10, 50]), 200, 12, 5, np.random.default_rng(0))
    increments = np.diff(np.concatenate([np.zeros((200, 1)), paths], axis=1), axis=1)
    firsts = increments[:, [0, 5, 10]]
    assert set(np.unique(firsts)) <= {10.0, 50.0}
    assert (np.diff(increments[:, :5], axis=1) == 1).all()             # consecutive days


def test_h_conditions_on_volatility_and_is_reproducible():
    cfg = _cfg(n_paths=2000)
    daily = _daily()
    a, _, starts = pe.h_paths(daily, 20, cfg, np.random.default_rng(3))
    b, _, _ = pe.h_paths(daily, 20, cfg, np.random.default_rng(3))
    assert "@ RV~" in a.label and a.n_starts == len(starts) and a.effective_n == len(starts) // 5
    assert np.array_equal(a.log_returns, b.log_returns)


def test_t_relaxes_then_falls_back():
    from analytics import indicators
    cfg = _cfg(n_paths=1000)
    daily = _daily()
    _, frame, starts = pe.h_paths(daily, 20, cfg, np.random.default_rng(0))
    state = pe.technical_state(indicators.compute(daily), cfg, None)
    loose = _cfg(n_paths=1000, min_starts=50)
    t = pe.t_paths(frame, starts, state, 20, loose, np.random.default_rng(0))
    assert t.log_returns is not None and "trend=" in t.label and t.n_starts >= 50
    everything_relaxed = pe.t_paths(frame, starts, state, 20, cfg, np.random.default_rng(0))
    if "trend=" not in (everything_relaxed.label or ""):
        assert everything_relaxed.flag == "fell back to H"          # never H counted twice
    strict = _cfg(n_paths=1000, min_starts=10**6)
    fallback = pe.t_paths(frame, starts, state, 20, strict, np.random.default_rng(0))
    assert fallback.log_returns is None and fallback.flag == "fell back to H"


# --- Evaluation --------------------------------------------------------------------------------

def _run(position, dte=30, targets=(25, 50, 100), vol=0.25, **kw):
    cfg = _cfg(n_paths=4000, **kw)
    steps = max(round(dte * 252 / 365), 1)
    spec = pe.TradeSpec(position, 100.0, dte, steps, 1,
                        position.collateral, event_day=kw.pop("event_day", None)
                        if "event_day" in kw else None)
    normals = pe.g_normals(cfg.n_paths, steps, np.random.default_rng(cfg.seed))
    return pe.evaluate(spec, pe.g_log_returns(normals, vol, cfg.rate, dte / 365),
                       list(targets), cfg), cfg


def test_curves_policies_and_fees():
    result, cfg = _run(_csp(credit=1.0))
    for curve in result["curves"].values():
        assert (np.diff(curve) >= -1e-12).all()                          # non-decreasing
    assert result["p_hit_25"] >= result["p_hit_50"] >= result["p_hit_100"]
    assert result["median_days_25"] <= result["median_days_50"]
    policies = result["policies"]
    assert {"hold", "close_25", "close_50", "time_stop_21", "close_50_or_21dte"} <= set(policies)
    assert policies["close_50"]["days"] < policies["hold"]["days"] == pytest.approx(30)
    from analytics import costs
    gain = 0.5 * 1.0 * 100 - costs.option_open(1, "sell").total - costs.option_close(1, "buy").total
    assert policies["close_50"]["net_gain_when_hit"] == pytest.approx(gain)
    assert policies["close_25"]["below_min_gain"] == (0.25 * 100 - 1.13 < cfg.min_net_gain)


def test_short_dated_trades_have_no_time_stop_and_small_targets_are_marked():
    result, _ = _run(_csp(credit=0.10), dte=7)
    assert "time_stop_21" not in result["policies"]
    assert result["policies"]["close_25"]["below_min_gain"]              # $2.50 minus fees


def test_spread_reports_max_loss_and_roll_trigger():
    result, _ = _run(_pcs())
    assert 0 < result["p_max_loss"] <= result["p_short_itm"] <= result["p_touch_short"]
    assert 0 <= result["p_roll_trigger"] <= 1
    assert "p_assign" not in result


def test_earnings_crush_helps_a_short_premium_position():
    base, _ = _run(_csp(iv=0.40, credit=2.0), vol=0.40)
    crushed, _ = _run(_csp(iv=0.40, credit=2.0), vol=0.40, event_day=5)
    assert crushed["p_hit_50"] > base["p_hit_50"]


def test_blend_renormalises_over_present_models():
    cfg = _cfg()
    a = {"pop": 0.8, "policies": {"hold": {"ev": 10.0, "p_profit": 0.8, "days": 30,
                                           "annualised": 0.1, "ev_per_day_bpr": 0.001}}}
    b = {"pop": 0.6, "policies": {"hold": {"ev": -10.0, "p_profit": 0.6, "days": 30,
                                           "annualised": -0.1, "ev_per_day_bpr": -0.001}}}
    out = pe.blend({"G": a, "H": b}, cfg)                               # T fell back
    assert out["weights"] == pytest.approx({"G": 0.5, "H": 0.5})
    assert out["pop"] == pytest.approx(0.7) and out["policies"]["hold"]["ev"] == pytest.approx(0)


def test_headline_policy():
    cfg = _cfg()
    policies = {"hold": {}, "close_25": {}, "close_50": {}, "close_50_or_21dte": {}}
    assert pe.headline_policy(policies, 7, cfg) == "hold"
    assert pe.headline_policy(policies, 35, cfg) == "close_50"
    assert pe.headline_policy({"hold": {}, "close_30": {}}, 35, cfg) == "close_30"
    assert pe.headline_policy(policies, 35, _cfg(headline_policy="hold")) == "hold"


# --- The sheet driver on real bars ------------------------------------------------------------

def _row(**kw):
    base = {"ticker": "SPY", "strategy": "csp", "expiration": "2026-11-20", "strike": 700.0,
            "spot": 770.0, "dte_calendar": 30, "dte_trading": 21, "modelled_fill": 3.0,
            "implied_vol": 0.18, "contracts": 1, "collateral": 70_000.0, "accepted": True,
            "rejections": (), "trade_id": "csp|SPY|2026-11-20|700", "settlement": "physical"}
    base.update(kw)
    return base


def test_run_sheet_adds_blended_columns_and_ranks(monkeypatch):
    sheet = pd.DataFrame([
        _row(),
        _row(strategy="pcs", strike=740.0, long_strike=730.0, long_iv=0.2, modelled_fill=1.2,
             collateral=880.0, trade_id="pcs|SPY|2026-11-20|740|730"),
        _row(strike=760.0, modelled_fill=8.0, accepted=False, rejections=("x",),
             trade_id="csp|SPY|2026-11-20|760")])
    cfg = _cfg(n_paths=2000)
    out = probabilities.run_sheet(sheet, ScanRequest.default(), cfg=cfg,
                                  today=dt.date(2026, 10, 21))
    frame = out["sheet"]
    for column in ("pop_blend", "p_hit_50_blend", "headline_policy", "ev_per_day_bpr",
                   "pop_G", "pop_H", "effective_n_H", "prob_labels"):
        assert column in frame
    assert frame["accepted"].tolist()[-1] is False or frame.iloc[-1]["accepted"] == False  # noqa: E712
    assert set(out["policies"]["model"]) >= {"G", "H", "blend"}
    assert set(out["curves"]["target"]) == {25, 30, 50, 100}
    min_pop = ScanRequest.from_dict({"risk_mode": "min_pop", "min_pop": 0.999})
    gated = probabilities.run_sheet(sheet.iloc[:1], min_pop, cfg=cfg,
                                    today=dt.date(2026, 10, 21))["sheet"]
    assert not gated["accepted"].iloc[0] and "blended P(profit)" in gated["rejections"].iloc[0][-1]


def test_prob_tables_round_trip(tmp_path, monkeypatch):
    from pipeline import results
    monkeypatch.setattr(results, "runs_dir", lambda: tmp_path)
    run_id = "20260927-130000-prob"
    (tmp_path / run_id).mkdir()
    (tmp_path / run_id / "manifest.json").write_text(json.dumps({
        "run_id": run_id, "started_at": "x", "session_block": "x", "session_state": "closed",
        "tickers": 1, "finished_at": "2026-09-27T13:00:00", "stages": {"analyse": {}},
        "warnings": [], "banner": ""}), encoding="utf-8")
    policies = pd.DataFrame({"trade_id": ["a"], "model": ["blend"], "policy": ["hold"],
                             "ev": [1.0]})
    results.write_tables(run_id, pd.DataFrame({"ticker": ["A"], "expiration": ["2026-11-20"],
                                               "strike": [1.0]}), [], policies=policies,
                         metrics=policies.rename(columns={"policy": "metric"}),
                         curves=pd.DataFrame({"trade_id": ["a"], "prob": [0.5]}))
    loaded = results.load_run(run_id)
    assert loaded.prob_policies["policy"].tolist() == ["hold"]
    assert len(loaded.prob_curves) == 1


# --- Validation helpers -------------------------------------------------------------------------

def test_validation_strike_for_delta_and_observation():
    sys.path.insert(0, str(ROOT / "scripts"))
    import validate_prob_engine as v
    from analytics.options_math import bs_price_greeks
    strike = v.strike_for_delta(100.0, 0.25, 30, 0.045, -0.25)
    assert bs_price_greeks(100.0, strike, 30, 0.25, 0.045, "put").delta == pytest.approx(-0.25, abs=1e-6)
    flat = np.full(21, 100.0)
    obs = v.observe(flat, strike, 1.0, 0.25, 30, 0.045)
    assert obs["obs_hit_100"] == 1.0 and obs["obs_touch"] == 0.0 and obs["obs_hit_50"] == 1.0
    crash = np.linspace(99, 80, 21)
    obs = v.observe(crash, strike, 1.0, 0.25, 30, 0.045)
    assert obs["obs_hit_100"] == 0.0 and obs["obs_touch"] == 1.0
