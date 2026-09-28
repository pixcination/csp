"""
Phase 16: strategy specs, the generic resolver, stock and multi-expiry
positions, margin classes, the recommender, and spec positions in the book.
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

from analytics import costs, exit_rules, margin, paper, sizing  # noqa: E402
from analytics import prob_engine as pe  # noqa: E402
from analytics import strategy_spec as ss  # noqa: E402
from analytics.options_math import bs_price_greeks  # noqa: E402
from analytics.strategies import resolver  # noqa: E402
from analytics.strategies.base import Leg, Position  # noqa: E402
from analytics.strategies.context import TickerContext  # noqa: E402

TODAY = dt.date(2026, 9, 28)
SPOT = 100.0
EXPIRIES = [dt.date(2026, 10, 9), dt.date(2026, 10, 12), dt.date(2026, 10, 16),
            dt.date(2026, 10, 30), dt.date(2026, 11, 13), dt.date(2026, 12, 18)]


def _chain(iv: float = 0.25, unquoted=(dt.date(2026, 10, 12),)) -> pd.DataFrame:
    """A synthetic chain priced by Black-Scholes, generous liquidity; the
    Columbus-Day weekly is listed but carries no quotes."""
    rows = []
    for exp in EXPIRIES:
        days = (exp - TODAY).days
        for k in np.arange(70.0, 131.0, 1.0):
            row = {"expiration": exp, "strike_price": float(k), "root_symbol": "SYN",
                   "settlement_type": "PM"}
            for kind in ("put", "call"):
                g = bs_price_greeks(SPOT, k, days, iv, 0.045, kind)
                quoted = exp not in unquoted and g.price > 0.01
                half = max(0.02, 0.02 * g.price)
                row.update({f"{kind}_bid": g.price - half if quoted else None,
                            f"{kind}_ask": g.price + half if quoted else None,
                            f"{kind}_mark": g.price if quoted else None,
                            f"{kind}_iv": iv, f"{kind}_delta": g.delta, f"{kind}_gamma": g.gamma,
                            f"{kind}_theta": g.theta, f"{kind}_vega": g.vega,
                            f"{kind}_open_interest": 5000.0, f"{kind}_volume": 800.0,
                            f"{kind}_symbol": f"SYN{kind[0]}{k:g}"})
            rows.append(row)
    return pd.DataFrame(rows)


def _daily(n: int = 3000, vol: float = 0.25, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    r = rng.normal(-0.5 * vol * vol / 252, vol / np.sqrt(252), n)
    close = SPOT * np.exp(np.cumsum(r))
    close *= SPOT / close[-1]
    return pd.DataFrame({"date": pd.bdate_range(end="2026-09-25", periods=n), "open": close,
                         "high": close * 1.005, "low": close * 0.995, "close": close,
                         "volume": 5e6})


EMPTY_EVENTS = pd.DataFrame(columns=["symbol", "date", "type", "time_of_day", "confirmed",
                                     "amount", "source", "sources_disagree", "note"])


def _resolve(spec_id: str, profile: str = "default", **ctx_kw):
    from analytics.scan_request import ScanRequest
    ctx = TickerContext(ticker="SYN", spot=SPOT, today=TODAY, chain=_chain(**ctx_kw))
    account = sizing.account_from_config(profile)
    return resolver.resolve(ss.load_all()[spec_id], ctx, ScanRequest.default(), account,
                            today=TODAY, daily=_daily(), events_frame=EMPTY_EVENTS)


# --- Specs -----------------------------------------------------------------------------

def test_shipped_specs_load_and_describe_themselves():
    specs = ss.load_all()
    assert {"bull_put", "bear_call", "iron_condor", "strangle", "covered_call",
            "call_calendar", "pmcc"} <= set(specs)
    assert specs["call_calendar"].multi_expiry and specs["covered_call"].has_stock
    assert specs["strangle"].margin_class == "naked"
    text = specs["iron_condor"].leg_text()
    assert "short put -0.16d @45DTE" in text and "3% past short_put" in text


def test_condition_matrix_follows_the_specs():
    matrix = ss.condition_matrix(ss.load_all()).set_index("iv_regime")
    assert "strangle" in matrix.loc["high", "range"]
    assert "strangle" not in matrix.loc["mid", "range"]
    assert "call_calendar" in matrix.loc["low", "range"]
    assert "pmcc" in matrix.loc["low", "uptrend"] and "pmcc" not in matrix.loc["low", "range"]
    assert matrix.loc["low", "downtrend"] == ""


def test_conditions_and_iv_regime():
    spec = ss.load_all()["iron_condor"]
    assert ss.iv_regime(0.1) == "low" and ss.iv_regime(0.4) == "mid" and ss.iv_regime(0.8) == "high"
    assert ss.applies(spec, {"iv_regime": "mid", "trend": "range"})[0]
    # Phase 17: the regime is soft (reported, not blocking) unless asked strictly.
    ok, why = ss.applies(spec, {"iv_regime": "low", "trend": "range"})
    assert ok and "IV regime low" in why[0] and "soft" in why[0]
    ok, why = ss.applies(spec, {"iv_regime": "low", "trend": "range"}, soft_regime=False)
    assert not ok and "IV regime low" in why[0]
    ok, why = ss.applies(spec, {"iv_regime": "high", "trend": "range",
                                "earnings_in_window": True})
    assert not ok and "earnings" in why[0]
    ok, why = ss.applies(spec, {})                          # unknown: reported, not blocking
    assert ok and "IV regime unknown" in why


@pytest.mark.parametrize("patch,message", [
    ({"margin_class": "yolo"}, "margin_class"),
    ({"legs": [{"name": "a", "type": "put", "side": "short", "expiration": "front",
                "select": {"delta": -0.2, "atm": True}}]}, "exactly one selector"),
    ({"legs": [{"name": "a", "type": "put", "side": "short", "expiration": "front",
                "select": {"offset_from": "b", "width": 5}}]}, "earlier leg"),
    ({"legs": [{"name": "a", "type": "put", "side": "short", "expiration": "back",
                "select": {"atm": True}}]}, "is not one of"),
    ({"entry": {"trend": ["sideways"]}}, "entry.trend"),
])
def test_spec_validation(patch, message):
    base = {"id": "x", "label": "X", "margin_class": "defined_risk",
            "expirations": {"front": {"dte_target": 30}},
            "legs": [{"name": "a", "type": "put", "side": "short", "expiration": "front",
                      "select": {"atm": True}}]}
    with pytest.raises(ss.SpecError, match=message):
        ss.from_dict({**base, **patch})


# --- Stock and multi-expiry positions --------------------------------------------------

def test_covered_call_position_with_a_stock_leg():
    call = Leg("call", "short", 105.0, TODAY + dt.timedelta(days=30), iv=0.25, mid=1.5,
               delta=0.3, gamma=0.02, theta=-0.03, vega=0.1)
    stock = Leg("stock", "long", SPOT, TODAY + dt.timedelta(days=30))
    pos = Position("covered_call", "SYN", [stock, call], 1.5 - SPOT)
    assert pos.payoff(200.0) == pytest.approx(105.0 - SPOT + 1.5)       # capped at the strike
    assert pos.payoff(0.0) == pytest.approx(1.5 - SPOT)
    assert pos.max_loss == pytest.approx(SPOT - 1.5)
    assert pos.net_greeks()["delta"] == pytest.approx(100 * (1 - 0.3))
    assert margin.bpr_per_contract(pos, "covered", SPOT) == pytest.approx((SPOT - 1.5) * 100)


def _calendar(front_iv=0.25, back_iv=0.25) -> Position:
    f_days, b_days = 14, 21
    f = bs_price_greeks(SPOT, SPOT, f_days, front_iv, 0.045, "call").price
    b = bs_price_greeks(SPOT, SPOT, b_days, back_iv, 0.045, "call").price
    legs = [Leg("call", "short", SPOT, TODAY + dt.timedelta(days=f_days), iv=front_iv, mid=f),
            Leg("call", "long", SPOT, TODAY + dt.timedelta(days=b_days), iv=back_iv, mid=b)]
    return Position("call_calendar", "SYN", legs, f - b, front_dte=f_days)


def test_calendar_payoff_and_forward_vol():
    pos = _calendar()
    assert pos.multi_expiry and pos.expiry_offsets() == [0, 7]
    assert pos.credit < 0                                                 # a debit
    assert pos.max_loss == pytest.approx(-pos.credit, rel=0.02)          # lose the debit
    assert pos.payoff(SPOT) == pytest.approx(pos.max_profit, rel=0.02)   # best at the strike
    back = pos.legs[1]
    assert pos.later_leg_vol(back, 7) == pytest.approx(0.25)             # flat structure
    steep = _calendar(front_iv=0.20, back_iv=0.25)
    fwd = steep.later_leg_vol(steep.legs[1], 7)
    assert fwd == pytest.approx(np.sqrt((0.25 ** 2 * 21 - 0.20 ** 2 * 14) / 7))
    # At entry the legs are worth exactly the debit paid.
    assert steep.value(SPOT, 14, rate=0.045) == pytest.approx(-steep.credit, abs=1e-9)


def test_single_expiry_positions_are_unchanged():
    legs = [Leg("put", "short", 95.0, "2026-11-20", iv=0.2),
            Leg("put", "long", 90.0, "2026-11-20", iv=0.22)]
    pos = Position("pcs", "X", legs, 1.2)
    grid = np.array([80.0, 92.0, 97.0])
    assert list(pos.payoff(grid)) == pytest.approx([1.2 - 5, 1.2 - 3, 1.2])
    assert not pos.multi_expiry and pos.max_loss == pytest.approx(3.8)


@pytest.mark.parametrize("builder", ["calendar", "covered_call"])
def test_market_implied_model_has_no_edge(builder):
    """G is a zero-edge baseline for any Position: a calendar valued at the
    forward vol, and a covered call net of the shares' risk-free carry."""
    cfg = pe.EngineConfig(n_paths=40_000, seed=7)
    rng = np.random.default_rng(3)
    if builder == "calendar":
        pos, dte = _calendar(front_iv=0.22, back_iv=0.26), 14
    else:
        prem = bs_price_greeks(SPOT, 105.0, 30, 0.25, 0.045, "call").price
        pos = Position("covered_call", "SYN",
                       [Leg("stock", "long", SPOT, TODAY + dt.timedelta(days=30)),
                        Leg("call", "short", 105.0, TODAY + dt.timedelta(days=30), iv=0.25)],
                       prem - SPOT)
        dte = 30
    steps = round(dte * 252 / 365)
    normals = pe.g_normals(cfg.n_paths, steps, rng, True)
    returns = pe.g_log_returns(normals, pos.legs[-1].iv if builder != "calendar"
                               else pos.legs[0].iv, cfg.rate, dte / 365)
    spec = pe.TradeSpec(pos, SPOT, dte, steps, 1, max(pos.max_loss, 1) * 100)
    out = pe.evaluate(spec, returns, [50, 100], cfg)
    entry, _, _ = pe._fees(spec)
    gross = out["policies"]["hold"]["ev"] + entry
    assert abs(gross) < 6.0, gross                 # dollars per contract, before entry fees


