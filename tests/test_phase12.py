"""
Phase 12: expected move, liquidity, the strategies package (CSP port and
PCS construction), multi-leg fees and premium flags.
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics import candidates, costs, expected_move as em, liquidity, regime, sizing  # noqa: E402
from analytics.scan_request import ScanRequest  # noqa: E402

GOLDEN = ROOT / "tests" / "fixtures" / "csp_golden"


# --- CSP port: identical numbers to the pre-Phase-12 code ------------------------

def test_csp_port_reproduces_the_pre_port_output():
    """Frozen 2026-09-27 from the pre-port `candidates.evaluate_strike` on real
    chain rows and bars (5 tickers, 21 strikes). Depends on the account,
    liquidity, cost and entry settings in config.yaml as of that date."""
    golden = json.loads((GOLDEN / "golden.json").read_text())
    today = dt.date.fromisoformat(golden["today"])
    rows = pd.read_parquet(GOLDEN / "chain_rows.parquet")
    bars = pd.read_parquet(GOLDEN / "daily.parquet")
    reading = regime.RegimeReading(None, 15, 12, 17, 90, 140, 0.71, "calm", 1.0, True, "calm", "")
    account = sizing.AccountState(net_liquidating_value=3_000_000, cash_available=3_000_000)
    cfg = candidates.load_config()
    produced = []
    for ticker, meta in golden["meta"].items():
        daily = bars[bars["_ticker"] == ticker].drop(columns="_ticker")
        for _, row in rows[rows["_ticker"] == ticker].iterrows():
            rec = candidates.evaluate_strike(
                ticker, row, meta["spot"], daily, meta["adv"], account, cfg, reading,
                context={}, today=today,
                metrics={"ivr": 0.3, "ivp": 0.4, "iv_index": 0.25, "liquidity_rating": 4})
            if rec is not None:
                produced.append(rec.to_dict())
    assert len(produced) == len(golden["rows"])
    keys = ["strike", "contracts", "modelled_fill", "net_credit", "fees", "collateral",
            "expected_value", "ev_annualised", "prob_otm_empirical", "prob_touch",
            "iv_rv_ratio", "accepted", "binding_constraint"]
    for new, old in zip(produced, golden["rows"]):
        for key in keys:
            a, b = new[key], old[key]
            if isinstance(b, float):
                assert a == pytest.approx(b, rel=1e-12, abs=1e-12), (old["_ticker"], key)
            else:
                assert a == b, (old["_ticker"], key)
        assert list(new["rejections"]) == list(old["rejections"])


# --- Expected move: hand calculations ------------------------------------------

def test_iv_expected_move_by_hand():
    # 100 x 0.20 x sqrt(30/365) = 5.7338
    assert em.iv_move(100.0, 0.20, 30) == pytest.approx(100 * 0.2 * math.sqrt(30 / 365))
    assert em.iv_move(100.0, 0.20, 30) == pytest.approx(5.7338, abs=1e-4)
    assert em.iv_move(250.0, 0.40, 365) == pytest.approx(100.0)
    assert math.isnan(em.iv_move(100.0, 0.0, 30))


def test_straddle_and_tastytrade_moves_by_hand():
    assert em.straddle_move(6.0) == pytest.approx(5.10)                 # 0.85 x 6
    # 0.6 x 6 + 0.3 x 3 + 0.1 x 1.2 = 3.6 + 0.9 + 0.12 = 4.62
    assert em.tasty_move(6.0, 3.0, 1.2) == pytest.approx(4.62)
    # a missing strangle: weights renormalise over what exists, (0.6 x 6 + 0.3 x 3) / 0.9
    assert em.tasty_move(6.0, 3.0, None) == pytest.approx(4.5 / 0.9)
    assert em.distance_em(94.0, 100.0, 4.0) == pytest.approx(-1.5)


def _chain_exp(spot=100.0, dte=30, vol=0.2, step=1.0, today=dt.date(2026, 9, 28)):
    """A Black-Scholes-priced chain for one expiration, zero-width quotes."""
    from analytics.options_math import bs_price_greeks
    rows = []
    exp = today + dt.timedelta(days=dte)
    for k in np.arange(spot - 20, spot + 20 + step, step):
        put = bs_price_greeks(spot, k, dte, vol, 0.0, "put")
        call = bs_price_greeks(spot, k, dte, vol, 0.0, "call")
        rows.append({"expiration": exp, "strike_price": float(k),
                     "put_bid": put.price, "put_ask": put.price, "put_mark": put.price,
                     "call_bid": call.price, "call_ask": call.price, "call_mark": call.price,
                     "put_delta": put.delta, "put_iv": vol, "put_open_interest": 5000,
                     "put_volume": 500, "put_theta": put.theta, "put_gamma": put.gamma,
                     "put_vega": put.vega})
    return pd.DataFrame(rows)


def test_expected_move_from_a_bs_chain():
    frame = _chain_exp()
    move = em.for_expiration(frame, 100.0, 30, method="iv")
    assert move.atm_strike == 100.0
    assert move.atm_iv == pytest.approx(0.20, abs=0.002)
    assert move.em_iv == pytest.approx(em.iv_move(100.0, 0.20, 30), rel=0.01)
    # ATM straddle ~ 0.798 x sigma x sqrt(T) x S, so 0.85 x straddle ~ 0.68 sigma
    assert move.em_straddle / move.em_iv == pytest.approx(0.85 * 0.7979, rel=0.02)
    assert move.em == move.em_iv
    assert move.distance(100 - move.em) == pytest.approx(-1.0)


def test_containment_labels_its_source():
    rng = np.random.default_rng(3)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 1500)))
    daily = pd.DataFrame({"date": pd.bdate_range("2019-01-01", periods=1500), "open": close,
                          "high": close * 1.005, "low": close * 0.995, "close": close,
                          "volume": 1e6})
    result = em.containment(daily, 10, lookback_years=None)
    assert "RV proxy" in result.source
    assert 0.55 < result.within_1x < 0.80 and result.within_2x > 0.90


# --- Positions ---------------------------------------------------------------------

def _pcs(credit=1.5, short=100.0, long=95.0):
    from analytics.strategies.base import Leg, Position
    return Position("pcs", "T", [Leg("put", "short", short, "2026-11-20", delta=-0.30,
                                     theta=-0.05, gamma=0.02, vega=0.10),
                                 Leg("put", "long", long, "2026-11-20", delta=-0.15,
                                     theta=-0.03, gamma=0.01, vega=0.07)], credit)


def test_pcs_payoff_max_loss_breakeven_bpr():
    pos = _pcs()
    assert pos.payoff(120.0) == pytest.approx(1.5)            # both expire worthless
    assert pos.payoff(98.0) == pytest.approx(1.5 - 2.0)       # short ITM by 2
    assert pos.payoff(80.0) == pytest.approx(1.5 - 5.0)       # max loss
    assert pos.max_profit == pytest.approx(1.5)
    assert pos.max_loss == pytest.approx(3.5)
    assert pos.breakevens == [pytest.approx(98.5)]
    assert pos.collateral == pytest.approx(350.0)             # BPR = max loss x 100
    assert pos.width == 5.0
    greeks = pos.net_greeks()
    assert greeks["delta"] == pytest.approx((0.30 - 0.15) * 100)
    assert greeks["theta"] == pytest.approx((0.05 - 0.03) * 100)


def test_csp_position_uses_cash_secured_collateral():
    from analytics.strategies.base import Leg, Position
    pos = Position("csp", "T", [Leg("put", "short", 50.0, "2026-10-02")], 0.40,
                   collateral_per_contract=5000.0)
    assert pos.max_loss == pytest.approx(49.60)
    assert pos.collateral == 5000.0 and pos.breakevens == [pytest.approx(49.60)]


def test_position_value_is_bs_before_expiry_and_intrinsic_after():
    pos = _pcs()
    for leg in pos.legs:
        leg.iv = 0.25
    assert pos.value(90.0, 0) == pytest.approx(-10.0 + 5.0)
    assert -5.0 < pos.value(100.0, 30) < 0.0


# --- Fees and fills ----------------------------------------------------------------

def test_vertical_fees_charge_each_leg_and_every_exit():
    econ = costs.vertical_economics(100, 95, 1.5, 2, 35)
    single = costs.option_open(2, "sell").total + costs.option_open(2, "buy").total
    assert econ.entry_fees == pytest.approx(single)
    assert econ.max_loss == pytest.approx((5 - 1.5) * 100 * 2)
    assert econ.exit_fees["expire_otm"] == 0.0
    assert econ.exit_fees["max_loss"] > econ.exit_fees["short_itm"] > econ.exit_fees["close"]
    cash = costs.vertical_economics(100, 95, 1.5, 2, 35, cash_settled=True)
    assert cash.exit_fees["max_loss"] == pytest.approx(2 * costs.assignment(2).total)
    # the commission cap is per leg: 20 contracts -> $10 per leg, not $20
    big = costs.legs_open([("sell", 20), ("buy", 20)])
    assert big.commission == pytest.approx(20.0)


def test_package_fill_between_natural_and_mid():
    fill = costs.package_fill(1.20, 1.30, 0.50, 0.56, fraction=0.40)
    assert fill["net_mid"] == pytest.approx(0.72)
    assert fill["natural"] == pytest.approx(0.64)
    assert fill["modelled"] == pytest.approx(0.72 - 0.4 * 0.08)
    assert costs.package_fill(1.2, 1.1, 0.5, 0.56) is None     # crossed quote


# --- Liquidity -----------------------------------------------------------------------

def test_fillability_and_weakest_leg():
    tight = liquidity.fillability(1.00, 1.02, 10_000, 3_000)
    wide = liquidity.fillability(0.10, 0.30, 20, 0)
    assert tight > 0.9 and wide < 0.3
    penny = liquidity.fillability(0.03, 0.05, 1000, 100)       # 50% wide, but a 2-cent market
    assert penny > liquidity.fillability(3.0, 4.5, 1000, 100)
    legs = [liquidity.leg({"strike_price": 100, "put_bid": 1.0, "put_ask": 1.02,
                           "put_open_interest": 9000, "put_volume": 900}, "put"),
            liquidity.leg({"strike_price": 95, "put_bid": 0.1, "put_ask": 0.3,
                           "put_open_interest": 40, "put_volume": 0}, "put")]
    pos = liquidity.position(legs)
    assert pos.weakest.strike == 95 and pos.min_open_interest == 40


def test_oi_walls():
    frame = pd.DataFrame({"strike_price": [90, 95, 100, 105],
                          "put_open_interest": [100, 900, 120, 110]})
    found = liquidity.walls(frame, "put", multiple=3.0)
    assert found["strike"].tolist() == [95.0]
    assert liquidity.nearest_wall_below(frame, 99.0)["strike"] == 95.0


def test_sizing_caps_bind_on_the_thinner_leg():
    account = sizing.AccountState(3_000_000, 3_000_000)
    both = sizing.max_contracts_for_position(
        350.0, account, legs=[(10_000, 2_000, "short"), (1_000, 200, "long")],
        notional_per_contract=10_000)
    assert both.binding_constraint in ("open_interest", "option_volume")
    assert both.contracts == min(int(1_000 * 0.03), int(200 * 0.10))
    thin = sizing.max_contracts_for_position(350.0, account, legs=[(10_000, 2_000, "short"),
                                                                  (100, 50, "long")])
    assert thin.contracts == 0 and any("long" in r for r in thin.reasons)


# --- PCS construction ------------------------------------------------------------------

def test_short_strike_rules_and_snapping():
    from analytics.strategies import pcs
    frame = _chain_exp()
    puts = frame[frame["strike_price"] < 100]
    request = ScanRequest.from_dict({"strategies": ["pcs"], "dte_min": 25, "dte_max": 35,
                                     "delta_range": [-0.30, -0.15], "em_multiple": 1.0})
    strongest = {"level_id": "50D SMA", "level": 93.4, "median_pierce_atr": 0.5,
                 "summary": "held"}
    chosen = pcs.short_strikes(puts, 100.0, request, 5.0, strongest, atr=2.0)
    rules = {rule: strike for strike, found in chosen.items() for rule, _ in found}
    delta_row = puts.iloc[(puts["put_delta"] - (-0.225)).abs().argmin()]
    assert rules["delta"] == delta_row["strike_price"]
    assert rules["em_multiple"] == 95.0                      # <= 100 - 1 x 5
    assert rules["support"] == 92.0                          # <= 93.4 - 0.5 x 2 = 92.4
    only = pcs.short_strikes(puts, 100.0, ScanRequest.from_dict(
        {"strategies": ["pcs"], "strike_rule": "em_multiple"}), 5.0, strongest, 2.0)
    assert list(only) == [95.0]
    assert pcs.snap_long(puts, 95.0, 2.5) in (92.0, 93.0)
    assert pcs.snap_long(puts, 95.0, 5) == 90.0
    assert pcs.snap_long(puts, 81.0, 5) == 80.0               # nearest listed below
    assert pcs._tier(0, 4, ["C", "M", "A"]) == "C" and pcs._tier(3, 4, ["C", "M", "A"]) == "A"


def test_build_candidates_on_a_synthetic_chain():
    from analytics.strategies import pcs
    from analytics.strategies.context import TickerContext
    today = dt.date(2026, 9, 28)
    chain = _chain_exp(today=today)
    rng = np.random.default_rng(11)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, 3000)))
    daily = pd.DataFrame({"date": pd.bdate_range("2014-01-01", periods=3000), "open": close,
                          "high": close * 1.006, "low": close * 0.994, "close": close,
                          "volume": 5e6})
    ctx = TickerContext("TST", 100.0, today, chain)
    reading = regime.RegimeReading(None, 15, 12, 17, 90, 140, 0.71, "calm", 1.0, True, "", "")
    request = ScanRequest.from_dict({"strategies": ["pcs"], "dte_min": 25, "dte_max": 35,
                                     "strike_rule": "delta", "spread_widths": [1, 2.5, 5, 10]})
    rows = pcs.build_candidates("TST", ctx, daily, 5e8, sizing.AccountState(3e6, 3e6),
                                candidates.load_config(), reading, request, checks={},
                                today=today)
    assert len(rows) == 4                                     # one short, four widths
    assert [r.tier for r in rows] == ["Conservative", "Conservative", "Moderate", "Aggressive"]
    for r in rows:
        assert r.long_strike < r.strike and r.width == r.strike - r.long_strike
        assert r.breakeven == pytest.approx(r.strike - r.modelled_fill)
        assert r.max_loss == pytest.approx((r.width - r.modelled_fill) * 100 * max(r.contracts, 1))
        assert 0 <= r.prob_max_loss <= 1 - r.prob_otm_empirical <= r.prob_short_itm + 1e-9
        assert r.credit_width == pytest.approx(r.modelled_fill / r.width)
        assert "American" in r.notes[-1]


def test_default_choice_is_the_lowest_passing_short():
    from analytics.strategies import pcs
    frame = pd.DataFrame({"ticker": ["A"] * 3, "strategy": ["pcs"] * 3,
                          "expiration": ["2026-11-20"] * 3, "tier": ["Moderate"] * 3,
                          "strike": [95.0, 92.0, 90.0], "accepted": [True, True, False]})
    marked = pcs.mark_default_choice(frame)
    assert marked["default_choice"].tolist() == [False, True, False]


def test_trade_ids_are_unique_across_widths_and_roots():
    frame = pd.DataFrame({"strategy": ["pcs", "pcs", "pcs", "csp"],
                          "ticker": ["SPX", "SPX", "SPX", "AAPL"],
                          "expiration": ["2026-11-20"] * 4,
                          "strike": [7000.0, 7000.0, 7000.0, 330.0],
                          "long_strike": [6990.0, 6975.0, 6990.0, None],
                          "root_symbol": ["SPXW", "SPXW", "SPX", "AAPL"]})
    ids = candidates.trade_ids(frame)
    assert len(set(ids)) == 4 and ids[3] == "csp|AAPL|2026-11-20|330"


def test_paper_book_refuses_unknown_strategies():
    """Spreads are recorded since Phase 15 (tests/test_phase15.py); anything
    else the book cannot describe as legs is still refused."""
    from analytics import paper
    with pytest.raises(ValueError, match="records CSP, PCS"):
        paper.accept({"strategy": "iron_condor", "ticker": "SPY", "strike": 700,
                      "expiration": "2026-11-20", "modelled_fill": 1.0, "contracts": 1})


def test_strategy_registry_is_import_compatible():
    from analytics.strategies import STRATEGIES
    assert {"csp", "covered_call", "pcs"} <= set(STRATEGIES)
    assert ScanRequest.default().strike_rule == "conservative"


def test_snapped_width_far_from_the_request_is_warned():
    from analytics.strategies import pcs
    from analytics.strategies.context import TickerContext
    today = dt.date(2026, 9, 28)
    chain = _chain_exp(step=5.0, today=today)                 # 5-point strikes
    rng = np.random.default_rng(5)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, 3000)))
    daily = pd.DataFrame({"date": pd.bdate_range("2014-01-01", periods=3000), "open": close,
                          "high": close * 1.006, "low": close * 0.994, "close": close,
                          "volume": 5e6})
    reading = regime.RegimeReading(None, 15, 12, 17, 90, 140, 0.71, "calm", 1.0, True, "", "")
    rows = pcs.build_candidates(
        "TST", TickerContext("TST", 100.0, today, chain), daily, 5e8,
        sizing.AccountState(3e6, 3e6), candidates.load_config(), reading,
        ScanRequest.from_dict({"strategies": ["pcs"], "dte_min": 25, "dte_max": 35,
                               "strike_rule": "delta", "spread_widths": [1, 5]}),
        checks={}, today=today)
    assert [r.width for r in rows] == [5.0]                   # $1 and $5 both snap to 5: built once
    assert rows[0].requested_width == 1.0 and any("strike spacing" in w for w in rows[0].warnings)


def test_empty_ticker_list_analyses_nothing():
    assert candidates.evaluate_universe([], request=ScanRequest.default()).empty
