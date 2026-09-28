"""
Phase 14: the Screener page, the Trade Detail page and its data module,
scan-request presets, Decisions fed from the Screener, and the calibration of
the underlying-ranking weights.
"""
from __future__ import annotations

import datetime as dt
import io
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics import rank_calibration as rc  # noqa: E402
from analytics import trade_detail as td  # noqa: E402
from analytics.strategies.base import Leg, Position  # noqa: E402

PCS_RUN, PCS_TRADE = "20260927-211850-cc01", "pcs|SPY|2026-10-30|748|738"
CSP_RUN, CSP_TRADE = "20260927-211952-9bf3", "csp|BMY|2026-10-02|61"


def _run_on_disk(run_id: str) -> bool:
    from core.paths import runs_dir
    return (runs_dir() / run_id / "candidates.parquet").exists()


def _sheet() -> pd.DataFrame:
    """Two tickers, CSP and PCS rows, as a persisted run stores them (missing
    text arrives as the string "nan")."""
    base = {"expiration": pd.Timestamp("2026-10-30"), "dte_calendar": 33, "dte_trading": 23,
            "spot": 100.0, "implied_vol": 0.25, "long_iv": 0.28, "ivr": 0.4,
            "iv_rv_ratio": 1.3, "short_distance_em": -1.0, "fillability": 0.7,
            "support_level_id": "50D SMA", "support_level": 96.0, "support_status": "ok",
            "strong_support_id": "nan", "strong_support_level": np.nan,
            "rejections": np.array([], dtype=object), "events": np.array([], dtype=object)}
    rows = [
        {**base, "trade_id": "pcs|AAA|2026-10-30|95|90", "ticker": "AAA", "strategy": "pcs",
         "strike": 95.0, "long_strike": 90.0, "width": 5.0, "modelled_fill": 1.20,
         "net_mid": 1.25, "contracts": 2, "collateral": 760.0, "max_loss": 760.0,
         "return_on_risk": 0.316, "accepted": True, "proposed": True, "best_per_ticker": True,
         "pop_blend": 0.80, "p_hit_50_blend": 0.85, "ev_per_day_bpr": 0.003,
         "rank_key": 0.003, "strong_support_id": "21W SMA", "strong_support_level": 93.0,
         "events": np.array(["cpi 2026-10-14"], dtype=object)},
        {**base, "trade_id": "pcs|AAA|2026-10-30|94|89", "ticker": "AAA", "strategy": "pcs",
         "strike": 94.0, "long_strike": 89.0, "width": 5.0, "modelled_fill": 1.00,
         "net_mid": 1.02, "contracts": 2, "collateral": 800.0, "max_loss": 800.0,
         "return_on_risk": 0.25, "accepted": True, "proposed": False,
         "best_per_ticker": False, "pop_blend": 0.84, "p_hit_50_blend": 0.88,
         "ev_per_day_bpr": 0.002, "rank_key": 0.002},
        {**base, "trade_id": "csp|BBB|2026-10-30|90", "ticker": "BBB", "strategy": "csp",
         "strike": 90.0, "long_strike": np.nan, "width": np.nan, "modelled_fill": 0.80,
         "net_mid": np.nan, "mid": 0.85, "contracts": 1, "collateral": 9000.0,
         "max_loss": np.nan, "return_on_risk": np.nan, "accepted": True, "proposed": True,
         "best_per_ticker": True, "pop_blend": 0.90, "p_hit_50_blend": 0.93,
         "ev_per_day_bpr": 0.0005, "rank_key": 0.0005},
        {**base, "trade_id": "csp|BBB|2026-10-30|98", "ticker": "BBB", "strategy": "csp",
         "strike": 98.0, "modelled_fill": 2.0, "contracts": 0, "collateral": 9800.0,
         "accepted": False, "proposed": False, "best_per_ticker": False, "pop_blend": 0.6,
         "ev_per_day_bpr": 0.01, "rank_key": -1e9,
         "rejections": np.array(["delta outside band"], dtype=object)},
    ]
    return pd.DataFrame(rows)


class _Results:
    def __init__(self, sheet):
        self.candidates = sheet


# --- Screener grid -----------------------------------------------------------------