# --- Costs and margin --------------------------------------------------------------------

def test_multi_leg_fill_credit_debit_and_refusal():
    condor = [{"side": "short", "qty": 1, "bid": 1.0, "ask": 1.1},
              {"side": "long", "qty": 1, "bid": 0.4, "ask": 0.5},
              {"side": "short", "qty": 1, "bid": 0.9, "ask": 1.0},
              {"side": "long", "qty": 1, "bid": 0.3, "ask": 0.4}]
    fill = costs.multi_leg_fill(condor, fraction=0.4)
    assert fill["net_mid"] == pytest.approx(1.05 - 0.45 + 0.95 - 0.35)
    assert fill["natural"] == pytest.approx(1.0 - 0.5 + 0.9 - 0.4)
    assert fill["modelled"] == pytest.approx(1.2 - 0.4 * 0.2)
    debit = costs.multi_leg_fill([{"side": "short", "qty": 1, "bid": 2.0, "ask": 2.1},
                                  {"side": "long", "qty": 1, "bid": 3.0, "ask": 3.1}])
    assert debit["net_mid"] == pytest.approx(-1.0) and debit["modelled"] < -1.0
    assert costs.multi_leg_fill([{"side": "short", "qty": 1, "bid": 0.0, "ask": 0.1}]) is None


