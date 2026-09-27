"""
Phase 5 tests: IV history rewiring, regimes, walk-forward, calibration.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics import calibration, regimes  # noqa: E402
from analytics.walkforward import walk_forward  # noqa: E402
from analytics.wheel_backtest import WheelParams  # noqa: E402


def _daily(n=3000, seed=17, drift=0.0003, vol=0.014, start=55.0, since="2006-01-01"):
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    dates = pd.bdate_range(since, periods=n)
    return pd.DataFrame({"date": dates, "open": close, "close": close,
                          "high": close * 1.007, "low": close * 0.993,
                          "volume": np.full(n, 4_000_000.0)})


# --- IV history rewiring --------------------------------------------------

def test_iv_history_reads_the_current_capture_path():
    """The Phase 2 capture writes to data/chains/<block>/. Before Phase 5 this
    module read data/stage3_chains/<date>/ and saw none of it."""
    import inspect
    from analytics import iv_history
    source = inspect.getsource(iv_history._available_blocks)
    assert "list_blocks" in source, "must read the current chains directory"
    assert "is_ingestable_for_iv_history" in source, "must filter to RTH blocks"


def test_iv_history_still_reads_the_legacy_path():
    import inspect
    from analytics import iv_history
    assert "list_snapshot_dates_for_ticker" in inspect.getsource(
        iv_history._available_blocks), "must not discard already-captured history"


def test_iv_rank_refuses_a_number_from_too_few_captures():
    from analytics.iv_history import iv_rank_and_percentile
    result = iv_rank_and_percentile("__NOSUCHTICKER__")
    assert result["iv_rank"] is None
    assert result["note"]


def test_legacy_iv_database_is_migrated_not_dropped(tmp_path, monkeypatch):
    import duckdb
    from analytics import iv_history

    path = tmp_path / "iv.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE iv_points (ticker VARCHAR, date DATE, iv DOUBLE, "
                 "put_delta DOUBLE, dte INTEGER, strike DOUBLE, spot DOUBLE, "
                 "PRIMARY KEY (ticker, date))")
    con.execute("INSERT INTO iv_points VALUES ('AAPL','2026-07-10',0.31,-0.25,9,290,309)")
    con.close()

    monkeypatch.setattr(iv_history, "db_trade_log", lambda: path, raising=False)
    con = duckdb.connect(str(path))
    iv_history._ensure_schema(con)
    rows = con.execute("SELECT ticker, block, iv, source FROM iv_points").fetchall()
    con.close()
    assert rows == [("AAPL", "2026-07-10", 0.31, "legacy")]


# --- Regimes ---------------------------------------------------------------

def test_every_date_falls_in_exactly_one_regime():
    for probe in (dt.date(2007, 6, 1), dt.date(2008, 10, 1), dt.date(2020, 3, 15),
                   dt.date(2022, 6, 1), dt.date(2026, 8, 1)):
        matches = [r for r in regimes.REGIMES if r.contains(probe)]
        assert len(matches) == 1, f"{probe} matched {len(matches)} regimes"


def test_regimes_do_not_overlap():
    ordered = sorted(regimes.REGIMES, key=lambda r: r.start)
    for earlier, later in zip(ordered, ordered[1:]):
        assert earlier.end < later.start


def test_stress_regimes_are_the_drawdowns():
    assert regimes.STRESS_REGIMES == {"gfc", "vol_2018", "covid", "bear_2022"}


def test_scorecard_splits_one_simulation_rather_than_running_many():
    """Running each regime independently would restart the wheel at every
    boundary and discard the cycles that straddle them -- which are the
    interesting ones."""
    import inspect
    source = inspect.getsource(regimes.scorecard)
    assert source.count("run_wheel") == 1


def test_scorecard_produces_regime_rows():
    table = regimes.scorecard(_daily(), "T", WheelParams(), min_cycles=3)
    assert not table.empty
    assert {"regime", "annualised", "stress"} <= set(table.columns)


def test_consistency_penalises_a_one_regime_wonder():
    """A ticker positive everywhere must outrank one that made everything in a
    single regime, even at the same median."""
    steady = pd.DataFrame([
        {"ticker": "STEADY", "regime": f"r{i}", "label": f"r{i}",
         "stress": i < 2, "n_cycles": 20, "annualised": 0.12 + 0.01 * (i % 2),
         "total_pnl": 1000.0, "pct_profitable": 0.9, "assignment_rate": 0.15,
         "mean_cycle_days": 15.0, "worst_cycle_days": 60, "worst_cycle_pnl": -50.0,
         "drawdown": -100.0} for i in range(8)])
    spiky = pd.DataFrame([
        {"ticker": "SPIKY", "regime": f"r{i}", "label": f"r{i}",
         "stress": i < 2, "n_cycles": 20,
         "annualised": 0.90 if i == 5 else -0.05,
         "total_pnl": 1000.0, "pct_profitable": 0.5, "assignment_rate": 0.3,
         "mean_cycle_days": 25.0, "worst_cycle_days": 200, "worst_cycle_pnl": -900.0,
         "drawdown": -2000.0} for i in range(8)])

    scored = regimes.consistency(pd.concat([steady, spiky], ignore_index=True))
    assert scored.iloc[0]["ticker"] == "STEADY"


def test_consistency_needs_enough_regimes():
    thin = pd.DataFrame([
        {"ticker": "THIN", "regime": "r1", "label": "r1", "stress": False,
         "n_cycles": 10, "annualised": 0.2, "total_pnl": 1.0, "pct_profitable": 1.0,
         "assignment_rate": 0.1, "mean_cycle_days": 10.0, "worst_cycle_days": 20,
         "worst_cycle_pnl": 0.0, "drawdown": 0.0}])
    assert regimes.consistency(thin, min_regimes=4).empty


# --- Walk-forward ----------------------------------------------------------

def test_walk_forward_never_scores_on_training_data():
    result = walk_forward(_daily(2200), "T", train_years=4, test_years=1, step_years=2)
    if result["n_folds"] == 0:
        pytest.skip("not enough synthetic history")
    for _, fold in result["folds"].iterrows():
        assert fold["test_start"] >= fold["train_end"]


def test_walk_forward_folds_move_forward():
    result = walk_forward(_daily(2600), "T", train_years=4, test_years=1, step_years=1)
    if result["n_folds"] < 2:
        pytest.skip("not enough folds")
    starts = list(result["folds"]["test_start"])
    assert starts == sorted(starts)


def test_walk_forward_reports_degradation_and_a_verdict():
    result = walk_forward(_daily(2600), "T", train_years=4, test_years=1)
    if result["n_folds"] == 0:
        pytest.skip("not enough folds")
    assert "degradation_ratio" in result
    assert result["verdict"]
    assert result["adaptive_verdict"]


def test_walk_forward_compares_against_a_fixed_baseline():
    """If re-optimising cannot beat one fixed setting, the tuning is not
    earning its keep -- and only the baseline comparison reveals that."""
    result = walk_forward(_daily(2600), "T", train_years=4, test_years=1)
    if result["n_folds"] == 0:
        pytest.skip("not enough folds")
    assert "beat_baseline_rate" in result
    assert "baseline_out_of_sample" in result["folds"].columns


def test_short_history_returns_no_folds_rather_than_crashing():
    result = walk_forward(_daily(300), "T", train_years=5, test_years=1)
    assert result["n_folds"] == 0


# --- Calibration -----------------------------------------------------------

def test_reliability_detects_an_overconfident_model():
    rng = np.random.default_rng(3)
    predicted = pd.Series(rng.uniform(0.80, 0.95, 200))
    outcomes = pd.Series((rng.random(200) < (predicted - 0.15)).astype(float))
    result = calibration.reliability_curve(predicted, outcomes)
    assert result is not None
    assert result.mean_actual < result.mean_predicted
    assert "OPTIMISTIC" in result.verdict


def test_reliability_accepts_a_calibrated_model():
    rng = np.random.default_rng(8)
    predicted = pd.Series(rng.uniform(0.70, 0.95, 400))
    outcomes = pd.Series((rng.random(400) < predicted).astype(float))
    result = calibration.reliability_curve(predicted, outcomes)
    assert abs(result.mean_actual - result.mean_predicted) < 0.06
    assert "Well calibrated" in result.verdict


def test_reliability_flags_zero_resolution():
    """A model predicting the same number for everything can be perfectly
    calibrated and completely useless."""
    predicted = pd.Series([0.85] * 120)
    rng = np.random.default_rng(2)
    outcomes = pd.Series((rng.random(120) < 0.85).astype(float))
    result = calibration.reliability_curve(predicted, outcomes)
    assert result.resolution < 0.005
    assert "Resolution is near zero" in result.verdict


def test_brier_decomposition_is_coherent():
    rng = np.random.default_rng(5)
    predicted = pd.Series(rng.uniform(0.6, 0.95, 300))
    outcomes = pd.Series((rng.random(300) < predicted).astype(float))
    result = calibration.reliability_curve(predicted, outcomes)
    # brier ~= reliability - resolution + uncertainty (Murphy decomposition)
    assert result.brier == pytest.approx(
        result.reliability - result.resolution + result.uncertainty, abs=0.02)


def test_reliability_needs_a_minimum_sample():
    assert calibration.reliability_curve(pd.Series([0.8, 0.9]),
                                          pd.Series([1.0, 1.0])) is None


def test_automation_gate_lists_unmet_conditions():
    gate = calibration._automation_gate(None, None, {"n_closed": 0})
    assert gate["ready"] is False
    assert any(c["pass"] is False for c in gate["checks"])
    assert gate["note"]


def test_calibration_never_edits_config():
    import inspect
    source = inspect.getsource(calibration)
    assert "write_text" not in source and "yaml.dump" not in source