def test_screener_grid_derives_the_missing_columns():
    grid = td.screener_grid(_sheet())
    assert list(grid.columns) == td.GRID_COLUMNS
    csp = grid[grid["trade_id"] == "csp|BBB|2026-10-30|90"].iloc[0]
    assert csp["net_mid"] == pytest.approx(0.85)                    # the short leg's mid
    assert csp["max_loss"] == pytest.approx((90 - 0.80) * 100)
    assert csp["return_on_risk"] == pytest.approx(0.80 / (90 - 0.80))
    rejected = grid[grid["trade_id"] == "csp|BBB|2026-10-30|98"].iloc[0]
    assert rejected["max_loss"] == pytest.approx((98 - 2.0) * 100)  # 0 contracts -> per 1
    assert rejected["why_not"] == "delta outside band"
    pcs = grid[grid["trade_id"] == "pcs|AAA|2026-10-30|95|90"].iloc[0]
    assert pcs["support"] == "21W SMA $93.00 (strong)"
    assert pcs["event_flags"] == "cpi 2026-10-14"
    other = grid[grid["trade_id"] == "pcs|AAA|2026-10-30|94|89"].iloc[0]
    assert other["support"] == "50D SMA $96.00 (ok)"                # "nan" text is missing


def test_filter_grid_rejected_last_best_per_ticker_and_grouping():
    grid = td.screener_grid(_sheet())
    everything = td.filter_grid(grid, accepted_only=False)
    assert everything.iloc[-1]["trade_id"] == "csp|BBB|2026-10-30|98"   # rejected last
    assert everything.iloc[0]["trade_id"] == "pcs|AAA|2026-10-30|95|90"
    best = td.filter_grid(grid, best_per_ticker=True)
    assert sorted(best["trade_id"]) == ["csp|BBB|2026-10-30|90", "pcs|AAA|2026-10-30|95|90"]
    grouped = td.filter_grid(grid, group_by_ticker=True)
    assert list(grouped["ticker"]) == ["AAA", "AAA", "BBB"]
    assert list(td.filter_grid(grid, min_pop=0.85)["ticker"]) == ["BBB"]
    assert td.filter_grid(grid, strategies=["pcs"])["strategy"].eq("pcs").all()
    assert td.filter_grid(grid, proposed_only=True)["proposed"].all()


def test_export_excel_round_trips():
    grid = td.screener_grid(_sheet()).drop(columns="trade_id")
    back = pd.read_excel(io.BytesIO(td.export_excel(grid)))
    assert len(back) == len(grid) and "ticker" in back


# --- Row, thesis, plan ------------------------------------------------------------

def test_record_is_plain_python_and_default_trade_is_top_accepted():
    results = _Results(_sheet())
    rec = td.record(results, "pcs|AAA|2026-10-30|95|90")
    assert rec["events"] == ["cpi 2026-10-14"] and isinstance(rec["contracts"], int)
    assert rec["expiration"] == "2026-10-30"
    other = td.record(results, "pcs|AAA|2026-10-30|94|89")
    assert other["strong_support_id"] is None and other["strong_support_level"] is None
    assert td.record(results, "nope") is None
    assert td.default_trade(results) == "pcs|AAA|2026-10-30|95|90"   # not the rejected 0.01


def test_verdicts_and_flags():
    results = _Results(_sheet())
    rejected = td.record(results, "csp|BBB|2026-10-30|98")
    assert td.verdict(rejected)[0] == "error" and "delta outside band" in td.verdict(rejected)[1]
    proposed = td.record(results, "csp|BBB|2026-10-30|90")
    assert td.verdict(proposed)[0] == "ok"
    losing = {**proposed, "headline_ev": -5.0}
    assert td.verdict(losing)[0] == "warn"
    flags = td.risk_flags({"events": ["fomc 2026-10-28"], "warnings": ["fomc 2026-10-28"],
                           "p_touch_blend": 0.5})
    assert sum("fomc" in f for f in flags) == 1                      # events shown once
    assert any("touched" in f for f in flags)
    thesis = td.thesis(td.record(results, "pcs|AAA|2026-10-30|95|90"))
    assert "Sell the $95 put and buy the $90 put" in thesis["text"]
    assert any("21W SMA" in w for w in thesis["why_strike"])
    assert "effective $89.20" in td.thesis(proposed)["text"]         # strike - credit