def test_naked_requirement_and_permissions():
    # Put: max(20% x 100 - 5 OTM + 1, 10% x 95 + 1) = 16
    assert margin.naked_requirement(100, 95, 1.0, "put") == pytest.approx(16.0)
    # Deep OTM call: the 10% floor binds: max(20 - 30 + 0.2, 10 + 0.2)
    assert margin.naked_requirement(100, 130, 0.2, "call") == pytest.approx(10.2)
    legs = [Leg("put", "short", 95.0, "2026-11-20", mid=1.0),
            Leg("call", "short", 105.0, "2026-11-20", mid=0.8)]
    pos = Position("strangle", "X", legs, 1.8)
    put_req = margin.naked_requirement(100, 95, 1.0, "put")
    call_req = margin.naked_requirement(100, 105, 0.8, "call")
    assert margin.bpr_per_contract(pos, "naked", 100) == pytest.approx(
        (max(put_req, call_req) + (0.8 if put_req >= call_req else 1.0)) * 100)
    assert not margin.permitted("naked", {"account_type": "research"})[0]
    assert "IRA" in margin.permitted("naked", {"account_type": "roth_ira",
                                               "naked_approval": True})[1]
    assert margin.permitted("naked", {"account_type": "margin", "naked_approval": True})[0]
    assert not margin.permitted("defined_risk", {"spread_approval": False})[0]


