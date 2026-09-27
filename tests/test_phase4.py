"""
Phase 4 tests: the call side, the roll engine, and the wheel-cycle backtest.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics import covered_call, moves  # noqa: E402
from analytics.wheel_backtest import (WheelParams, _round_strike,  # noqa: E402
                                        _strike_for_delta, compare_to_buy_and_hold,
                                        run_wheel, sweep)


def _daily(n=2000, seed=11, drift=0.0003, vol=0.014, start=60.0):
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    dates = pd.bdate_range("2015-01-01", periods=n)
    return pd.DataFrame({"date": dates, "open": close, "close": close,
                          "high": close * 1.007, "low": close * 0.993,
                          "volume": np.full(n, 5_000_000.0)})


# --- Upside probabilities -------------------------------------------------

def test_call_touch_dominates_call_assignment():
    frame = _daily()
    spot = float(frame["close"].iloc[-1])
    up = moves.upside_probabilities(frame, "T", spot, spot * 1.04, 7,
                                     vol_conditioned=False)
    assert up is not None
    assert up.prob_touch >= up.prob_called_away


def test_call_probability_falls_as_strike_rises():
    frame = _daily()
    spot = float(frame["close"].iloc[-1])
    near = moves.upside_probabilities(frame, "T", spot, spot * 1.01, 7,
                                       vol_conditioned=False)
    far = moves.upside_probabilities(frame, "T", spot, spot * 1.10, 7,
                                      vol_conditioned=False)
    assert near.prob_called_away > far.prob_called_away


def test_call_probabilities_are_complementary():
    frame = _daily()
    spot = float(frame["close"].iloc[-1])
    up = moves.upside_probabilities(frame, "T", spot, spot * 1.03, 7,
                                     vol_conditioned=False)
    assert up.prob_called_away + up.prob_expires_worthless == pytest.approx(1.0)


# --- Covered calls: the basis rule ----------------------------------------

def _call_row(strike, bid=0.40, ask=0.46, delta=0.25, oi=2000, iv=0.30,
               expiration="2026-09-04"):
    return pd.Series({"strike_price": strike, "expiration": pd.Timestamp(expiration),
                       "call_bid": bid, "call_ask": ask, "call_mark": (bid + ask) / 2,
                       "call_delta": delta, "call_iv": iv, "call_open_interest": oi})


def test_call_below_basis_is_rejected_by_default():
    frame = _daily()
    spot = float(frame["close"].iloc[-1])
    result = covered_call.evaluate_call("T", _call_row(spot * 0.95), spot,
                                         basis=spot, shares=500, daily=frame)
    assert result is not None
    assert not result.accepted
    assert not result.above_basis
    assert any("BELOW your" in r for r in result.rejections)


def test_call_below_basis_states_the_exact_loss_when_allowed():
    frame = _daily()
    spot = float(frame["close"].iloc[-1])
    basis = spot
    strike = spot * 0.95
    result = covered_call.evaluate_call("T", _call_row(strike), spot, basis=basis,
                                         shares=500, daily=frame,
                                         allow_below_basis=True)
    assert result.accepted
    assert result.locked_in_loss == pytest.approx((strike - basis) * 100 * 5)
    assert any("realises a" in w for w in result.warnings)


def test_call_above_basis_is_accepted():
    frame = _daily()
    spot = float(frame["close"].iloc[-1])
    result = covered_call.evaluate_call("T", _call_row(spot * 1.05), spot,
                                         basis=spot * 0.95, shares=200, daily=frame)
    assert result.accepted and result.above_basis
    assert result.locked_in_loss == 0.0


def test_called_away_gain_includes_the_stock_leg():
    """The put side treats assignment as loss; the call side must count the
    stock move from basis to strike, or the wheel never looks profitable."""
    frame = _daily()
    spot = float(frame["close"].iloc[-1])
    basis = spot * 0.90
    strike = spot * 1.05
    result = covered_call.evaluate_call("T", _call_row(strike), spot, basis=basis,
                                         shares=100, daily=frame)
    assert result.gain_if_called > result.net_credit


def test_partial_lot_under_one_contract_is_skipped():
    frame = _daily()
    spot = float(frame["close"].iloc[-1])
    assert covered_call.evaluate_call("T", _call_row(spot * 1.05), spot,
                                       basis=spot, shares=60, daily=frame) is None


def test_contracts_track_whole_hundreds_only():
    frame = _daily()
    spot = float(frame["close"].iloc[-1])
    result = covered_call.evaluate_call("T", _call_row(spot * 1.05), spot,
                                         basis=spot * 0.9, shares=350, daily=frame)
    assert result.contracts == 3


# --- Wheel backtest -------------------------------------------------------

def test_strike_for_delta_round_trips():
    """Solving for a delta and re-pricing must reproduce it."""
    spot, vol, rate, days = 100.0, 0.30, 0.045, 7
    strike = _strike_for_delta(spot, -0.25, days, vol, rate, "put")
    from scipy.stats import norm
    t = days / 252.0
    d1 = (np.log(spot / strike) + (rate + 0.5 * vol ** 2) * t) / (vol * np.sqrt(t))
    assert norm.cdf(d1) - 1.0 == pytest.approx(-0.25, abs=1e-6)


def test_strikes_snap_to_listed_increments():
    assert _round_strike(11.3) == 11
    assert _round_strike(62.4) == 62.5
    assert _round_strike(305.7) == 306


def test_wheel_produces_cycles_not_trades():
    result = run_wheel(_daily(), "T", WheelParams())
    assert not result.cycles.empty
    assert set(result.cycles["outcome"]) <= {"expired", "called_away", "abandoned"}


def test_assignment_extends_the_cycle_beyond_the_put_dte():
    """The core correction: an assigned cycle keeps consuming capital. The old
    harness ended the trade at expiry and could not see this."""
    result = run_wheel(_daily(seed=5, drift=-0.0002), "T", WheelParams())
    table = result.cycles
    assigned = table[table["assigned"]]
    clean = table[~table["assigned"]]
    if len(assigned) and len(clean):
        assert assigned["trading_days"].mean() > clean["trading_days"].mean()


def test_cycles_never_overlap():
    """Capital-days are only meaningful if one cycle finishes before the next
    starts."""
    table = run_wheel(_daily(), "T", WheelParams()).cycles
    ends = pd.to_datetime(table["end_date"]).to_numpy()
    starts = pd.to_datetime(table["start_date"]).to_numpy()
    assert (starts[1:] > ends[:-1]).all()


def test_every_cycle_is_charged_fees():
    table = run_wheel(_daily(), "T", WheelParams()).cycles
    assert (table["fees"] > 0).all()


def test_never_below_basis_is_enforced_in_the_simulation():
    """Turning the rule off must change the results, or it is not being applied."""
    frame = _daily(seed=9, drift=-0.0004)
    strict = run_wheel(frame, "T", WheelParams(never_below_basis=True)).summary
    loose = run_wheel(frame, "T", WheelParams(never_below_basis=False)).summary
    assert strict != loose


def test_capital_days_reflect_collateral_and_duration():
    table = run_wheel(_daily(), "T", WheelParams()).cycles
    row = table.iloc[0]
    assert row["capital_days"] == pytest.approx(row["collateral"] * row["trading_days"])


def test_abandoned_cycles_are_marked_to_market():
    """An unclosed cycle must not flatter results by simply never resolving."""
    table = run_wheel(_daily(seed=21, drift=-0.0012), "T",
                       WheelParams(max_cycle_days=40)).cycles
    abandoned = table[table["outcome"] == "abandoned"]
    if len(abandoned):
        assert (abandoned["stock_pnl"] != 0).any()


def test_insufficient_history_reports_an_error_not_a_crash():
    result = run_wheel(_daily(30), "T", WheelParams())
    assert result.summary.get("error")


def test_sweep_ranks_by_capital_efficiency():
    grid = sweep(_daily(1200), "T", put_deltas=(-0.20, -0.30), put_dtes=(7,),
                  call_deltas=(0.25,))
    assert len(grid) == 2
    values = grid["annualised_on_capital_deployed"].to_numpy()
    assert values[0] >= values[1]


def test_buy_and_hold_comparison_is_reported():
    frame = _daily()
    result = run_wheel(frame, "T", WheelParams())
    comparison = compare_to_buy_and_hold(frame, result)
    assert "buy_and_hold_annualised" in comparison
    assert "wheel_beats_buy_and_hold" in comparison
