"""Phase 17 -- live shakedown and open decisions.

IV-percentile regime (clamp, hysteresis, soft gate, extremes), account
profiles, gaps on daily bars, the index-ETF RV floor, loss-stop policies in
the probability engine (intraday triggers, spot-vol IV), the spread liquidity
fallbacks (monthly expirations, OI-aware long leg, spread-leg floors) and the
targeted PMCC chain widening.
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest

from analytics import prob_engine as pe
from analytics import sizing, strategy_spec as ss
from analytics.strategies.base import Leg, Position


# --- IV regime ----------------------------------------------------------------------------

def test_regime_clamps_and_uses_percentile():
    assert ss.clamp_rank(1.11) == 1.0 and ss.clamp_rank(-0.007) == 0.0
    assert ss.clamp_rank(None) is None and ss.clamp_rank(float("nan")) is None
    value, measure = ss.regime_value({"ivr": 1.11, "ivp": 0.40})
    assert (value, measure) == (0.40, "ivp")
    value, measure = ss.regime_value({"ivr": 1.11, "ivp": None})
    assert (value, measure) == (1.0, "ivr")              # falls back to the clamped rank
    assert ss.iv_regime(1.11) == "high" and ss.iv_regime(-0.2) == "low"


def test_regime_hysteresis():
    assert ss.iv_regime(0.26) == "mid"
    assert ss.iv_regime(0.26, previous="low") == "low"   # not cleared by 0.03
    assert ss.iv_regime(0.29, previous="low") == "mid"
    assert ss.iv_regime(0.24, previous="mid") == "mid"
    assert ss.iv_regime(0.21, previous="mid") == "low"
    assert ss.iv_regime(0.48, previous="high") == "high"
    assert ss.iv_regime(0.52, previous="mid") == "mid"
    assert ss.iv_regime(0.60, previous="mid") == "high"


def test_regime_is_soft_except_at_the_extremes():
    specs = ss.load_all()
    condor, calendar = specs["iron_condor"], specs["call_calendar"]
    assert condor.premium == "credit" and calendar.premium == "debit"
    assert specs["pmcc"].premium == "debit"
    ok, why = ss.applies(condor, {"iv_regime": "low", "iv_value": 0.20, "trend": "range"})
    assert ok and "soft" in why[0]
    assert ss.regime_fit(condor, "low") is False and ss.regime_fit(condor, "high") is True
    ok, why = ss.applies(condor, {"iv_regime": "low", "iv_value": 0.05, "trend": "range"})
    assert not ok and any("too cheap to sell" in w for w in why)
    ok, why = ss.applies(calendar, {"iv_regime": "high", "iv_value": 0.95, "trend": "range"})
    assert not ok and any("too rich to buy" in w for w in why)
    ok, _ = ss.applies(calendar, {"iv_regime": "high", "iv_value": 0.70, "trend": "range"})
    assert ok


def test_spec_premium_is_validated():
    base = {"id": "x", "label": "x", "margin_class": "defined_risk",
            "expirations": {"front": {"dte_target": 30}},
            "legs": [{"name": "s", "type": "put", "side": "short", "expiration": "front",
                      "select": {"delta": -0.2}}]}
    assert ss.from_dict(base).premium == "credit"
    with pytest.raises(ss.SpecError):
        ss.from_dict({**base, "premium": "free"})


def test_underlying_rank_clamps_iv_rank():
    from analytics.underlying_rank import score_iv_rank
    assert score_iv_rank(1.11, 0.9) == pytest.approx(0.95)
    assert score_iv_rank(-0.007, None) == 0.0


# --- Profiles and B.2 --------------------------------------------------------------------

def test_profiles_have_placeholders_and_labels():
    from core import user_settings as us
    names = us.profile_names()
    assert {"roth_ira", "traditional_ira", "taxable"} <= set(names)
    taxable = sizing.account_config("taxable")
    assert taxable["naked_approval"] and taxable["naked_research_only"]
    assert taxable["account_type"] == "margin"
    assert not sizing.account_config("roth_ira")["naked_approval"]
    assert "PLACEHOLDER" in us.profile_label("roth_ira")
    assert "PLACEHOLDER" not in us.profile_label("default")
    assert us.validate_profile({"placeholder": 0, "naked_research_only": 1}) == \
        {"placeholder": False, "naked_research_only": True}


def test_broad_index_rv_floor():
    from analytics.universe_screen import classify
    from core.paths import load_config
    thr = load_config()["stage1_thresholds"]
    m = {"days_since_last": 1, "adv_90d_dollars": 1e10, "last_price": 500.0,
         "rv_20d_annualized": 0.10, "max_drawdown": -0.3, "pct_off_peak_now": -0.01,
         "history_years": 20}
    assert "volatility_out_of_range" in classify(m, thr)[1]
    assert classify(m, thr, "broad_index_us")[0] == "tier1_backtestable"


def test_default_request_still_builds():
    from analytics.scan_request import ScanRequest
    request = ScanRequest.default()
    assert request.ranking_weights == "calibrated"


# --- Gaps on daily bars -------------------------------------------------------------------

def test_gaps_use_daily_bars():
    from analytics import gaps
    from core.freshness import last_completed_session
    frame = gaps.session_frame("SPY", years=2)
    if frame.empty:
        pytest.skip("no SPY daily bars")
    assert frame.attrs["source"] == "daily"
    assert frame["date"].max().date() <= last_completed_session()
    profile = gaps.profile("SPY", years=5)
    assert profile is not None and profile.source.startswith("daily bars")
    assert profile.excluded_days == 0


# --- Engine: stops, intraday triggers, spot-vol ------------------------------------------

def test_managed_policy_name():
    name = pe.managed_policy_name
    assert name({"target": 50, "stop": 2.0, "time_stop": 21}, 45) == "close_50_stop_2x_or_21dte"
    assert name({"target": 50, "stop": 2.0, "time_stop": 21}, 14) == "close_50_stop_2x"
    assert name({"target": None, "stop": 2.0, "time_stop": 21}, 7) == "stop_2x"
    assert name({"target": 25, "stop": 0.5, "time_stop": None}, 14) == "close_25_stop_0.5x"
    assert name({"target": None, "stop": None, "time_stop": 21}, 45) == "time_stop_21"
    assert name(None, 45) is None


def test_bootstrap_extremes_share_the_draws():
    returns = np.linspace(-0.02, 0.02, 200)
    lo, hi = returns - 0.01, returns + 0.01
    closes = pe.bootstrap(returns, np.arange(0, 190), 50, 12, 5, np.random.default_rng(1))
    c2, low, high = pe.bootstrap(returns, np.arange(0, 190), 50, 12, 5,
                                 np.random.default_rng(1), (lo, hi))
    assert np.allclose(closes, c2)
    assert (low <= c2 + 1e-12).all() and (high >= c2 - 1e-12).all()


def test_bridge_extremes_bracket_both_closes():
    rng = np.random.default_rng(0)
    closes = np.cumsum(rng.normal(0, 0.01, (500, 20)), axis=1)
    low, high = pe.bridge_extremes(closes, 0.01, np.random.default_rng(1))
    prior = np.concatenate([np.zeros((500, 1)), closes[:, :-1]], axis=1)
    assert (low <= np.minimum(prior, closes) + 1e-12).all()
    assert (high >= np.maximum(prior, closes) - 1e-12).all()


def _spread_spec(**kw) -> pe.TradeSpec:
    exp = dt.date(2026, 11, 13)
    legs = [Leg("put", "short", 95.0, exp, iv=0.25), Leg("put", "long", 90.0, exp, iv=0.27)]
    position = Position("pcs", "SYN", legs, 1.20)
    return pe.TradeSpec(position=position, spot=100.0, dte_calendar=45, dte_trading=31,
                        contracts=1, bpr=380.0, **kw)


def test_stop_policies_and_intraday_triggers():
    cfg = pe.EngineConfig()
    cfg.n_paths = 4000
    normals = pe.g_normals(cfg.n_paths, 31, np.random.default_rng(3))
    g = pe.g_log_returns(normals, 0.30, cfg.rate, 45 / 365)
    rules = {"target": 50, "stop": 2.0, "time_stop": 21}
    plain = pe.evaluate(_spread_spec(), g, [50], cfg)
    closes = pe.evaluate(_spread_spec(loss_stop_multiple=2.0, managed=rules), g, [50], cfg)
    low, high = pe.bridge_extremes(g, 0.30 * math.sqrt(45 / 365 / 31), np.random.default_rng(4))
    intra = pe.evaluate(_spread_spec(loss_stop_multiple=2.0, managed=rules), g, [50], cfg,
                        low, high)
    # Unstopped policies do not move when stops are added.
    for name in plain["policies"]:
        assert closes["policies"][name]["ev"] == pytest.approx(plain["policies"][name]["ev"])
    for name in ("stop_2x", "close_50_stop_2x", "close_50_stop_2x_or_21dte",
                 "stop_2x_or_21dte"):
        assert name in intra["policies"]
    assert intra["managed_policy"] == "close_50_stop_2x_or_21dte"
    # Intraday lows reach the stop at least as often as closes do.
    assert intra["policies"]["stop_2x"]["p_stopped"] >= closes["policies"]["stop_2x"]["p_stopped"]
    assert intra["policies"]["stop_2x"]["p_stopped"] > 0
    # A stop caps the loss near 2x credit (plus fees), unless the close gapped through.
    assert pe.headline_policy(intra["policies"], 45, _cfg_shipped(),
                              intra["managed_policy"]) == "close_50_stop_2x_or_21dte"


def _cfg_shipped():
    cfg = pe.EngineConfig()
    cfg.headline_policy = "shipped"
    return cfg


def test_spot_vol_raises_the_put_spread_mark_on_down_paths():
    cfg = pe.EngineConfig()
    down = np.tile(np.linspace(-0.005, -0.08, 31), (2, 1))
    sticky = pe.evaluate(_spread_spec(), down, [50], cfg)
    moving = pe.evaluate(_spread_spec(), down, [50], cfg, spot_vol_beta=-4.0)
    # Higher IV on the way down makes the short spread worth more to buy back
    # before expiry (a worse interim P&L); expiry is unchanged.
    assert moving["policies"]["time_stop_21"]["ev"] < sticky["policies"]["time_stop_21"]["ev"]
    assert moving["policies"]["hold"]["ev"] == pytest.approx(sticky["policies"]["hold"]["ev"])


def test_name_spot_vol_beta_is_scaled_down_from_the_index():
    from data_sources.yfinance_sync import load_daily
    cfg = pe.EngineConfig.from_config()
    index_beta = pe.index_spot_vol_beta(cfg)
    if index_beta is None:
        pytest.skip("no VIX reference data")
    assert -9 < index_beta < -2
    spy = load_daily("SPY", basis="price")
    assert pe.name_spot_vol_beta(spy, cfg, spy) == pytest.approx(index_beta, rel=1e-6)


def test_sheet_headline_follows_the_shipped_rules():
    from analytics.probabilities import shipped_rules
    assert shipped_rules({"strategy": "csp", "dte_calendar": 7}) is None
    rules = shipped_rules({"strategy": "pcs", "dte_calendar": 45})
    assert rules == {"target": 50, "stop": 2.0, "time_stop": 21}
    assert shipped_rules({"strategy": "pcs", "dte_calendar": 10})["target"] is None


# --- Spread liquidity fallbacks -----------------------------------------------------------

def test_monthly_expirations():
    from analytics.chain_utils import monthly_expirations
    chain = pd.DataFrame({"expiration": ["2026-11-06", "2026-11-20", "2026-12-18"],
                          "expiration_type": ["Weekly", "Regular", "Regular"]})
    assert monthly_expirations(chain) == {dt.date(2026, 11, 20), dt.date(2026, 12, 18)}
    bare = pd.DataFrame({"expiration": ["2026-11-06", "2026-11-20", "2026-11-19"]})
    assert monthly_expirations(bare) == {dt.date(2026, 11, 20), dt.date(2026, 11, 19)}


def test_spreads_also_build_at_the_nearest_monthly():
    from analytics.scan_request import ScanRequest
    request = ScanRequest.from_dict({"strategies": ["pcs"], "pcs_dte_targets": [45],
                                     "spread_width_pct": [4]})
    dtes = [32, 39, 53, 81]
    assert request.nearest_pcs_dtes(dtes) == {39}
    assert request.nearest_pcs_dtes(dtes, monthly_dtes={53, 81}) == {39, 53}
    assert request.nearest_pcs_dtes(dtes, monthly_dtes={39}) == {39}


def test_specs_take_the_monthly_for_long_dated_roles():
    from analytics.strategies.resolver import pick_expirations
    spec = ss.load_all()["bull_put"]
    today = dt.date(2026, 9, 28)
    dates = [dt.date(2026, 11, 6), dt.date(2026, 11, 13), dt.date(2026, 11, 20)]
    assert pick_expirations(dates, spec, today)["front"] == dt.date(2026, 11, 13)
    assert pick_expirations(dates, spec, today,
                            monthly={dt.date(2026, 11, 20)})["front"] == dt.date(2026, 11, 20)


def test_long_leg_snaps_to_open_interest():
    from analytics.strategies.pcs import snap_long
    puts = pd.DataFrame({"strike_price": [85.0, 88.0, 89.0, 90.0, 91.0, 95.0],
                         "put_open_interest": [900, 400, 300, 5, 20, 1000]})
    assert snap_long(puts, 95.0, 5.0) == 90.0
    # +/-25% of the $5 width around 90: 88.75-91.25; 91 is too thin, 89 clears
    assert snap_long(puts, 95.0, 5.0, min_oi=100, band=0.25) == 89.0
    assert snap_long(puts, 95.0, 5.0, min_oi=5000, band=0.25) == 90.0  # none clears: nearest


def test_spread_leg_floors():
    floors = sizing.spread_leg_floors()
    assert floors == {"min_open_interest": 100, "min_option_volume": 0}
    acct = sizing.AccountState(net_liquidating_value=100_000, cash_available=100_000)
    legs = [(150, 3, "short leg"), (400, 0, "long leg")]
    single = sizing.max_contracts_for_position(400.0, acct, legs=legs)
    spread = sizing.max_contracts_for_position(400.0, acct, legs=legs, leg_floors=floors)
    assert single.rejected                              # OI 150 < 250 and volume 3 < 25
    assert not spread.rejected and spread.contracts == 4   # 3% of OI 150 caps it


# --- Targeted PMCC widening ----------------------------------------------------------------

def test_strike_window_extra_call_band():
    from data_sources.chains import StrikeWindow
    window = StrikeWindow(spot=100.0, iv=0.30, extra_call_bands=((60, 120, 0.68, 0.92),))
    assert window.extra_bounds("call", 30) == [] and window.extra_bounds("put", 90) == []
    (lo, hi), = window.extra_bounds("call", 90)
    assert 75 < lo < hi < 95
    assert lo < window.bounds("call", 90)[0]            # below the default call window


def test_spec_widening_only_where_the_trend_holds():
    from analytics.scan_request import ScanRequest
    from data_sources.chains import spec_widening
    request = ScanRequest.from_dict({"strategies": ["pcs"], "recommend": True})
    out = spec_widening(request, ["UP", "FLAT", "NONE"], {"UP": "uptrend", "FLAT": "range"})
    assert set(out) == {"UP"}
    (dte_lo, dte_hi, d_lo, d_hi), = [b for b in out["UP"] if b[3] > 0.8]
    assert (dte_lo, dte_hi) == (60, 120) and d_lo == pytest.approx(0.68)
    plain = ScanRequest.from_dict({"strategies": ["pcs"]})
    assert spec_widening(plain, ["UP"], {"UP": "uptrend"}) == {}


def test_recommender_conditions_keep_the_iv_value():
    import datetime as _dt
    from analytics.recommender import _conditions
    latest = pd.DataFrame({"symbol": ["SYN"], "trend_state": ["range"]})
    cond = _conditions("SYN", _dt.date(2026, 9, 28), {"ivr": 1.11, "ivp": 0.62}, latest,
                       None, 45, previous={"ivp": 0.49})
    assert cond["iv_value"] == pytest.approx(0.62) and cond["trend"] == "range"
    assert cond["iv_regime_previous"] == "mid" and cond["iv_regime"] == "high"
