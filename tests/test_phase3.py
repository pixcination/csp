"""
Phase 3 tests: portability, portfolio-scale sizing, EV candidates, paper book.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics import candidates, paper, sizing  # noqa: E402
from core import paths  # noqa: E402


# --- Portability ----------------------------------------------------------

def test_relative_paths_resolve_inside_the_project():
    resolved = paths.resolve("vendor/tastytrade")
    assert resolved.is_absolute()
    assert paths.project_root() in resolved.parents


def test_absolute_paths_are_left_alone():
    """The external archive is the one legitimate absolute path."""
    target = "/mnt/archive/1m" if sys.platform != "win32" else "D:/pricing_data"
    assert str(paths.resolve(target)).replace("\\", "/").endswith(
        target.replace("\\", "/"))


def test_empty_path_resolves_to_none():
    assert paths.resolve(None) is None
    assert paths.resolve("") is None


def test_external_archive_is_optional_by_default():
    """Absent and not required must not raise -- that is what makes the
    project movable to another machine."""
    assert paths.pricing_data_required() is False
    paths.pricing_data_root()  # must not raise


def test_intraday_store_is_inside_the_project():
    from data_sources.massive_sync import archive_root
    assert paths.project_root() in archive_root().parents


def test_no_module_reads_config_from_the_working_directory(tmp_path, monkeypatch):
    before = paths.config_path()
    monkeypatch.chdir(tmp_path)
    assert paths.config_path() == before


# --- Portfolio-scale sizing ----------------------------------------------

def test_every_universe_name_fits_at_three_million():
    assert sizing.max_tradable_strike() >= 900


def test_position_limit_is_a_gate_not_a_contract_count():
    """Allowing 25 more positions must not silently cap the trade at 25
    contracts -- mixing the units is how a $3M book gets sized like a $50k one."""
    result = sizing.max_contracts_for_strike(
        11.0, open_interest=500_000, option_volume=200_000, adv_dollars=5e9)
    assert result.contracts > 25
    assert result.binding_constraint != "position_count"


def test_open_interest_caps_size():
    # 3% of 500 = 15 contracts, below the ~26 the position cap would allow.
    result = sizing.max_contracts_for_strike(
        45.0, open_interest=500, option_volume=100_000, adv_dollars=1e10)
    assert result.binding_constraint == "open_interest"
    assert result.contracts == 15
    assert not result.scales


def test_option_volume_caps_size():
    result = sizing.max_contracts_for_strike(
        45.0, open_interest=100_000, option_volume=150, adv_dollars=1e10)
    assert result.binding_constraint == "option_volume"
    assert result.contracts == 15          # 10% of 150


def test_thin_strike_is_rejected_outright():
    result = sizing.max_contracts_for_strike(45.0, open_interest=100, option_volume=5)
    assert result.rejected
    assert any("open interest" in r for r in result.reasons)


def test_zero_volume_falls_back_to_open_interest():
    """Off-hours snapshots report zero volume; that is a stale field, not a
    dead contract."""
    result = sizing.max_contracts_for_strike(
        45.0, open_interest=4000, option_volume=0, adv_dollars=1e10)
    assert result.contracts > 0


def test_capital_still_binds_when_liquidity_is_deep():
    result = sizing.max_contracts_for_strike(
        45.0, open_interest=1_000_000, option_volume=500_000, adv_dollars=1e12)
    assert result.binding_constraint in ("position_cap", "ticker_cap", "cash")
    assert result.scales


def test_sizing_explains_itself():
    result = sizing.max_contracts_for_strike(45.0, open_interest=2000,
                                              option_volume=90_000, adv_dollars=1e10)
    assert "capped by" in result.explain()


# --- Candidates -----------------------------------------------------------

def _daily(n=1200, seed=3):
    rng = np.random.default_rng(seed)
    close = 45 * np.exp(np.cumsum(rng.normal(0.0002, 0.014, n)))
    dates = pd.bdate_range("2020-01-01", periods=n)
    return pd.DataFrame({"date": dates, "open": close, "close": close,
                          "high": close * 1.008, "low": close * 0.992,
                          "volume": np.full(n, 8_000_000.0)})


def test_basis_assessment_flags_a_bad_entry_price():
    frame = _daily()
    high = float(frame["close"].tail(252).max())
    quality, note = candidates.basis_assessment(frame, high * 1.02)
    assert quality in ("poor", "fair")
    assert note


def test_basis_assessment_likes_a_cheap_entry():
    frame = _daily()
    low = float(frame["close"].tail(252).min())
    quality, _ = candidates.basis_assessment(frame, low * 0.98)
    assert quality in ("good", "fair")


def test_basis_assessment_admits_ignorance_on_short_history():
    quality, _ = candidates.basis_assessment(_daily(50), 45.0)
    assert quality == "unknown"


def test_adv_uses_dollars_not_shares():
    frame = _daily()
    adv = candidates._adv_dollars(frame)
    assert adv > 1e8


def test_rank_key_sorts_rejected_candidates_last():
    good = candidates.Recommendation.__new__(candidates.Recommendation)
    object.__setattr__(good, "accepted", True)
    object.__setattr__(good, "ev_annualised", 0.05)
    bad = candidates.Recommendation.__new__(candidates.Recommendation)
    object.__setattr__(bad, "accepted", False)
    object.__setattr__(bad, "ev_annualised", 9.99)
    assert good.rank_key > bad.rank_key


# --- Paper book -----------------------------------------------------------

@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr("analytics.paper.db_trade_log", lambda: tmp_path / "t.duckdb")
    paper.ensure_schema()
    return tmp_path


REC = {"ticker": "F", "strike": 11.0, "expiration": "2026-08-28",
        "modelled_fill": 0.18, "contracts": 20, "prob_otm_empirical": 0.87,
        "expected_value": 246.0, "ev_annualised": 0.19, "iv_rv_ratio": 1.22,
        "sample_label": "last 10y", "rationale": "..."}


def test_accept_records_the_recommendation_alongside_the_fill(book):
    result = paper.accept(REC, actual_fill=0.17)
    assert result.contracts == 20
    assert result.slippage == pytest.approx(-0.01)
    row = paper.list_positions().iloc[0]
    assert row["rec_prob_otm"] == pytest.approx(0.87)
    assert row["actual_fill"] == pytest.approx(0.17)


def test_size_override_is_honoured(book):
    result = paper.accept(REC, contracts=5)
    assert result.contracts == 5


def test_assumed_fills_are_excluded_from_calibration(book):
    """A modelled fill entered as if it were real would just confirm the
    assumption it is supposed to test."""
    paper.accept(REC)                      # no actual_fill given
    assert paper.slippage_report()["n"] == 0
    paper.accept({**REC, "ticker": "T"}, actual_fill=0.20)
    assert paper.slippage_report()["n"] == 1


def test_assignment_creates_a_share_lot_with_adjusted_basis(book):
    result = paper.accept(REC, actual_fill=0.20)
    paper.close_position(result.position_id, "assigned")
    lots = paper.list_share_lots()
    assert len(lots) == 1
    assert lots.iloc[0]["shares"] == 2000            # 20 contracts
    assert lots.iloc[0]["adjusted_basis"] == pytest.approx(10.80)  # 11.00 - 0.20


def test_expiry_closes_the_cycle_and_charges_no_exit_fee(book):
    result = paper.accept(REC, actual_fill=0.18)
    paper.close_position(result.position_id, "expired_otm")
    row = paper.list_positions().iloc[0]
    assert row["status"] == "expired_otm"
    assert row["exit_fees"] == 0.0


def test_performance_is_net_of_every_fee(book):
    result = paper.accept(REC, actual_fill=0.20)
    paper.close_position(result.position_id, "expired_otm")
    stats = paper.performance()
    gross = 0.20 * 100 * 20
    assert stats["total_realized"] < gross


def test_calibration_refuses_to_judge_a_tiny_sample(book):
    paper.accept(REC, actual_fill=0.18)
    assert "not enough" in paper.calibration()["verdict"]


def test_invalid_status_is_rejected(book):
    result = paper.accept(REC, actual_fill=0.18)
    with pytest.raises(ValueError):
        paper.close_position(result.position_id, "vaporised")