def test_management_plan_time_stop_only_above_21_dte():
    results = _Results(_sheet())
    pcs = {**td.record(results, "pcs|AAA|2026-10-30|95|90"), "headline_policy": "close_50"}
    plan = {p["rule"]: p["detail"] for p in td.management_plan(pcs)}
    assert "buy back at about $0.60" in plan["Profit target"]
    assert plan["Time stop"].startswith(("close at 21 DTE", "none"))  # Phase 15 spread rule
    assert "2x the credit" in plan["Loss stop"]
    assert "Max loss" in plan
    weekly = {**td.record(results, "csp|BBB|2026-10-30|90"), "dte_calendar": 5,
              "headline_policy": "hold"}
    plan = {p["rule"]: p["detail"] for p in td.management_plan(weekly)}
    assert plan["Profit target"].startswith("hold to expiry")
    assert plan["Time stop"].startswith("none")
    assert "Assignment" in plan


# --- Payoff, Greeks, scenarios, distributions ----------------------------------------

def _pcs(credit=1.2):
    return Position("pcs", "T", [Leg("put", "short", 95.0, "x", iv=0.25),
                                 Leg("put", "long", 90.0, "x", iv=0.28)], credit)


def test_payoff_frame_matches_position_payoff_and_t_plus_n_is_between():
    position = _pcs()
    frame = td.payoff_frame(position, 100.0, 30, contracts=2, em=6.0, days=[0, 15])
    expiry = frame[frame["curve"] == "expiry"]
    assert np.allclose(expiry["pnl"], position.payoff(expiry["price"].to_numpy()) * 200)
    assert expiry["pnl"].min() == pytest.approx(-(5 - 1.2) * 200)
    assert expiry["pnl"].max() == pytest.approx(1.2 * 200)
    t0 = frame[frame["curve"] == "T+0"]
    assert set(frame["curve"]) == {"expiry", "T+0", "T+15"}
    assert (t0["pnl"] >= expiry["pnl"].min() - 1e-6).all()
    assert (t0["pnl"] <= expiry["pnl"].max() + 1e-6).all()


def test_greeks_signs_and_scenario_grid():
    position = _pcs()
    g = td.leg_greeks(position, 100.0, 30)
    assert g["delta"] > 0 and g["theta"] > 0 and g["vega"] < 0      # short put spread
    frame = td.greeks_frame(position, 100.0, 30, contracts=3)
    assert len(frame) == 30 and frame.loc[0, "delta"] == pytest.approx(3 * g["delta"])
    csp = Position("csp", "T", [Leg("put", "short", 95.0, "x", iv=0.25)], 1.0)
    from analytics.options_math import bs_price_greeks
    put = bs_price_greeks(100.0, 95.0, 30, 0.25, td._rate(), "put")
    assert td.leg_greeks(csp, 100.0, 30)["delta"] == pytest.approx(-100 * put.delta)
    grid = td.scenario_grid(position, 100.0, 30, day=29)
    flat = grid[grid["iv_shift"] == 0.0].sort_values("move")
    assert flat["pnl"].is_monotonic_increasing                        # long delta
    assert len(grid) == 11 * 5


def test_em_cone_scales_with_sqrt_time():
    cone = td.em_cone(100.0, dt.date(2026, 9, 25), 16, em_iv=8.0, em_straddle=None)
    assert cone["iv_upper"].iloc[-1] == pytest.approx(108.0)
    assert cone["iv_lower"].iloc[4] == pytest.approx(100 - 8.0 * 0.5)
    assert "straddle_upper" not in cone


def test_terminal_distributions_on_real_bars():
    from data_sources.yfinance_sync import load_daily
    daily = load_daily("SPY", basis="price")
    if daily.empty:
        pytest.skip("no SPY bars on disk")
    dists = td.terminal_distributions(daily, 100.0, 0.20, 21, 30, n_paths=20_000)
    assert {"empirical", "lognormal"} <= set(dists)
    years = 30 / 365
    assert dists["lognormal"].mean() == pytest.approx(100 * np.exp(0.045 * years), rel=0.01)
    table = td.distribution_table(dists, {"K": 95.0, "none": None})
    assert list(table["level"]) == ["K"]
    assert 0 < table.loc[0, "p_below_lognormal"] < 0.5


