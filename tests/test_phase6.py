"""
Phase 6 tests: correlation clustering, marginal risk, selection, stress.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics import portfolio  # noqa: E402


@pytest.fixture
def synthetic(monkeypatch):
    """Three correlated tech names, one weakly related, one independent."""
    rng = np.random.default_rng(11)
    n = 1600
    dates = pd.bdate_range("2019-01-01", periods=n)
    market = rng.normal(0.0003, 0.011, n)
    frames = {}
    for name, beta, idio in [("TECH1", 1.00, 0.006), ("TECH2", 0.98, 0.006),
                              ("TECH3", 1.02, 0.007), ("UTIL", 0.25, 0.008),
                              ("GOLD", -0.05, 0.011)]:
        series = 100 * np.exp(np.cumsum(beta * market + rng.normal(0, idio, n)))
        frames[name] = pd.DataFrame({
            "date": dates, "open": series, "close": series,
            "high": series * 1.005, "low": series * 0.995,
            "volume": np.full(n, 5e6)})

    import data_sources.yfinance_sync as yfs
    monkeypatch.setattr(
        yfs, "load_daily_total_return",
        lambda t, start=None, end=None, allow_fallback=True:
            frames.get(t, pd.DataFrame()))
    return frames


# --- Clustering ------------------------------------------------------------

def test_correlated_names_share_a_cluster(synthetic):
    assignment = portfolio.cluster(["TECH1", "TECH2", "TECH3", "UTIL", "GOLD"])
    assert assignment["TECH1"] == assignment["TECH2"] == assignment["TECH3"]


def test_uncorrelated_names_are_separated(synthetic):
    assignment = portfolio.cluster(["TECH1", "TECH2", "TECH3", "UTIL", "GOLD"])
    assert assignment["GOLD"] != assignment["TECH1"]
    assert assignment["UTIL"] != assignment["TECH1"]


def test_unknown_ticker_gets_its_own_cluster(synthetic):
    assignment = portfolio.cluster(["TECH1", "TECH2", "__NOHISTORY__"])
    assert "__NOHISTORY__" in assignment
    assert assignment["__NOHISTORY__"] != assignment["TECH1"]


def test_thin_history_is_not_correlated(synthetic, monkeypatch):
    """A confident-looking correlation from forty overlapping days is worse
    than no correlation at all."""
    short = pd.DataFrame({
        "date": pd.bdate_range("2024-01-01", periods=40),
        "open": np.arange(40.0), "close": np.arange(40.0) + 100,
        "high": np.arange(40.0) + 101, "low": np.arange(40.0) + 99,
        "volume": np.full(40, 1e6)})
    import data_sources.yfinance_sync as yfs
    original = yfs.load_daily_total_return
    monkeypatch.setattr(
        yfs, "load_daily_total_return",
        lambda t, start=None, end=None, allow_fallback=True:
            short if t == "SHORT" else original(t))
    matrix = portfolio.correlation_matrix(["TECH1", "TECH2", "SHORT"])
    assert "SHORT" not in matrix.columns


# --- Marginal risk ---------------------------------------------------------

def test_marginal_risk_is_measured_in_dollars_not_weights(synthetic):
    """Normalised weights make every addition look risk-reducing, because the
    new name dilutes the old ones. Adding capital must add risk."""
    result = portfolio.marginal_risk(
        "TECH3", ["TECH1", "TECH2"],
        collateral={"TECH1": 100_000.0, "TECH2": 100_000.0},
        candidate_collateral=100_000.0)
    assert result.marginal_contribution > 0
    assert result.portfolio_dollar_vol_after > result.portfolio_dollar_vol_before


def test_correlated_addition_gets_little_diversification_credit(synthetic):
    held = ["TECH1", "TECH2"]
    coll = {"TECH1": 100_000.0, "TECH2": 100_000.0}
    correlated = portfolio.marginal_risk("TECH3", held, coll, 100_000.0)
    independent = portfolio.marginal_risk("GOLD", held, coll, 100_000.0)
    assert correlated.diversification_benefit < independent.diversification_benefit
    assert correlated.marginal_contribution > independent.marginal_contribution


def test_diversification_benefit_is_bounded(synthetic):
    for ticker in ("TECH3", "UTIL", "GOLD"):
        result = portfolio.marginal_risk(
            ticker, ["TECH1", "TECH2"],
            {"TECH1": 100_000.0, "TECH2": 100_000.0}, 100_000.0)
        assert 0.0 <= result.diversification_benefit <= 1.0


def test_first_position_has_no_diversification_benefit(synthetic):
    result = portfolio.marginal_risk("TECH1", [], {}, 100_000.0)
    assert result.marginal_contribution == pytest.approx(
        result.standalone_dollar_vol)


# --- Selection -------------------------------------------------------------

def _candidates():
    return pd.DataFrame([
        {"ticker": "TECH1", "collateral": 100_000.0, "ev_annualised": 0.24},
        {"ticker": "TECH2", "collateral": 100_000.0, "ev_annualised": 0.23},
        {"ticker": "TECH3", "collateral": 100_000.0, "ev_annualised": 0.22},
        {"ticker": "UTIL", "collateral": 100_000.0, "ev_annualised": 0.18},
        {"ticker": "GOLD", "collateral": 100_000.0, "ev_annualised": 0.15},
    ])


def test_selection_rejects_a_third_correlated_name(synthetic):
    result = portfolio.select(_candidates(), held=[], max_new=4)
    accepted = [a["ticker"] for a in result.accepted]
    assert "TECH3" not in accepted
    rejected = {r["ticker"]: r for r in result.rejected}
    assert "cluster" in rejected["TECH3"]["rejection_reasons"][0]


def test_selection_prefers_diversifiers_over_higher_ev(synthetic):
    """UTIL and GOLD have lower EV than TECH3 and should still be taken."""
    result = portfolio.select(_candidates(), held=[], max_new=4)
    accepted = [a["ticker"] for a in result.accepted]
    assert "UTIL" in accepted and "GOLD" in accepted


def test_rejections_keep_their_reason(synthetic):
    result = portfolio.select(_candidates(), held=[], max_new=4)
    assert all(r.get("rejection_reasons") for r in result.rejected)


def test_existing_holdings_count_toward_cluster_limits(synthetic):
    result = portfolio.select(_candidates(), held=["TECH1", "TECH2"], max_new=4)
    accepted = [a["ticker"] for a in result.accepted]
    assert "TECH3" not in accepted


def test_max_new_is_respected(synthetic):
    result = portfolio.select(_candidates(), held=[], max_new=1)
    assert len(result.accepted) == 1


def test_empty_candidates_is_not_an_error(synthetic):
    result = portfolio.select(pd.DataFrame(), held=[])
    assert result.accepted == [] and result.rejected == []


# --- Stress ----------------------------------------------------------------

def _book(frames, names):
    return [{"ticker": t, "spot": float(frames[t]["close"].iloc[-1]),
             "strike": float(frames[t]["close"].iloc[-1]) * 0.95,
             "collateral": 100_000.0} for t in names]


def test_concentrated_book_assigns_together(synthetic):
    result = portfolio.simultaneous_assignment(
        _book(synthetic, ["TECH1", "TECH2", "TECH3"]), horizon=7)
    assert result is not None
    assert result.all_assigned_ever
    assert result.worst_pct_converted == pytest.approx(1.0)


def test_diversified_book_assigns_less_together(synthetic):
    concentrated = portfolio.simultaneous_assignment(
        _book(synthetic, ["TECH1", "TECH2", "TECH3"]), horizon=7)
    diversified = portfolio.simultaneous_assignment(
        _book(synthetic, ["TECH1", "UTIL", "GOLD"]), horizon=7)
    assert diversified.p99_assigned < concentrated.p99_assigned
    assert diversified.worst_pct_converted < concentrated.worst_pct_converted


def test_stress_reports_a_date_for_the_worst_window(synthetic):
    result = portfolio.simultaneous_assignment(
        _book(synthetic, ["TECH1", "TECH2"]), horizon=7)
    assert result.worst_date


def test_stress_needs_a_book(synthetic):
    assert portfolio.simultaneous_assignment([]) is None


def test_stress_skips_positions_without_a_price(synthetic):
    book = _book(synthetic, ["TECH1"])
    book.append({"ticker": "TECH2", "spot": 0, "strike": 0, "collateral": 1.0})
    result = portfolio.simultaneous_assignment(book, horizon=7)
    assert result.positions == 1