# --- Resolver ----------------------------------------------------------------------------

def test_iron_condor_resolves_with_ordered_strikes():
    rows, reasons = _resolve("iron_condor")
    assert len(rows) == 1, reasons
    row = rows[0]
    pos = resolver.position_from_row(row)
    by = {(l.option_type, l.side): l.strike for l in pos.legs}
    assert by[("put", "long")] < by[("put", "short")] < SPOT < by[("call", "short")] \
        < by[("call", "long")]
    assert by[("put", "short")] - by[("put", "long")] == pytest.approx(3.0)
    assert row["dte_calendar"] == (dt.date(2026, 11, 13) - TODAY).days     # nearest 45
    assert row["modelled_fill"] > 0 and row["bpr_per_contract"] == pytest.approx(
        pos.max_loss * 100)
    assert "delta nearest -0.16" in row["strike_reasons"]
    assert json.loads(row["legs_json"])[0]["option_type"] == "put"


def test_calendar_skips_an_unquoted_expiry_and_is_a_debit():
    rows, reasons = _resolve("call_calendar")
    assert rows, reasons
    row = rows[0]
    pos = resolver.position_from_row(row)
    front, back = sorted(pos.legs, key=lambda l: l.expiration)
    assert front.expiration == dt.date(2026, 10, 9)          # not the unquoted Oct 12
    assert back.expiration == dt.date(2026, 10, 16) and front.strike == back.strike
    assert row["modelled_fill"] < 0 and row["multi_expiry"]
    assert any("model risk" in w for w in row["warnings"])