def test_chain_window_flags_the_legs():
    chain = pd.DataFrame({"expiration": ["2026-10-30"] * 30 + ["2026-11-20"] * 2,
                          "strike_price": list(range(80, 110)) + [95, 90],
                          "put_bid": 1.0, "put_ask": 1.2, "put_iv": 0.25, "put_delta": -0.2,
                          "put_open_interest": 100, "put_volume": 5})
    window = td.chain_window(chain, "2026-10-30", [95.0, 90.0], n_each_side=3)
    assert window["strike"].min() == 87 and window["strike"].max() == 98
    assert window.loc[window["strike"] == 95, "leg"].item() == "short"
    assert window.loc[window["strike"] == 90, "leg"].item() == "long"
    assert window["width"].iloc[0] == pytest.approx(0.2)
    assert td.chain_window(pd.DataFrame(), "2026-10-30", [95.0]).empty


def test_trade_events_hide_past_macro_dates_and_levels_dedupe():
    events = pd.DataFrame({"symbol": ["AAA", "*", "*", "BBB", "*"],
                           "date": ["2026-08-01", "2026-08-15", "2026-10-14", "2026-10-01",
                                    "2026-10-16"],
                           "type": ["earnings", "cpi", "cpi", "earnings", "opex"]})
    out = td.trade_events("AAA", "2026-07-01", "2026-10-30", events,
                          market_from="2026-09-25")
    assert [label for _, label in out] == ["Earnings", "CPI"]
    support = pd.DataFrame({"level_id": ["a", "b", "c", "d"], "level": [99.0, 98.9, 97.0, 80.0],
                            "strong": [False, False, True, False]})
    levels = td.nearby_levels(support, 100.0, [95.0])
    assert list(levels["level_id"]) == ["c", "a"]         # b is within 0.5% of a; d out of band


def test_detail_charts_build():
    from app.components import charts
    position = _pcs()
    frame = td.payoff_frame(position, 100.0, 30, em=6.0)
    strikes = [("Short $95", 95.0, "short"), ("Long $90", 90.0, "long")]
    assert charts.payoff_chart(frame, 100.0, position.breakevens, strikes).data
    assert charts.scenario_heatmap(td.scenario_grid(position, 100.0, 30)).data
    assert charts.greeks_time_chart(td.greeks_frame(position, 100.0, 30)).data
    curves = pd.DataFrame({"model": ["G", "H", "G"], "target": [50, 50, 100],
                           "day": [1.0, 1.0, 30.0], "prob": [0.2, 0.3, 0.8]})
    fig = charts.prob_curves_chart(curves)
    assert len(fig.data) == 2                              # target 100 is a step: left out


# --- Scan presets -------------------------------------------------------------------

@pytest.fixture
def user_file(tmp_path, monkeypatch):
    from core import user_settings
    monkeypatch.setattr(user_settings, "config_dir", lambda: tmp_path)
    return tmp_path / user_settings.FILE


def test_scan_presets_save_load_delete(user_file):
    from analytics.scan_request import ScanRequest
    from core import user_settings as us
    request = ScanRequest.from_dict({"strategies": ["pcs"], "dte_min": 30, "dte_max": 45})
    assert us.save_scan_preset("PCS_Monthly", request.to_dict()) == "pcs_monthly"
    stored = us.scan_presets()["pcs_monthly"]
    again = ScanRequest.from_dict(stored)
    assert again.strategies == ["pcs"] and again.dte_window() == (30, 45)
    assert stored["name"] == "pcs_monthly"
    with pytest.raises(us.SettingsError):
        us.save_scan_preset("bad", {"strategies": ["iron_fly"]})
    assert us.delete_scan_preset("pcs_monthly") and not us.scan_presets()
    assert "pcs_30_45" in us.example_requests()


# --- Ranking calibration ---------------------------------------------------------------

