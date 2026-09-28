"""
Phase 11: scan request, underlying ranking, targeted chain capture, and the
end of the forced one-per-ticker sheet.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics import candidates, sizing, underlying_rank as ur  # noqa: E402
from analytics.scan_request import (RequestError, ScanRequest,  # noqa: E402
                                    resolve_universe, strategies_for)
from core.paths import load_config  # noqa: E402
from data_sources import chains, events  # noqa: E402

EXAMPLES = ROOT / "examples"


# --- ScanRequest -------------------------------------------------------------

def test_default_request_reproduces_the_config_entry_window():
    entry = load_config()["management"]["entry"]
    request = ScanRequest.default()
    assert request.strategies == ["csp"]
    assert request.dte_window() == (entry["dte_min"], entry["dte_max"])
    assert request.delta_range == sorted(entry["delta_band"])
    assert request.top_n_underlyings == 15
    assert request.profit_targets == [25, 30, 50, 100]


def test_request_round_trips_through_json():
    request = ScanRequest.from_dict({"strategies": ["csp", "pcs"], "dte_targets": [35, 21],
                                     "risk_mode": "min_pop", "min_pop": 0.8,
                                     "universe": ["spy", " qqq"]})
    again = ScanRequest.from_json(request.to_json())
    assert again == request
    assert again.dte_targets == [21, 35]              # sorted
    assert again.universe == ["SPY", "QQQ"]           # normalised


@pytest.mark.parametrize("bad, message", [
    ({"strategies": ["iron_condor"]}, "strategies"),
    ({"dte_min": 30, "dte_max": 20}, "dte_min"),
    ({"risk_mode": "min_pop"}, "min_pop"),
    ({"risk_mode": "max_pct_capital", "max_pct_capital": 1.5}, "max_pct_capital"),
    ({"account_profile": "nonexistent"}, "account_profile"),
    ({"universe": "sp500"}, "universe"),
    ({"surprise": 1}, "unknown request field"),
    ({"event_policy_overrides": {"fomc": {"action": "panic"}}}, "action"),
])
def test_invalid_requests_are_refused_with_a_reason(bad, message):
    with pytest.raises(RequestError, match=message):
        ScanRequest.from_dict(bad)


def test_delta_range_accepts_positive_magnitudes():
    request = ScanRequest.from_dict({"delta_range": [0.12, 0.30]})
    assert request.delta_range == [-0.30, -0.12]


def test_dte_targets_window_and_membership():
    tol = load_config()["scan_defaults"]["dte_target_tolerance_days"]
    request = ScanRequest.from_dict({"dte_targets": [21, 35]})
    assert request.dte_window() == (21 - tol, 35 + tol)
    assert request.accepts_dte(21 + tol) and request.accepts_dte(35 - tol)
    assert not request.accepts_dte(28)                # between the targets
    assert request.reference_dte() == 28.0


def test_chain_window_adds_the_roll_buffer():
    buffer = load_config()["chain_capture"]["roll_buffer_days"]
    request = ScanRequest.from_dict({"dte_min": 30, "dte_max": 45})
    assert request.chain_dte_window() == (30, 45 + buffer)


def test_example_requests_are_valid():
    files = sorted(EXAMPLES.glob("*.json"))
    assert any(f.name == "pcs_30_45.json" for f in files)
    for path in files:
        ScanRequest.load(path)                        # raises if invalid


def test_universe_scope_follows_the_strategies():
    csp_only = resolve_universe(ScanRequest.from_dict({"strategies": ["csp"]}))
    with_pcs = resolve_universe(ScanRequest.from_dict({"strategies": ["pcs"]}))
    assert "SPX" not in csp_only and "SPY" in csp_only
    assert {"SPX", "XSP", "NDX", "RUT"} <= set(with_pcs)
    assert strategies_for("cash", ScanRequest.from_dict({"strategies": ["csp", "pcs"]})) == ["pcs"]


# --- Component scores --------------------------------------------------------

def test_support_score_band():
    band = (0.5, 2.0)
    assert ur.score_support(1.0, band, studied=True) == 1.0
    assert ur.score_support(0.25, band, studied=True) == pytest.approx(0.5)
    assert ur.score_support(3.0, band, studied=True) == pytest.approx(0.5)
    assert ur.score_support(5.0, band, studied=True) == 0.0
    assert ur.score_support(None, band, studied=True) == 0.0     # no strong level
    assert ur.score_support(None, band, studied=False) is None   # never studied


def test_iv_rv_liquidity_trend_drawdown_scores():
    assert ur.score_iv_rv(0.8, 0.8, 2.0) == 0.0
    assert ur.score_iv_rv(1.4, 0.8, 2.0) == pytest.approx(0.5)
    assert ur.score_iv_rv(3.0, 0.8, 2.0) == 1.0
    assert ur.score_iv_rv(None, 0.8, 2.0) is None
    assert ur.score_liquidity(5, None) == 1.0
    assert ur.score_liquidity(None, 10 ** 3.75, (2.0, 5.5)) == pytest.approx(0.5)
    assert ur.score_liquidity(4, 10 ** 5.5, (2.0, 5.5)) == pytest.approx(0.9)
    assert ur.score_trend("uptrend", {"uptrend": 1.0}) == 1.0
    assert ur.score_trend(None, {"uptrend": 1.0}) is None
    assert ur.score_drawdown(-0.325, -0.65) == pytest.approx(0.5)
    assert ur.score_drawdown(-0.9, -0.65) == 0.0


def test_composite_renormalises_over_available_components():
    weights = {"iv_rank": 0.5, "iv_rv": 0.25, "liquidity": 0.25,
               "trend": 0.0, "support": 0.0, "drawdown": 0.0}
    score, coverage = ur.composite({"iv_rank": 1.0, "iv_rv": 0.0, "liquidity": None,
                                    "trend": None, "support": None, "drawdown": None},
                                   weights)
    assert score == pytest.approx(0.5 / 0.75)
    assert coverage == pytest.approx(0.75)
    assert ur.composite({k: None for k in ur.COMPONENTS}, weights) == (None, 0.0)


def test_expected_move_and_iv_near_dte():
    assert ur.expected_move(100.0, 0.20, 365.0) == pytest.approx(20.0)
    today = dt.date(2026, 9, 27)
    metrics = {"iv_index": 0.30, "expirations_json": json.dumps([
        {"expiration": "2026-10-09", "iv": 0.25},
        {"expiration": "2026-11-06", "iv": 0.22}])}
    assert ur.iv_near_dte(metrics, 12, today) == 0.25
    assert ur.iv_near_dte(metrics, 40, today) == 0.22
    assert ur.iv_near_dte({"iv_index": 0.30}, 40, today) == 0.30


# --- Gates -------------------------------------------------------------------

def _events(rows):
    frame = pd.DataFrame([{"symbol": s, "date": d, "type": t, "time_of_day": "bmo",
                           "confirmed": True, "amount": None, "source": "test",
                           "sources_disagree": False, "note": ""} for s, d, t in rows])
    return frame


def test_event_gate_blocked_partial_ok():
    today = dt.date(2026, 9, 28)
    request = ScanRequest.from_dict({"strategies": ["csp"], "dte_min": 30, "dte_max": 45})
    early = _events([("AAA", today + dt.timedelta(days=10), "earnings")])
    late = _events([("AAA", today + dt.timedelta(days=40), "earnings")])
    later = _events([("AAA", today + dt.timedelta(days=90), "earnings")])
    status = lambda frame: ur._event_status("AAA", ["csp"], "stock", request, today,  # noqa: E731
                                            frame, True)
    assert status(early)["event_status"] == "blocked"
    partial = status(late)
    assert partial["event_status"] == "partial" and "clear to ~39 DTE" in partial["event_notes"]
    assert status(later)["event_status"] == "ok"


def test_event_overrides_merge_over_policy():
    today = dt.date(2026, 9, 28)
    fomc = _events([("*", today + dt.timedelta(days=5), "fomc")])
    base = events.check("SPY", today, today + dt.timedelta(days=10), "csp",
                        asset_class="etf", frame=fomc)
    blocked = events.check("SPY", today, today + dt.timedelta(days=10), "csp",
                           asset_class="etf", frame=fomc,
                           overrides={"fomc": {"action": "block"}})
    assert base.action == "warn" and blocked.action == "block"


def test_capital_gate_follows_profile_and_risk_mode():
    request = ScanRequest.from_dict({"strategies": ["csp", "pcs"]})
    small = {"net_liquidating_value": 50_000, "max_collateral_per_position_pct": 0.10,
             "allowed_strategies": ["csp", "pcs"], "spread_approval": True}
    feasible, reasons = ur._capital(["csp", "pcs"], 600.0, 20.0, request, small)
    assert feasible == ["pcs"] and "csp" in reasons[0]
    no_spreads = dict(small, spread_approval=False)
    feasible, _ = ur._capital(["csp", "pcs"], 40.0, 2.0, request, no_spreads)
    assert feasible == ["csp"]
    tight = ScanRequest.from_dict({"strategies": ["pcs"], "risk_mode": "max_loss_per_trade",
                                   "max_loss_per_trade": 50, "spread_widths": [1, 5]})
    feasible, _ = ur._capital(["pcs"], 40.0, 2.0, tight, small)
    assert feasible == []                             # $1 width = $100 max loss > $50


# --- Ranking on real data ----------------------------------------------------

@pytest.fixture(scope="module")
def ranked():
    return ur.rank(ScanRequest.from_dict({"strategies": ["csp", "pcs"], "dte_min": 30,
                                          "dte_max": 45, "top_n_underlyings": 10}))


def test_rank_scores_every_symbol_with_its_breakdown(ranked):
    assert len(ranked) >= 50
    for column in [f"score_{c}" for c in ur.COMPONENTS] + ["score", "coverage", "exclusion"]:
        assert column in ranked
    eligible = ranked[ranked["eligible"]]
    assert eligible["score"].is_monotonic_decreasing
    assert eligible["rank"].tolist() == list(range(1, len(eligible) + 1))
    assert (ranked.loc[~ranked["eligible"], "exclusion"].str.len() > 0).all()
    assert ranked["selected"].sum() == min(10, len(eligible))
    assert not ranked.loc[ranked["selected"], "eligible"].eq(False).any()


def test_rank_puts_indices_in_scope_only_for_pcs(ranked):
    spx = ranked.set_index("symbol").loc["SPX"]
    assert spx["strategies"] in ("pcs", "")           # never csp


def test_chain_targets_add_held_names(ranked):
    top = ranked.loc[ranked["selected"], "symbol"].tolist()
    targets = ur.chain_targets(ranked, held={"ZZZZ"})
    assert targets[:len(top)] == top and targets[-1] == "ZZZZ"


# --- Strike filter and targeted capture --------------------------------------

class _StubClient:
    """Enough of tastytrade_common for _rows_for_chain."""

    @staticmethod
    def select_expirations(flat, tokens):
        return sorted({e["expiration-date"] for e in flat})

    @staticmethod
    def empty_strike_row(exp, strike, call, put):
        return {"expiration": exp, "strike_price": float(strike),
                "call_symbol": call, "put_symbol": put}


def _chain(today, dtes, strikes, root="XYZ"):
    exps = []
    for d in dtes:
        date = (today + dt.timedelta(days=d)).isoformat()
        exps.append({"expiration-date": date, "settlement-type": "PM",
                     "expiration-type": "Weekly",
                     "strikes": [{"strike-price": str(k), "call": f"C{date}{k}",
                                  "put": f"P{date}{k}"} for k in strikes]})
    return {"data": {"items": [{"root-symbol": root, "expirations": exps}]}}


def test_strike_window_bounds_scale_with_dte():
    window = chains.StrikeWindow(spot=100.0, iv=0.365, put_em=(-3.0, 0.5),
                                 call_em=(-0.5, 1.5), min_pct=0.03)
    lo, hi = window.bounds("put", 365)
    assert lo == pytest.approx(100 - 3 * 36.5) and hi == pytest.approx(100 + 0.5 * 36.5)
    lo, hi = window.bounds("put", 1)                   # tiny EM -> the % floor
    assert lo <= 97.0 and hi >= 103.0
    assert chains.StrikeWindow().bounds("put", 30) == (-float("inf"), float("inf"))


def test_rows_for_chain_filters_strikes_and_sides():
    today = dt.date(2026, 9, 28)
    window = chains.StrikeWindow(spot=100.0, iv=0.20, put_em=(-3.0, 0.5),
                                 call_em=(-0.5, 1.5), min_pct=0.03)
    rows, symbols, index, info = chains._rows_for_chain(
        _StubClient, _chain(today, [30], range(50, 151, 5)), "tok", window, today)
    strikes = sorted(r["strike_price"] for r in rows)
    em = 100 * 0.20 * np.sqrt(30 / 365)                # ~5.7
    assert min(strikes) >= 100 - 3 * em and max(strikes) <= 100 + 1.5 * em
    puts = [s for s in symbols if s.startswith("P")]
    calls = [s for s in symbols if s.startswith("C")]
    assert len(puts) > len(calls)                      # calls near the money only
    assert info["strikes_listed"] == 21
    assert rows[0]["root_symbol"] == "XYZ" and rows[0]["settlement_type"] == "PM"


def test_subscription_cap_drops_expirations_furthest_from_the_request():
    today = dt.date(2026, 9, 28)
    chain = _chain(today, [7, 35, 60], range(90, 111))   # 42 subscriptions per expiration
    date = lambda d: (today + dt.timedelta(days=d)).isoformat()  # noqa: E731
    window = chains.StrikeWindow(spot=100.0, iv=None, max_subscriptions=100,
                                 reference_dte=35)
    rows, _, _, info = chains._rows_for_chain(_StubClient, chain, "tok", window, today)
    assert {r["expiration"] for r in rows} == {date(35), date(60)}   # 7 DTE is furthest
    assert info["expirations_dropped"] == [date(7)]
    window.max_subscriptions = 50
    rows, symbols, _, info = chains._rows_for_chain(_StubClient, chain, "tok", window, today)
    assert {r["expiration"] for r in rows} == {date(35)} and len(symbols) <= 50


def test_needs_capture_when_the_block_snapshot_misses_the_window(tmp_path, monkeypatch):
    monkeypatch.setattr(chains, "chains_dir", lambda: tmp_path)
    sunday = dt.datetime(2026, 9, 27, 12, 0)
    block = chains.session_block(sunday)
    (tmp_path / block).mkdir()
    pd.DataFrame({"expiration": ["2026-10-02"], "strike_price": [100.0]}).to_parquet(
        tmp_path / block / "XYZ_chain.parquet")
    pd.DataFrame({"symbol": ["XYZ"], "mark": [100.0], "dte_min": [0], "dte_max": [21]}) \
        .to_parquet(tmp_path / block / "XYZ_underlying.parquet")
    assert chains.needs_capture("XYZ", sunday, (5, 21))[0] is False
    wanted, reason = chains.needs_capture("XYZ", sunday, (30, 59))
    assert wanted and "0-21" in reason


# --- Sheet: every accepted strike kept, best per ticker flagged ---------------

def test_select_sheet_keeps_every_accepted_strike():
    frame = pd.DataFrame({"ticker": ["AAA", "AAA", "BBB", "CCC"],
                          "accepted": [True, True, True, False],
                          "ev_annualised": [0.3, 0.2, 0.1, 0.5]})
    sheet = candidates.select_sheet(frame)
    assert len(sheet) == 3
    assert sheet["best_per_ticker"].tolist() == [True, False, True]
    assert "proposed" not in sheet                     # proposing is portfolio's job
    best = candidates.select_sheet(frame, best_per_ticker_only=True)
    assert best["ticker"].tolist() == ["AAA", "BBB"]


def test_candidate_strikes_follow_the_request():
    today = dt.date(2026, 9, 28)
    chain = pd.DataFrame({
        "expiration": [today + dt.timedelta(days=d) for d in (7, 7, 35, 35, 49)],
        "strike_price": [95.0, 90.0, 95.0, 90.0, 90.0],
        "put_delta": [-0.25, -0.10, -0.25, -0.18, -0.20]})
    weekly = candidates._candidate_strikes(chain, 100.0, today, {}, ScanRequest.default())
    assert set(weekly["dte_calendar"]) == {7} and len(weekly) == 1
    monthly = candidates._candidate_strikes(
        chain, 100.0, today, {},
        ScanRequest.from_dict({"dte_targets": [35], "delta_range": [0.15, 0.20]}))
    assert monthly["strike_price"].tolist() == [90.0]


def test_annotate_marks_best_per_ticker_and_underlyings_persist(tmp_path, monkeypatch):
    from pipeline import results
    monkeypatch.setattr(results, "runs_dir", lambda: tmp_path)
    run_id = "20260927-120000-test"
    (tmp_path / run_id).mkdir()
    (tmp_path / run_id / "manifest.json").write_text(json.dumps({
        "run_id": run_id, "started_at": "2026-09-27T12:00:00", "session_block": "x",
        "session_state": "closed", "tickers": 2, "finished_at": "2026-09-27T12:01:00",
        "stages": {"analyse": {}}, "warnings": [], "banner": "",
        "request": ScanRequest.default().to_dict()}), encoding="utf-8")
    full = pd.DataFrame({"ticker": ["AAA", "AAA"], "expiration": [dt.date(2026, 10, 2)] * 2,
                         "strike": [50.0, 48.0], "accepted": [True, True]})
    annotated = results.annotate_sheet(full, full.iloc[[0]], full.iloc[[0]])
    ranked = pd.DataFrame({"symbol": ["AAA"], "score": [0.5], "selected": [True]})
    results.write_tables(run_id, annotated, [], underlyings=ranked)
    loaded = results.load_run(run_id)
    assert loaded.candidates["best_per_ticker"].tolist() == [True, False]
    assert loaded.underlyings["symbol"].tolist() == ["AAA"]
    assert loaded.request["strategies"] == ["csp"]


# --- Account profiles and request caps ---------------------------------------

def test_account_profile_merges_over_the_account_block(monkeypatch):
    import core.paths
    cfg = dict(load_config())
    cfg["account_profiles"] = {"default": {}, "roth": {"net_liquidating_value": 100_000,
                                                      "max_collateral_per_position_pct": 0.10,
                                                      "allowed_strategies": ["csp"]}}
    monkeypatch.setattr(sizing, "load_config", lambda: cfg)
    roth = sizing.account_from_config("roth")
    assert roth.net_liquidating_value == 100_000
    assert roth.config["allowed_strategies"] == ["csp"]
    assert roth.config["cash_buffer_pct"] == cfg["account"]["cash_buffer_pct"]
    assert sizing.max_tradable_strike(roth) == pytest.approx(100.0)
    capped = sizing.account_from_config("roth", position_pct_override=0.05)
    assert sizing.max_tradable_strike(capped) == pytest.approx(50.0)
    assert core.paths.load_config() is not cfg


def test_screen_entry_uses_the_request_dte_window():
    from analytics.exit_rules import screen_entry
    inside = screen_entry(credit=1.5, strike=100, contracts=1, dte=35, dte_window=(30, 45))
    outside = screen_entry(credit=1.5, strike=100, contracts=1, dte=35)
    assert not any("target window" in w for w in inside.warnings)
    assert any("target window" in w for w in outside.warnings)


def test_manifest_carries_the_request():
    from pipeline.run import STAGES, RunManifest
    assert "request" in RunManifest.__dataclass_fields__
    keys = [k for k, _ in STAGES]
    assert keys.index("rank_underlyings") < keys.index("chains") < keys.index("analyse")