def test_strangle_is_refused_without_naked_permission():
    rows, _ = _resolve("strangle")
    assert rows and not rows[0]["accepted"]
    assert any("margin account" in r for r in rows[0]["rejections"])


def test_covered_call_buys_the_shares():
    rows, reasons = _resolve("covered_call")
    assert rows, reasons
    row = rows[0]
    assert row["legs"].startswith("+100 sh")
    assert row["modelled_fill"] == pytest.approx(row["option_credit"] - SPOT)
    assert row["bpr_per_contract"] == pytest.approx((SPOT - row["option_credit"]) * 100, rel=0.01)


# --- Scan request and pipeline tables ------------------------------------------------------

def test_scan_request_specs_widen_the_chain_window():
    from analytics.scan_request import RequestError, ScanRequest
    req = ScanRequest.default(specs=["pmcc"])
    lo, hi = req.chain_dte_window()
    assert hi >= 90 + 30
    assert ScanRequest.default().spec_dte_window() == (None, None)
    with pytest.raises(RequestError, match="unknown strategy spec"):
        ScanRequest.default(specs=["butterfly"])


def test_strategy_tables_round_trip(tmp_path, monkeypatch):
    from pipeline import results as run_results
    monkeypatch.setattr(run_results, "runs_dir", lambda: tmp_path)
    rows, _ = _resolve("iron_condor")
    sheet = pd.DataFrame(rows)
    (tmp_path / "r1").mkdir()
    (tmp_path / "r1" / "manifest.json").write_text(json.dumps(
        {"run_id": "r1", "started_at": "2026-09-28T09:00:00", "session_block": "x",
         "session_state": "closed", "tickers": 1}))
    written = run_results.write_strategies("r1", sheet, pd.DataFrame([{"ticker": "SYN"}]))
    assert written == {"strategies": 1, "strategy_conditions": 1}
    loaded = run_results.load_run("r1")
    back = resolver.position_from_row(loaded.strategies.iloc[0].to_dict())
    assert len(back.legs) == 4


# --- Recommender on a real stored chain ----------------------------------------------------

def test_recommender_ranks_real_spy_positions():
    from analytics import recommender
    from data_sources import chains
    if chains.load_chain("SPY")[0].empty:
        pytest.skip("no stored SPY chain")
    out = recommender.run(["SPY"], spec_ids=["iron_condor", "call_calendar"], n_paths=2000)
    sheet, conditions = out["sheet"], out["conditions"]
    assert set(conditions["strategy"]) == {"iron_condor", "call_calendar"}
    if sheet.empty:
        pytest.skip("stored SPY chain resolves none of the specs")
    assert {"pop_blend", "headline_policy", "ev_per_day_bpr", "conditions_met"} <= set(sheet)
    assert list(sheet["rank_key"]) == sorted(sheet["rank_key"], reverse=True)
    cal = sheet[sheet["strategy"] == "call_calendar"]
    if not cal.empty:
        # Phase 17: the spec's own exit block -- 25% target and a 0.5x-debit stop.
        assert cal.iloc[0]["headline_policy"] == "close_25_stop_0.5x"


# --- Paper book and management -------------------------------------------------------------

@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "db_trade_log", lambda: tmp_path / "t.duckdb")
    paper.ensure_schema()