def test_information_coefficient_and_suggested_weights():
    rng = np.random.default_rng(1)
    rows = []
    for d in range(30):
        outcome = rng.normal(size=20)
        for i in range(20):
            rows.append({"date": d, "iv_rank": outcome[i], "trend": rng.normal(),
                         "drawdown": -outcome[i], "support": np.nan, "outcome": outcome[i]})
    ic = rc.information_coefficients(pd.DataFrame(rows), ["iv_rank", "trend", "drawdown",
                                                          "support"]).set_index("component")
    assert ic.loc["iv_rank", "mean_ic"] == pytest.approx(1.0)
    assert ic.loc["drawdown", "mean_ic"] == pytest.approx(-1.0)
    assert abs(ic.loc["trend", "mean_ic"]) < 0.15
    assert ic.loc["support", "dates"] == 0
    current = {"iv_rank": 0.25, "iv_rv": 0.2, "liquidity": 0.2, "trend": 0.15,
               "support": 0.1, "drawdown": 0.1}
    weights = rc.suggested_weights(ic.reset_index(), current)
    assert weights["iv_rank"] == pytest.approx(0.6) and weights["trend"] == 0.0
    assert weights["iv_rv"] == pytest.approx(0.2) and sum(weights.values()) == pytest.approx(1.0)


def test_symbol_panel_is_point_in_time_on_real_bars():
    from data_sources.yfinance_sync import load_daily
    daily = load_daily("SPY", basis="price")
    if len(daily) < 2000:
        pytest.skip("no SPY history on disk")
    s = rc.Settings(horizon=21, years=3)
    entries = rc.entry_calendar(daily, s)
    panel = rc.symbol_panel("SPY", daily, s, entries)
    assert len(panel) >= 30 and set(panel["date"]) <= entries
    for column in rc.TESTABLE:
        values = panel[column].dropna()
        assert ((values >= 0) & (values <= 1)).all()
    # the outcome only uses the close `horizon` sessions after entry
    truncated = daily[pd.to_datetime(daily["date"]) <= panel["date"].iloc[0]
                      + pd.Timedelta(days=40)]
    first = rc.symbol_panel("SPY", truncated, rc.Settings(horizon=21, years=30), entries)
    if not first.empty:
        row = first[first["date"] == panel["date"].iloc[0]]
        if not row.empty:
            assert row["outcome"].item() == pytest.approx(panel["outcome"].iloc[0])


def test_calibrated_preset_ships_and_is_the_default():
    # Phase 17 (review B.2, Tom 2026-09-28): `calibrated` became the default.
    from core import user_settings as us
    presets = us.weight_presets()
    assert "calibrated" in presets and us.default_weight_preset() == "calibrated"
    assert presets["calibrated"]["trend"] == 0.0 and presets["calibrated"]["support"] == 0.0
    assert sum(presets["calibrated"].values()) == pytest.approx(1.0)


# --- Pages ------------------------------------------------------------------------------

def _app(page: str):
    from streamlit.testing.v1 import AppTest
    return AppTest.from_file(str(ROOT / "app" / "pages" / page), default_timeout=300)


@pytest.mark.parametrize("run_id,trade_id", [(PCS_RUN, PCS_TRADE), (CSP_RUN, CSP_TRADE)])
def test_trade_detail_fully_populated(run_id, trade_id):
    if not _run_on_disk(run_id):
        pytest.skip(f"run {run_id} not on disk")
    at = _app("9_Trade_Detail.py")
    at.query_params["run"] = run_id
    at.query_params["trade"] = trade_id
    at.run()
    assert not at.exception, [e.message for e in at.exception]
    assert len(at.tabs) == 10
    assert len(at.get("plotly_chart")) == 7
    assert trade_id.split("|")[1] in at.title[0].value


def test_screener_page_runs_and_lists_the_run():
    at = _app("8_Screener.py")
    at.run()
    assert not at.exception, [e.message for e in at.exception]
    assert at.title[0].value == "Screener"
    at.radio(key=[w.key for w in at.radio if w.key and w.key.endswith("strat")][0]).set_value("Both").run()
    assert not at.exception
    # Phase 17: no request until an account profile is chosen explicitly.
    assert any("choose an account profile" in e.value for e in at.error)
    at.selectbox(key=[w.key for w in at.selectbox
                      if w.key and w.key.endswith("profile")][0]).set_value("default").run()
    assert not at.exception
    assert any("CSP + PCS" in c.value or "csp" in c.value.lower() for c in at.caption)


def test_decisions_shows_the_screener_selection_first():
    if not _run_on_disk(PCS_RUN):
        pytest.skip("run not on disk")
    at = _app("1_Decisions.py")
    at.session_state["screener_selection"] = {"run": PCS_RUN, "trade": PCS_TRADE}
    at.run()
    assert not at.exception, [e.message for e in at.exception]
    assert "Selected on the Screener" in at.info[0].value
