"""
Phase 15: multi-leg paper book, PCS management, calibration of P(reach X%),
the open book (Greeks, beta-weighted delta, events) and the PCS backtest.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics import book, calibration, costs, exit_rules, paper  # noqa: E402
from analytics import pcs_backtest as pb  # noqa: E402
from analytics.exit_rules import Action, OpenSpread  # noqa: E402

PCS = {"strategy": "pcs", "ticker": "SPY", "strike": 700.0, "long_strike": 690.0,
       "expiration": "2026-11-20", "modelled_fill": 2.00, "contracts": 3,
       "bid": 5.00, "ask": 5.20, "mid": 5.10, "long_bid": 3.00, "long_ask": 3.12,
       "long_mid": 3.06, "implied_vol": 0.18, "long_iv": 0.20, "delta": -0.25,
       "long_delta": -0.17, "prob_otm_empirical": 0.78, "pop_blend": 0.74,
       "p_hit_25_blend": 0.85, "p_hit_50_blend": 0.62, "p_hit_100_blend": 0.55,
       "median_days_50_blend": 12.0, "p_max_loss_blend": 0.08,
       "headline_policy": "close_50", "settlement": "physical"}
CSP = {"ticker": "F", "strike": 11.0, "expiration": "2026-08-28", "modelled_fill": 0.18,
       "contracts": 20, "prob_otm_empirical": 0.87, "bid": 0.17, "ask": 0.20, "mid": 0.185}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    db = tmp_path / "trade_log.duckdb"
    monkeypatch.setattr(paper, "db_trade_log", lambda: db)
    paper.ensure_schema()
    return db


# --- Schema and migration --------------------------------------------------------------

def test_single_leg_rows_are_migrated_to_one_leg_idempotently(tmp_path, monkeypatch):
    db = tmp_path / "old.duckdb"
    monkeypatch.setattr(paper, "db_trade_log", lambda: db)
    con = duckdb.connect(str(db))
    for statement in paper.SCHEMA[:7]:            # the Phase 14 tables only
        con.execute(statement)
    con.execute("INSERT INTO paper_positions (ticker, strategy, strike, expiration, contracts, "
                "modelled_fill, actual_fill, status, collateral) VALUES "
                "('KO', 'csp', 60, DATE '2026-10-02', 2, 0.30, 0.31, 'open', 12000)")
    con.close()
    paper.ensure_schema()
    paper.ensure_schema()
    legs = paper.list_legs()
    assert len(legs) == 1
    leg = legs.iloc[0]
    assert (leg["side"], leg["option_type"], leg["strike"]) == ("short", "put", 60.0)
    assert leg["entry_price"] == pytest.approx(0.31)
    row = paper.list_positions().iloc[0]
    assert "long_strike" in row.index and pd.isna(row["long_strike"])


# --- Accepting spreads -----------------------------------------------------------------

def test_accept_spread_records_legs_bpr_quote_and_predictions(ledger):
    result = paper.accept(PCS, actual_fill=1.95)
    assert result.cycle_id is None                        # spreads do not open a wheel cycle
    assert result.collateral == pytest.approx((10 - 1.95) * 100 * 3)
    row = paper.list_positions().iloc[0]
    assert row["strategy"] == "pcs" and row["long_strike"] == 690 and row["width"] == 10
    assert row["max_loss"] == pytest.approx(result.collateral)
    assert row["entry_fees"] == pytest.approx(costs.legs_open([("sell", 3), ("buy", 3)]).total)
    assert row["quote_net_mid"] == pytest.approx(2.04)
    assert row["quote_half_spread"] == pytest.approx(0.16)
    assert row["rec_pop"] == pytest.approx(0.74) and row["settlement_type"] == "physical"
    legs = paper.list_legs([result.position_id])
    assert list(legs["side"]) == ["short", "long"] and list(legs["strike"]) == [700, 690]
    preds = paper.list_predictions(result.position_id)
    blend = preds[preds["model"] == "blend"].set_index("metric")["value"]
    assert blend["p_hit_50"] == pytest.approx(0.62) and blend["p_max_loss"] == pytest.approx(0.08)
    assert "median_days_50" in blend.index
    assert "SPY 2026-11-20 $700/$690 put spread" in result.message


def test_leg_fills_give_the_net_and_bad_credits_are_refused(ledger):
    result = paper.accept(PCS, leg_fills=[5.05, 3.10])
    assert result.actual_fill == pytest.approx(1.95)
    legs = paper.list_legs([result.position_id])
    assert list(legs["entry_price"]) == pytest.approx([5.05, 3.10])
    with pytest.raises(ValueError, match="between 0 and the width"):
        paper.accept(PCS, actual_fill=10.5)
    with pytest.raises(ValueError, match="2 leg fill"):
        paper.accept(PCS, leg_fills=[5.0])


def test_index_spreads_are_cash_settled(ledger):
    paper.accept({**PCS, "ticker": "SPX", "settlement": None, "root_symbol": "SPXW"})
    assert paper.list_positions().iloc[0]["settlement_type"] == "cash"


# --- Closing, settling, rolling --------------------------------------------------------

@pytest.mark.parametrize("price,debit,fee_case,settlement", [
    (695.0, 5.0, "short_itm", "cash"), (680.0, 10.0, "max_loss", "cash"),
    (680.0, 10.0, "max_loss", "physical")])
def test_settling_a_spread_prices_the_debit_and_exercise_fees(ledger, price, debit, fee_case,
                                                              settlement):
    result = paper.accept({**PCS, "settlement": settlement}, actual_fill=2.0)
    paper.close_position(result.position_id, "settled", dt.date(2026, 11, 20),
                         settlement_price=price)
    row = paper.list_positions().iloc[0]
    assert row["status"] == "settled" and paper.list_share_lots().empty
    assert row["exit_price"] == pytest.approx(debit)
    assert row["exit_fees"] == pytest.approx(
        costs.vertical_exit_fees(3, settlement == "cash")[fee_case])
    realised = paper.performance()["total_realized"]
    assert realised == pytest.approx((2.0 - debit) * 300 - row["entry_fees"] - row["exit_fees"])
    assert sorted(paper.list_legs()["exit_price"]) == sorted(
        [max(700 - price, 0), max(690 - price, 0)])


def test_physical_spread_between_the_strikes_becomes_a_share_lot(ledger):
    """Tom, 2026-09-28: treat it as an assigned CSP -- shares at the short
    strike, basis lowered by the net credit, in a wheel cycle."""
    result = paper.accept(PCS, actual_fill=2.0)
    assert result.cycle_id is None
    paper.close_position(result.position_id, "settled", dt.date(2026, 11, 20),
                         settlement_price=695.0)
    row = paper.list_positions().iloc[0]
    assert row["status"] == "assigned" and row["exit_price"] == 0.0
    assert row["exit_fees"] == pytest.approx(costs.vertical_exit_fees(3, False)["short_itm"])
    assert row["max_profit_pct_seen"] == pytest.approx((2.0 - 5.0) / 2.0)
    assert "300 shares held" in row["notes"]
    lots = paper.list_share_lots()
    assert len(lots) == 1
    lot = lots.iloc[0]
    assert lot["shares"] == 300 and lot["acquisition_price"] == 700
    assert lot["adjusted_basis"] == pytest.approx(698.0)
    assert lot["cycle_id"] == row["cycle_id"] and pd.notna(row["cycle_id"])
    # POP: settled below the 698 breakeven, so the spread's claim failed ...
    closed = paper.list_positions()
    assert calibration.outcomes(closed).iloc[0] == 0.0
    # ... and one settling between the breakeven and the short strike made money.
    other = paper.accept(PCS, actual_fill=2.0)
    paper.close_position(other.position_id, "settled", settlement_price=699.0)
    won = calibration.outcomes(paper.list_positions().set_index("id"))
    assert won[other.position_id] == 1.0


def test_status_rules_per_strategy(ledger):
    spread = paper.accept(PCS)
    with pytest.raises(ValueError, match="settled"):
        paper.close_position(spread.position_id, "assigned")
    with pytest.raises(ValueError, match="settlement price"):
        paper.close_position(spread.position_id, "settled")
    put = paper.accept(CSP)
    with pytest.raises(ValueError, match="assigned"):
        paper.close_position(put.position_id, "settled", settlement_price=10)
    paper.close_position(spread.position_id, "expired_otm")
    with pytest.raises(ValueError, match="already"):
        paper.close_position(spread.position_id, "closed_early", exit_price=0.1)
    stats = paper.performance()
    assert stats["by_strategy"]["pcs"]["n_closed"] == 1


def test_csp_roll_stays_in_its_cycle_and_is_linked(ledger):
    first = paper.accept(CSP, actual_fill=0.18)
    rolled = paper.roll_position(first.position_id, close_debit=0.40,
                                 new_expiration="2026-09-04", new_strike=10.5, new_credit=0.45)
    assert rolled.net_per_share == pytest.approx(0.05) and "net credit" in rolled.message
    book_rows = paper.list_positions().set_index("id")
    assert book_rows.loc[first.position_id, "status"] == "rolled"
    new = book_rows.loc[rolled.opened.position_id]
    assert new["rolled_from"] == first.position_id and new["rolls_used"] == 1
    assert new["cycle_id"] == book_rows.loc[first.position_id, "cycle_id"]


def test_spread_roll_keeps_the_width_and_flags_a_debit(ledger):
    first = paper.accept(PCS, actual_fill=2.0)
    rolled = paper.roll_position(first.position_id, close_debit=4.0,
                                 new_expiration="2026-12-18", new_strike=690, new_credit=3.5)
    assert "DEBIT" in rolled.message
    new = paper.list_positions().set_index("id").loc[rolled.opened.position_id]
    assert new["long_strike"] == 680 and pd.isna(new["cycle_id"])


def test_marks_keep_the_best_profit_seen(ledger):
    result = paper.accept(PCS, actual_fill=2.0)
    assert paper.record_mark(result.position_id, 1.2) == pytest.approx(0.4)
    paper.record_mark(result.position_id, 1.8, mark_date=dt.date.today() + dt.timedelta(days=1))
    assert paper.list_positions().iloc[0]["max_profit_pct_seen"] == pytest.approx(0.4)
    assert len(paper.list_marks(result.position_id)) == 2


# --- Calibration ------------------------------------------------------------------------

def test_target_outcomes_hit_miss_and_censoring(ledger):
    early = paper.accept(PCS, actual_fill=2.0)
    paper.record_mark(early.position_id, 1.40)                     # 30% of max
    paper.close_position(early.position_id, "closed_early", exit_price=1.40)
    expired = paper.accept(PCS, actual_fill=2.0)
    paper.close_position(expired.position_id, "expired_otm")
    settled = paper.accept(PCS, actual_fill=2.0)
    paper.record_mark(settled.position_id, 1.60)                   # 20% of max
    paper.close_position(settled.position_id, "settled", settlement_price=695.0)

    frame = calibration.target_outcomes().set_index(["position_id", "target"])["result"]
    assert frame[(early.position_id, 25)] == "hit"
    assert frame[(early.position_id, 50)] == "censored"
    assert frame[(early.position_id, 100)] == "censored"
    assert all(frame[(expired.position_id, t)] == "hit" for t in (25, 50, 100))
    assert all(frame[(settled.position_id, t)] == "miss" for t in (25, 50, 100))

    table = calibration.target_calibration()["table"].set_index("target")
    assert table.loc[25, "observed"] == pytest.approx(2 / 3)
    assert table.loc[50, "censored"] == 1 and table.loc[50, "scored"] == 2


def test_pop_outcomes_count_a_losing_early_close_as_a_loss(ledger):
    loser = paper.accept(CSP, actual_fill=0.18)
    paper.close_position(loser.position_id, "closed_early", exit_price=0.50)
    winner = paper.accept(CSP, actual_fill=0.18)
    paper.close_position(winner.position_id, "closed_early", exit_price=0.05)
    closed = paper.list_positions().set_index("id")
    won = calibration.outcomes(closed)
    assert won[loser.position_id] == 0.0 and won[winner.position_id] == 1.0


def test_fill_calibration_uses_the_package_quote(ledger):
    paper.accept({**PCS, "bid": 1.00, "ask": 1.10, "long_bid": 0.40, "long_ask": 0.46,
                  "modelled_fill": 0.60}, actual_fill=0.59)
    fills = calibration.fill_calibration()
    # net mid 1.05 - 0.43 = 0.62, summed half-spread 0.05 + 0.03 = 0.08
    assert fills.implied_fraction == pytest.approx((0.62 - 0.59) / 0.08)


# --- Spread management -----------------------------------------------------------------

CFG = {"profit_target_pct": 50, "loss_stop_multiple": 2.0, "time_stop_dte": 21,
       "roll_when_short_delta_beyond": -0.45, "roll_when_price_below_short": True,
       "min_dte_to_roll": 5, "max_rolls": 1, "require_net_credit_to_roll": True,
       "roll_out_days": [7, 35], "close_when_remaining_value_below": 0.05,
       "min_net_gain": 5.0, "hold_max_dte": 14}


@pytest.fixture
def rules(monkeypatch):
    cfg = dict(CFG)
    monkeypatch.setattr(exit_rules, "spread_config", lambda: cfg)
    return cfg


def _spread(**kw) -> OpenSpread:
    base = dict(ticker="SPY", short_strike=700.0, long_strike=690.0, contracts=2,
                entry_credit=2.0, spot=720.0, current_mark=1.5, calendar_days_left=30,
                trading_days_left=21, entry_dte_calendar=45, short_delta=-0.2)
    return OpenSpread(**{**base, **kw})


def test_loss_stop_rolls_while_a_roll_is_left_then_closes(rules):
    decision = exit_rules.evaluate_put_spread(_spread(current_mark=6.5))
    assert decision.action == Action.ROLL and decision.urgency == "act_now"
    assert "loss stop" in decision.headline
    decision = exit_rules.evaluate_put_spread(_spread(current_mark=6.5, rolls_used=1))
    assert decision.action == Action.CLOSE


def test_breach_rolls_and_close_when_too_late(rules):
    assert exit_rules.evaluate_put_spread(
        _spread(spot=695.0, current_mark=3.0)).action == Action.ROLL
    late = exit_rules.evaluate_put_spread(
        _spread(spot=695.0, current_mark=3.0, calendar_days_left=3, trading_days_left=2))
    assert late.action == Action.CLOSE and "too little time" in late.rationale


def test_profit_target_applies_above_the_hold_horizon_only(rules):
    assert exit_rules.evaluate_put_spread(_spread(current_mark=0.9)).action == Action.CLOSE
    weekly = exit_rules.evaluate_put_spread(
        _spread(current_mark=0.9, entry_dte_calendar=7, calendar_days_left=5,
                trading_days_left=4))
    assert weekly.action == Action.HOLD


def test_target_below_minimum_net_gain_holds(rules):
    rules["close_when_remaining_value_below"] = 0.01
    decision = exit_rules.evaluate_put_spread(
        _spread(contracts=1, entry_credit=0.12, current_mark=0.06))
    assert decision.action == Action.HOLD and decision.numbers["below_min_gain"]


def test_time_stop_and_value_floor(rules):
    decision = exit_rules.evaluate_put_spread(_spread(calendar_days_left=20))
    assert decision.action == Action.CLOSE and "time stop" in decision.headline
    rules["time_stop_dte"] = None
    assert exit_rules.evaluate_put_spread(_spread(calendar_days_left=20)).action == Action.HOLD
    assert exit_rules.evaluate_put_spread(_spread(current_mark=0.04)).headline.endswith(
        "nothing left in it")


def test_roll_candidates_same_width_later_expiry_net_credit_only(rules):
    rows = []
    # Put value rising with the strike, more steeply for later expirations,
    # so a 10-wide spread is worth more the further out it is.
    for exp, slope in (("2026-10-16", 0.15), ("2026-10-23", 0.25), ("2026-11-20", 0.35),
                       ("2026-12-18", 0.45)):
        for k in range(670, 705, 5):
            value = max(0.2, (k - 670) * slope)
            rows.append({"expiration": exp, "strike_price": float(k), "put_bid": value,
                         "put_ask": value + 0.1, "put_delta": -0.2})
    chain = pd.DataFrame(rows)
    position = _spread(current_mark=2.0)
    out = exit_rules.spread_roll_candidates(chain, position, "2026-10-16",
                                            today=dt.date(2026, 10, 1))
    assert not out.empty
    assert (out["net"] > 0).all()
    assert ((out["short_strike"] - out["long_strike"]) == 10).all()
    assert (out["short_strike"] <= 700).all()
    assert set(pd.to_datetime(out["expiration"]).dt.date) <= {dt.date(2026, 10, 23),
                                                              dt.date(2026, 11, 20)}


# --- The open book ---------------------------------------------------------------------

def _chain(exp="2026-11-20"):
    return pd.DataFrame({
        "expiration": [exp, exp], "strike_price": [700.0, 690.0],
        "put_bid": [3.9, 2.4], "put_ask": [4.1, 2.6], "put_mark": [4.0, 2.5],
        "put_iv": [0.18, 0.2], "put_delta": [-0.30, -0.15], "put_gamma": [0.01, 0.006],
        "put_theta": [-0.05, -0.03], "put_vega": [0.5, 0.35]})


def _legs(position_id=1):
    return pd.DataFrame({"position_id": [position_id] * 2, "option_type": ["put", "put"],
                         "side": ["short", "long"], "strike": [700.0, 690.0],
                         "expiration": [dt.date(2026, 11, 20)] * 2, "qty": [1, 1],
                         "iv": [0.18, 0.2], "root_symbol": [None, None]})


def test_mark_and_greeks_of_a_spread():
    pos = {"contracts": 2, "actual_fill": 2.0}
    marks = book.mark_position(pos, _legs(), _chain(), 720.0, dt.date(2026, 10, 1))
    assert marks["mark"] == pytest.approx(1.5)                    # 4.0 - 2.5
    assert marks["natural"] == pytest.approx(4.1 - 2.4)
    assert marks["unrealized"] == pytest.approx(0.5 * 200)
    assert marks["delta_shares"] == pytest.approx((0.30 - 0.15) * 200)   # short puts: long delta
    assert marks["theta_day"] == pytest.approx((0.05 - 0.03) * 200)      # credit earns theta
    assert marks["vega"] < 0 and marks["greeks_source"] == "chain"
    flat = _legs().assign(iv=0.18)          # equal IVs: the nearer short leg decays faster
    unquoted = book.mark_position(pos, flat, pd.DataFrame(), 720.0, dt.date(2026, 10, 1))
    assert unquoted["mark"] is None and unquoted["greeks_source"] == "model"
    assert unquoted["delta_shares"] > 0 and unquoted["theta_day"] > 0


def _daily(multiplier: float) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    r = rng.normal(0, 0.01, 400)
    dates = pd.bdate_range("2025-01-01", periods=401)
    close = 100 * np.cumprod(np.concatenate([[1.0], 1 + multiplier * r]))
    return pd.DataFrame({"date": dates, "close": close})


def test_open_book_beta_weights_delta_and_summarises():
    positions = pd.DataFrame([{"id": 1, "ticker": "AAA", "strategy": "pcs", "strike": 700.0,
                               "long_strike": 690.0, "expiration": dt.date(2026, 11, 20),
                               "contracts": 2, "actual_fill": 2.0, "modelled_fill": 2.0,
                               "collateral": 1600.0, "max_loss": 1600.0, "rolls_used": 0,
                               "entry_date": dt.date(2026, 10, 1)}])
    loaders = {"AAA": (_chain(), 720.0), "SPY": (pd.DataFrame(), 480.0)}
    frame = book.open_book(dt.date(2026, 10, 1), chain_loader=lambda t: loaders[t],
                           daily_loader=lambda t, basis: _daily(2.0 if t == "AAA" else 1.0),
                           positions=positions, legs=_legs())
    row = frame.iloc[0]
    assert row["beta"] == pytest.approx(2.0, rel=1e-6)
    assert row["bw_delta"] == pytest.approx(30.0 * 2.0 * 720 / 480)
    assert row["legs"] == "$700/$690 put spread" and row["dte"] == 50
    totals = book.summary(frame, nlv=100_000)
    assert totals["bpr"] == 1600 and totals["utilisation"] == pytest.approx(0.016)
    assert totals["theta_day"] == pytest.approx(4.0)

    events = pd.DataFrame({"symbol": ["*", "BBB", "AAA", "*"],
                           "date": [dt.date(2026, 10, 28), dt.date(2026, 10, 20),
                                    dt.date(2026, 10, 22), dt.date(2026, 12, 9)],
                           "type": ["fomc", "earnings", "earnings", "fomc"],
                           "note": ["", "", "", ""]})
    calendar = book.event_calendar(frame, events, dt.date(2026, 10, 1))
    assert list(calendar["event"]) == ["earnings", "fomc"]       # BBB's and December's excluded
    assert calendar["positions"].iloc[0] == "#1 AAA"


# --- Pipeline review of open spreads ----------------------------------------------------

def test_pipeline_reviews_spreads_and_records_marks(ledger, monkeypatch):
    from data_sources import chains, yfinance_sync
    from pipeline import run as pipeline_run
    from core.progress import NullReporter
    exp = (dt.date.today() + dt.timedelta(days=40)).isoformat()
    result = paper.accept({**PCS, "expiration": exp}, actual_fill=2.0,
                          entry_date=dt.date.today() - dt.timedelta(days=5))
    monkeypatch.setattr(chains, "load_chain",
                        lambda t, block=None: (_chain(exp), pd.DataFrame({"mark": [720.0]})))
    monkeypatch.setattr(yfinance_sync, "load_daily", lambda *a, **k: pd.DataFrame())
    decisions = pipeline_run._evaluate_open_positions(NullReporter())
    assert len(decisions) == 1 and decisions[0]["strategy"] == "pcs"
    assert decisions[0]["position_id"] == result.position_id
    assert decisions[0]["action"] == "hold"
    marks = paper.list_marks(result.position_id)
    assert len(marks) == 1 and marks.iloc[0]["mark"] == pytest.approx(1.5)
    assert marks.iloc[0]["source"] == "pipeline"


# --- PCS backtest ----------------------------------------------------------------------

def _gbm(days=6000, vol=0.20, seed=11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dt_ = 1 / 252
    r = rng.normal(-0.5 * vol * vol * dt_, vol * np.sqrt(dt_), days)
    close = 100 * np.exp(np.concatenate([[0.0], np.cumsum(r)]))
    return pd.DataFrame({"date": pd.bdate_range("1990-01-01", periods=days + 1),
                         "close": close})


def test_backtest_has_no_edge_when_iv_equals_rv_and_costs_are_off():
    """With IV = RV, no skew, no slippage and zero rates, holding to expiry is
    a fair game: the mean gross P&L per trade sits within noise of zero."""
    params = pb.PCSParams(vol_risk_premium=1.0, skew_per_sd=0.0, slippage_fraction=0.0,
                          rate=0.0, profit_target_pct=None, loss_stop_multiple=None,
                          time_stop_dte=None, min_credit=0.0, width_pct=0.05, dte=30)
    trades = pb.simulate(_gbm(), "GBM", params)
    gross = trades["credit"] - trades["exit_debit"]
    assert len(trades) > 200
    assert abs(gross.mean()) < 3 * gross.std() / np.sqrt(len(gross))
    assert set(trades["exit_reason"]) <= {"expiry", "max_loss"}


def test_backtest_rules_fire_as_specified():
    params = pb.PCSParams(slippage_fraction=0.0, profit_target_pct=50, loss_stop_multiple=2.0,
                          time_stop_dte=21, dte=45, close_on_breach=True)
    trades = pb.simulate(_gbm(3000), "GBM", params)
    reasons = set(trades["exit_reason"])
    assert {"target", "time_stop"} <= reasons
    hits = trades[trades["exit_reason"] == "target"]
    assert (hits["max_profit_pct"] >= 0.5 - 1e-9).all()
    assert (trades["days_held"] <= 45 - 21 + 1).all()             # the time stop caps holding
    summary = pb.summarise(trades)
    assert summary["n_trades"] == len(trades)
    assert summary["pct_target"] + summary["pct_time_stop"] + summary["pct_loss_stop"] \
        + summary["pct_breach"] + summary["pct_expiry"] + summary["pct_max_loss"] \
        == pytest.approx(1.0)


def test_grid_and_walk_forward_on_synthetic_history():
    assert len(pb.grid()) == 2 * 4 * 3 * 4 * 2 * 2 * 2
    daily = _gbm(252 * 9)
    sets = pb.grid(short_delta=(-0.2, -0.3), width_pct=(0.03,), profit_target_pct=(50,),
                   loss_stop_multiple=(None,), time_stop_dte=(None,), dte=(30,),
                   close_on_breach=(False,))
    assert len(sets) == 2
    result = pb.walk_forward(daily, "GBM", sets, min_train_trades=5, min_test_trades=3)
    assert result["n_folds"] >= 2
    assert {"in_sample", "out_of_sample", "baseline_out_of_sample"} <= set(result["folds"])
    assert result["verdict"]


def test_validation_script_observes_spreads():
    path = ROOT / "scripts" / "validate_prob_engine.py"
    spec = importlib.util.spec_from_file_location("validate_prob_engine_t15", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from analytics.strategies.base import Leg, Position
    position = Position("pcs", "X", [Leg("put", "short", 100, "x", iv=0.2),
                                     Leg("put", "long", 95, "x", iv=0.2)], 1.0)
    crash = module.observe(np.linspace(104, 90, 21), position, 30, 0.0)
    assert crash["obs_max_loss"] == 1.0 and crash["obs_hit_100"] == 0.0
    calm = module.observe(np.full(21, 110.0), position, 30, 0.0)
    assert calm["obs_max_loss"] == 0.0 and calm["obs_hit_100"] == 1.0
    assert calm["obs_hit_50"] == 1.0


# --- Wheel backtest: price basis plus dividends -----------------------------------------

def test_load_daily_can_carry_the_dividend_column(tmp_path, monkeypatch):
    import data_sources.yfinance_sync as ys
    path = tmp_path / "universe_daily.duckdb"
    monkeypatch.setattr(ys, "db_universe_daily", lambda: path)
    con = duckdb.connect(str(path))
    con.execute(ys.RAW_SCHEMA)
    dates = pd.bdate_range("2025-01-02", periods=30)
    frame = pd.DataFrame({"ticker": "DIV", "date": dates.date, "open": 50.0, "high": 50.5,
                          "low": 49.5, "close": 50.0, "adj_close": 50.0, "volume": 1e6,
                          "dividends": [0.0] * 20 + [0.4] + [0.0] * 9, "splits": 0.0})
    con.register("f", frame)
    con.execute(f"INSERT INTO {ys.RAW_TABLE} SELECT {', '.join(ys.RAW_COLUMNS)} FROM f")
    con.close()
    assert "dividends" not in ys.load_daily("DIV").columns
    with_div = ys.load_daily("DIV", with_dividends=True)
    assert with_div["dividends"].sum() == pytest.approx(0.4)
    assert with_div.attrs["price_basis"] == "price"


def _wheel_daily() -> pd.DataFrame:
    """Flat, then a drop that forces assignment, with a quarterly-ish dividend."""
    rng = np.random.default_rng(4)
    n = 700
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, n)))
    close[300:] *= 0.80
    dividends = np.zeros(n)
    dividends[::40] = 0.50
    return pd.DataFrame({"date": pd.bdate_range("2020-01-01", periods=n), "close": close,
                         "dividends": dividends})


def test_wheel_credits_dividends_only_while_shares_are_held():
    from analytics.wheel_backtest import WheelParams, compare_to_buy_and_hold, run_wheel
    daily = _wheel_daily()
    paid = run_wheel(daily, "D", WheelParams())
    unpaid = run_wheel(daily, "D", WheelParams(credit_dividends=False))
    cycles = paid.cycles
    assert paid.summary["total_dividends"] > 0 and unpaid.summary["total_dividends"] == 0
    assert (cycles.loc[~cycles["assigned"], "dividends"] == 0).all()
    assert paid.summary["total_pnl"] - unpaid.summary["total_pnl"] == pytest.approx(
        paid.summary["total_dividends"])
    # Buy-and-hold on price bars adds the same dividends back.
    with_div = compare_to_buy_and_hold(daily, paid)
    without = compare_to_buy_and_hold(daily.drop(columns="dividends"), paid)
    assert with_div["buy_and_hold_total"] > without["buy_and_hold_total"]


# --- Default spread shape (Tom, 2026-09-28): 4% of spot, 45 DTE -------------------------

def test_default_request_builds_spreads_at_45_dte_and_4pct_wide():
    from analytics.scan_request import ScanRequest
    req = ScanRequest.default(strategies=["csp", "pcs"])
    assert req.spread_width_pct == [4.0] and req.pcs_dte_targets == [45]
    assert req.pcs_widths(700.0) == [pytest.approx(28.0)]
    tol = req.pcs_tolerance
    assert req.accepts_dte(45 + tol, "pcs") and not req.accepts_dte(7, "pcs")
    assert req.accepts_dte(7, "csp") and not req.accepts_dte(45, "csp")
    assert req.accepts_dte(7) and req.accepts_dte(45)             # any requested strategy
    assert req.dte_window() == (req.dte_min, 45 + tol)            # capture covers both
    # Only the listed expiration nearest 45 is built (monthlies-only names too).
    assert req.nearest_pcs_dtes([4, 11, 25, 39, 53, 88]) == {39}
    assert req.nearest_pcs_dtes([4, 11]) == set()
    assert req.dte_window("csp") == (req.dte_min, req.dte_max)
    assert "PCS 45 DTE" in req.label()
    assert ScanRequest.default(strategies=["pcs"]).reference_dte() == 45.0


def test_explicit_requests_keep_dollar_widths_and_the_shared_window():
    """Since Phase 19 a request that leaves spread_width_pct / pcs_dte_targets
    out inherits scan_defaults' 4% / 45 DTE; dollars and the shared window
    need them spelled out as null."""
    from analytics.scan_request import RequestError, ScanRequest
    partial = ScanRequest.from_dict({"strategies": ["pcs"], "dte_min": 30, "dte_max": 45,
                                     "spread_widths": [5, 10]})
    assert partial.spread_width_pct == [4.0] and partial.pcs_dte_targets == [45]
    assert {"spread_width_pct", "pcs_dte_targets"} <= set(partial.inherited)
    req = ScanRequest.from_dict({"strategies": ["pcs"], "dte_min": 30, "dte_max": 45,
                                 "spread_widths": [5, 10], "spread_width_pct": None,
                                 "pcs_dte_targets": None})
    assert req.spread_width_pct is None and req.pcs_dte_targets is None
    assert req.pcs_widths(700.0) == [5.0, 10.0]
    assert req.accepts_dte(35, "pcs") and req.dte_window("pcs") == (30, 45)
    with pytest.raises(RequestError, match="percents of spot"):
        ScanRequest.from_dict({"strategies": ["pcs"], "spread_width_pct": [0]})


# --- Pages ------------------------------------------------------------------------------

def _app(page: str):
    from streamlit.testing.v1 import AppTest
    return AppTest.from_file(str(ROOT / "app" / "pages" / page), default_timeout=300)


def test_decisions_and_portfolio_render_a_multi_leg_book(ledger):
    paper.accept(PCS, actual_fill=1.95)
    paper.accept(CSP, actual_fill=0.18)
    at = _app("1_Decisions.py")
    at.run()
    assert not at.exception, [e.message for e in at.exception]
    assert any(e.label == "Roll a position" for e in at.expander)
    at = _app("4_Portfolio.py")
    at.run()
    assert not at.exception, [e.message for e in at.exception]
    assert any(s.value == "Open book" for s in at.subheader)


def test_management_plan_follows_the_spread_rules(rules):
    from analytics import trade_detail as td
    row = {"strategy": "pcs", "ticker": "SPY", "dte_calendar": 45, "modelled_fill": 2.0,
           "contracts": 2, "long_strike": 690.0, "max_loss": 1600.0,
           "headline_policy": "close_50"}
    plan = {p["rule"]: p["detail"] for p in td.management_plan(row)}
    assert plan["Time stop"].startswith("close at 21 DTE (day 24)")
    assert "$6.00" in plan["Loss stop"] and "2x the credit" in plan["Loss stop"]
    assert "at most 1 time(s)" in plan["Roll trigger"]
    rules["time_stop_dte"] = None
    rules["loss_stop_multiple"] = None
    plan = {p["rule"]: p["detail"] for p in td.management_plan(row)}
    assert plan["Time stop"].startswith("none") and "Loss stop" not in plan