def test_spec_positions_record_and_close(ledger):
    condor = _resolve("iron_condor")[0][0]
    result = paper.accept(condor, contracts=2)
    row = paper.list_positions().iloc[0]
    assert row["strategy"] == "iron_condor" and len(paper.list_legs()) == 4
    assert row["max_profit_share"] == pytest.approx(condor["modelled_fill"])
    assert paper.leg_text(row) == condor["legs"]
    paper.record_mark(result.position_id, condor["modelled_fill"] / 2)
    assert paper.list_positions().iloc[0]["max_profit_pct_seen"] == pytest.approx(0.5)
    paper.close_position(result.position_id, "expired_otm")
    assert paper.performance()["by_strategy"]["iron_condor"]["n_closed"] == 1

    calendar = _resolve("call_calendar")[0][0]
    cal = paper.accept(calendar, contracts=1)
    stored = paper.list_positions().set_index("id").loc[cal.position_id]
    assert pd.isna(stored["actual_fill"])                   # the modelled fill, marked as such
    assert stored["modelled_fill"] < 0 and stored["max_profit_share"] > 0
    with pytest.raises(ValueError, match="outlives its front expiry"):
        paper.close_position(cal.position_id, "settled", settlement_price=SPOT)
    paper.close_position(cal.position_id, "closed_early", exit_price=-2.0)

    with pytest.raises(ValueError, match="option legs only"):
        paper.accept(_resolve("covered_call")[0][0])


def test_spec_position_management():
    base = dict(ticker="SYN", label="Long call calendar", contracts=2, entry_credit=-2.0,
                current_mark=-2.0, max_profit=3.0, calendar_days_left=8,
                entry_dte_calendar=11, legs=(("short", 1), ("long", 1)))
    cfg = {"profit_target_pct": 25, "loss_stop_multiple": 0.5}
    stop = exit_rules.evaluate_spec_position(
        exit_rules.OpenSpecPosition(**{**base, "current_mark": -0.9}), cfg)
    assert stop.action == exit_rules.Action.CLOSE and "loss stop" in stop.headline
    hit = exit_rules.evaluate_spec_position(
        exit_rules.OpenSpecPosition(**{**base, "current_mark": -3.0}), cfg)
    assert hit.action == exit_rules.Action.CLOSE and "target" in hit.headline
    hold = exit_rules.evaluate_spec_position(exit_rules.OpenSpecPosition(**base), cfg)
    assert hold.action == exit_rules.Action.HOLD
    timed = exit_rules.evaluate_spec_position(
        exit_rules.OpenSpecPosition(**{**base, "entry_credit": 3.0, "current_mark": 2.5,
                                       "max_profit": 3.0, "entry_dte_calendar": 45,
                                       "calendar_days_left": 20}),
        {"time_stop_dte": 21})
    assert "time stop" in timed.headline


def test_pipeline_manages_open_spec_positions(ledger, monkeypatch):
    from core.progress import NullReporter
    from data_sources import chains, yfinance_sync
    from pipeline import run as pipeline_run
    condor = _resolve("iron_condor")[0][0]
    result = paper.accept(condor, contracts=1, entry_date=TODAY)
    chain = _chain()
    monkeypatch.setattr(chains, "load_chain",
                        lambda t, block=None: (chain, pd.DataFrame({"mark": [SPOT]})))
    monkeypatch.setattr(yfinance_sync, "load_daily", lambda *a, **k: pd.DataFrame())
    decisions = pipeline_run._evaluate_open_positions(NullReporter())
    assert len(decisions) == 1 and decisions[0]["strategy"] == "iron_condor"
    assert decisions[0]["position_id"] == result.position_id
    assert len(paper.list_marks(result.position_id)) == 1


# --- Pages ---------------------------------------------------------------------------------

def test_strategies_page_scans_the_stored_chain():
    from data_sources import chains
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app" / "pages" / "10_Strategies.py"), default_timeout=300)
    at.run()
    assert not at.exception, [e.message for e in at.exception]
    assert len(at.tabs) == 3
    if chains.load_chain("SPY")[0].empty:
        return
    radio = [r for r in at.radio if r.key == "strat_source"][0]
    radio.set_value("Scan the stored chains now").run()
    at.text_input[0].set_value("SPY")
    at.radio[1].set_value("Chosen specs")
    at.multiselect[0].set_value(["iron_condor"])
    at.selectbox[1].set_value(2000)
    at.button[0].click().run()
    assert not at.exception, [e.message for e in at.exception]
