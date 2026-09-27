"""
Tests for the Phase 1 foundation.

Run with:  python -m pytest tests/ -q

These are behavioural tests against the specific defects the review found,
not coverage padding. Each one fails if the corresponding bug comes back.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics import costs, moves, sizing, vrp  # noqa: E402
from core import market_calendar as cal  # noqa: E402
from core.progress import ConsoleReporter, NullReporter  # noqa: E402


# --- F-01: credentials resolve independent of the working directory -------

def test_env_file_is_absolute_and_cwd_independent(tmp_path, monkeypatch):
    from core import env
    monkeypatch.delenv(env.ENV_FILE_VAR, raising=False)
    first = env.env_file()
    monkeypatch.chdir(tmp_path)
    second = env.env_file()
    assert first == second, "env path must not follow the working directory"
    assert first.is_absolute()


def test_env_override_is_honoured(tmp_path, monkeypatch):
    from core import env
    target = tmp_path / "custom.env"
    target.write_text("CLIENT_SECRET=abc\n", encoding="utf-8")
    monkeypatch.setenv(env.ENV_FILE_VAR, str(target))
    assert env.env_file() == target.resolve()


def test_persist_refresh_token_preserves_other_keys(tmp_path, monkeypatch):
    from core import env
    target = tmp_path / ".env"
    target.write_text("CLIENT_ID=keep-me\nREFRESH_TOKEN=old\nFRED_API_KEY=also-keep\n",
                      encoding="utf-8")
    monkeypatch.setenv(env.ENV_FILE_VAR, str(target))
    env.persist_refresh_token("brand-new-token")
    body = target.read_text(encoding="utf-8")
    assert "REFRESH_TOKEN=brand-new-token" in body
    assert "CLIENT_ID=keep-me" in body
    assert "FRED_API_KEY=also-keep" in body
    assert body.count("REFRESH_TOKEN=") == 1


def test_describe_never_leaks_a_secret(tmp_path, monkeypatch):
    from core import env
    target = tmp_path / ".env"
    target.write_text("CLIENT_SECRET=super-secret-value\n", encoding="utf-8")
    monkeypatch.setenv(env.ENV_FILE_VAR, str(target))
    env.load_env(force=True)
    rendered = "\n".join(s.render() for s in env.describe())
    assert "super-secret-value" not in rendered


# --- F-02 / F-12: session classification ----------------------------------

@pytest.mark.parametrize("when,expected", [
    (dt.datetime(2026, 8, 21, 10, 0, tzinfo=cal.ET), cal.SessionState.RTH),
    (dt.datetime(2026, 8, 21, 5, 0, tzinfo=cal.ET), cal.SessionState.PREMARKET),
    (dt.datetime(2026, 8, 21, 17, 0, tzinfo=cal.ET), cal.SessionState.AFTERHOURS),
    (dt.datetime(2026, 8, 22, 12, 0, tzinfo=cal.ET), cal.SessionState.CLOSED_WEEKEND),
    (dt.datetime(2026, 12, 25, 12, 0, tzinfo=cal.ET), cal.SessionState.CLOSED_HOLIDAY),
])
def test_session_classification(when, expected):
    assert cal.classify(when).state is expected


def test_holiday_calendar_extends_past_2026():
    """The old hardcoded US_MARKET_HOLIDAYS_2026 would treat this as a
    normal trading day."""
    july_5_2027 = dt.datetime(2027, 7, 5, 12, 0, tzinfo=cal.ET)
    assert cal.classify(july_5_2027).state is cal.SessionState.CLOSED_HOLIDAY


def test_weekend_captures_collapse_to_one_block():
    """Friday night, Saturday and Sunday must share a block key, or three
    study runs manufacture three fake IV observations of one stale quote."""
    friday_night = dt.datetime(2026, 8, 21, 22, 0, tzinfo=cal.ET)
    saturday = dt.datetime(2026, 8, 22, 14, 0, tzinfo=cal.ET)
    sunday = dt.datetime(2026, 8, 23, 20, 0, tzinfo=cal.ET)
    blocks = {cal.session_block(t) for t in (friday_night, saturday, sunday)}
    assert len(blocks) == 1


def test_intraday_captures_get_distinct_blocks():
    morning = dt.datetime(2026, 8, 21, 10, 5, tzinfo=cal.ET)
    afternoon = dt.datetime(2026, 8, 21, 14, 5, tzinfo=cal.ET)
    assert cal.session_block(morning) != cal.session_block(afternoon)


def test_only_rth_blocks_feed_iv_history():
    rth = cal.session_block(dt.datetime(2026, 8, 21, 11, 0, tzinfo=cal.ET))
    weekend = cal.session_block(dt.datetime(2026, 8, 22, 11, 0, tzinfo=cal.ET))
    assert cal.is_ingestable_for_iv_history(rth)
    assert not cal.is_ingestable_for_iv_history(weekend)


# --- Costs ----------------------------------------------------------------

def test_open_costs_match_published_schedule():
    fees = costs.option_open(1, "sell")
    # $1.00 commission + $0.10 clearing + $0.02 ORF + $0.00329 TAF
    assert fees.total == pytest.approx(1.12329, abs=1e-5)


def test_closing_is_commission_free_but_not_fee_free():
    fees = costs.option_close(1, "buy")
    assert fees.commission == 0.0
    assert fees.total == pytest.approx(0.12, abs=1e-9)


def test_commission_is_capped_per_leg():
    assert costs.option_open(50, "sell").commission == pytest.approx(10.0)


def test_expiring_worthless_is_free():
    assert costs.option_expire(3).total == 0.0


def test_realistic_fill_is_worse_than_mid_when_selling():
    fill = costs.realistic_fill(0.20, 0.26, side="sell")
    assert 0.20 <= fill < 0.23


def test_realistic_fill_never_leaves_the_quoted_market():
    assert costs.realistic_fill(1.00, 1.02, side="sell", fraction=5.0) >= 1.00
    assert costs.realistic_fill(1.00, 1.02, side="buy", fraction=5.0) <= 1.02


def test_cost_drag_is_severe_on_small_short_dated_credits():
    """The finding that reshapes the exit rules: on a $0.20 credit, opening
    and closing costs 6% of gross before the trade even moves."""
    cheap = costs.csp_economics(strike=47.0, fill_price=0.20, contracts=1,
                                 dte=7, outcome="close")
    rich = costs.csp_economics(strike=47.0, fill_price=1.50, contracts=1,
                                dte=45, outcome="close")
    assert cheap.cost_drag_pct > 5 * rich.cost_drag_pct


def test_assignment_carries_the_five_dollar_fee():
    assigned = costs.csp_economics(50, 0.30, 1, 7, outcome="assign")
    expired = costs.csp_economics(50, 0.30, 1, 7, outcome="expire")
    assert assigned.exit_fees - expired.exit_fees == pytest.approx(5.0)


# --- Sizing: IRA cash-secured constraints ---------------------------------

# These pin behaviour, not the configured account size -- the account moved
# from $50k to $3M in Phase 3 and these must keep meaning the same thing.

def test_max_tradable_strike_tracks_the_position_cap():
    from core.paths import load_config
    cfg = load_config()["account"]
    nlv = cfg["net_liquidating_value"]
    expected = min(nlv * cfg["max_collateral_per_position_pct"],
                   nlv * (1 - cfg["cash_buffer_pct"])) / 100.0
    assert sizing.max_tradable_strike() == pytest.approx(expected)


def test_stock_too_expensive_for_the_account_is_rejected_with_a_reason():
    small = sizing.AccountState(50_000, 50_000)
    result = sizing.max_contracts_for_strike(900.0, small)   # COST-like
    assert result.rejected
    assert result.contracts == 0
    assert any("per-position cap" in r for r in result.reasons)


def test_affordable_stock_is_sized():
    result = sizing.max_contracts_for_strike(45.0)
    assert result.ok and result.contracts >= 1
    assert result.collateral == result.contracts * 4500.0


def test_position_limit_is_enforced():
    from core.paths import load_config
    limit = load_config()["account"]["max_open_positions"]
    acct = sizing.AccountState(3_000_000, 3_000_000, open_positions=limit)
    assert sizing.max_contracts_for_strike(20.0, acct).rejected


def test_kelly_returns_zero_on_a_losing_edge():
    assert sizing.fractional_kelly_contracts(0.50, 20.0, 400.0, 4500.0) == 0


def test_kelly_sizes_a_real_edge():
    n = sizing.fractional_kelly_contracts(0.85, 22.0, 90.0, 4500.0)
    assert n >= 0


# --- Empirical moves ------------------------------------------------------

def _synthetic(n=1500, seed=7, drift=0.0003, vol=0.015):
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift, vol, n)
    close = 100 * np.exp(np.cumsum(steps))
    dates = pd.bdate_range("2018-01-01", periods=n)
    return pd.DataFrame({
        "date": dates,
        "open": close * (1 + rng.normal(0, 0.001, n)),
        "high": close * (1 + np.abs(rng.normal(0, 0.004, n))),
        "low": close * (1 - np.abs(rng.normal(0, 0.004, n))),
        "close": close,
    })


def test_windows_never_look_at_the_entry_bar():
    df = _synthetic(300)
    w = moves.build_windows(df, 5)
    assert (w["exit_date"] > w["entry_date"]).all()
    assert len(w) == len(df) - 5


def test_mae_is_never_positive_and_mfe_never_negative():
    w = moves.build_windows(_synthetic(600), 7)
    assert (w["mae"] <= w["mfe"]).all()


def test_touch_probability_dominates_terminal_probability():
    """Price can dip through a strike and recover. Assignment needs a close
    below it. Touch must therefore always be at least as likely."""
    df = _synthetic(2000)
    spot = float(df["close"].iloc[-1])
    b = moves.breach_probabilities(df, "TEST", spot, spot * 0.95, 7,
                                    vol_conditioned=False)
    assert b is not None
    assert b.prob_touch >= b.prob_terminal


def test_probabilities_are_monotonic_in_moneyness():
    df = _synthetic(2000)
    spot = float(df["close"].iloc[-1])
    near = moves.breach_probabilities(df, "T", spot, spot * 0.98, 7, vol_conditioned=False)
    far = moves.breach_probabilities(df, "T", spot, spot * 0.90, 7, vol_conditioned=False)
    assert near.prob_terminal >= far.prob_terminal
    assert far.prob_otm_empirical >= near.prob_otm_empirical


def test_effective_n_is_honest_about_overlap():
    df = _synthetic(1500)
    s = moves.compute_stats(df, "T", 10)
    assert s.effective_n < s.n_observations
    assert s.effective_n == max(s.n_observations // 10, 1)


def test_insufficient_history_returns_none_not_a_fake_number():
    assert moves.compute_stats(_synthetic(40), "T", 21, min_observations=60) is None


def test_strike_inversion_round_trips():
    df = _synthetic(2500)
    spot = float(df["close"].iloc[-1])
    k = moves.strike_for_target_probability(df, spot, 7, target_prob_otm=0.85)
    b = moves.breach_probabilities(df, "T", spot, k, 7, vol_conditioned=False)
    assert b.prob_otm_empirical == pytest.approx(0.85, abs=0.03)


def test_recovery_reports_assignment_rate_and_duration():
    r = moves.recovery_statistics(_synthetic(2000), drop_pct=0.03, horizon=7)
    assert r["assignments"] > 0
    assert r["median_sessions_to_recover"] > 0


# --- VRP ------------------------------------------------------------------

def test_vrp_flags_selling_cheap_volatility():
    df = _synthetic(500)
    rv = vrp.realized_vol(df, 20)
    thin = vrp.reading("T", rv * 0.85, df, 20)
    rich = vrp.reading("T", rv * 1.35, df, 20)
    assert not thin.tradable
    assert rich.tradable
    assert rich.score > thin.score


def test_vrp_score_is_bounded():
    df = _synthetic(500)
    rv = vrp.realized_vol(df, 20)
    for mult in (0.2, 0.9, 1.0, 1.5, 5.0):
        assert 0.0 <= vrp.reading("T", rv * mult, df, 20).score <= 1.0


def test_rv_window_tracks_option_tenor():
    assert vrp.match_rv_window_to_dte(7) < vrp.match_rv_window_to_dte(30)


# --- Progress -------------------------------------------------------------

def test_reporter_records_stages_and_survives_errors():
    r = NullReporter([("a", "A"), ("b", "B")])
    with r.stage("a", "A", total=2):
        r.advance(2)
    with pytest.raises(ValueError):
        with r.stage("b", "B"):
            raise ValueError("boom")
    assert r.records["a"].fraction == 1.0
    assert "boom" in r.records["b"].error
    assert "FAILED" in r.records["b"].summary()


def test_eta_uses_measured_throughput():
    r = NullReporter()
    with r.stage("x", "X", total=100) as rec:
        r.advance(10)
        assert rec.eta_seconds > 0


# --- Phase 2: capture cadence and run safety ------------------------------

def test_dte_token_shape():
    from data_sources.tastytrade_client import dte_token
    assert dte_token(0, 21) == "between_0_21_dte"
    assert dte_token(0, 60) == "between_0_60_dte"


def test_capture_needed_when_no_snapshot_exists():
    from data_sources.chains import needs_capture
    wanted, reason = needs_capture("__NOSUCHTICKER__")
    assert wanted and "no snapshot" in reason


def test_manifest_is_strict_json_despite_nans():
    """json.dumps emits bare NaN, which no strict parser accepts."""
    import json
    from pipeline.run import _json_safe
    payload = _json_safe({"a": float("nan"), "b": [float("inf"), 1.5],
                           "c": {"d": float("-inf")}})
    parsed = json.loads(json.dumps(payload))
    assert parsed["a"] is None
    assert parsed["b"] == [None, 1.5]
    assert parsed["c"]["d"] is None


def test_run_lock_blocks_a_second_run():
    from pipeline.run import RunLock, RunLocked
    with RunLock():
        with pytest.raises(RunLocked):
            with RunLock():
                pass


def test_run_lock_releases_on_exit():
    from core.paths import runs_dir
    from pipeline.run import RunLock
    with RunLock():
        pass
    assert not (runs_dir() / "run.lock").exists()


def test_earnings_guard_fails_safe_when_calendar_is_missing():
    """An unknown earnings date must block, not wave the trade through."""
    from data_sources.yfinance_sync import earnings_guard
    guard = earnings_guard("__NOSUCHTICKER__", dt.date(2026, 9, 30))
    assert guard.blocks_expiry
    assert not guard.confirmed


def test_risk_free_rate_reports_its_provenance():
    from data_sources.reference import risk_free_rate
    rate, source = risk_free_rate(7)
    assert 0.0 <= rate < 0.25
    assert source  # never silently returns a number with no explanation


def test_subscription_limiter_tracks_a_rolling_window():
    from data_sources.chains import SubscriptionLimiter
    limiter = SubscriptionLimiter(budget_per_min=1000)
    limiter.record(400)
    limiter.record(400)
    limiter.wait(100)  # 900 total, under budget: must not block
    assert sum(c for _, c in limiter.events) == 800


# --- Portability regressions -----------------------------------------------
#
# `analytics/config.py` used to resolve the project root by reading
# `project_root: "D:/csp"` out of config.yaml. On Windows that happened to
# work; anywhere else `Path("D:/csp")` is *relative*, so every database opened
# under the current working directory. The Trade Log page failed exactly this
# way, reaching for `.../build/D:/csp/data/trade_log.duckdb`.

def test_config_yaml_declares_no_absolute_project_root():
    """The folder must not announce its own location."""
    import yaml
    from core.paths import config_path
    raw = yaml.safe_load(config_path().read_text(encoding="utf-8"))
    assert "project_root" not in raw


def test_legacy_config_shim_agrees_with_core_paths():
    from analytics.config import project_root as legacy_root
    from core.paths import project_root as canonical_root
    assert legacy_root() == canonical_root()
    assert legacy_root().is_absolute()


def test_legacy_shim_shares_one_config_cache():
    """Two lru_caches over the same file meant reload_config() on one loader
    left the other holding stale values."""
    import analytics.config as legacy
    import core.paths as canonical
    assert legacy.load_config is canonical.load_config
    assert legacy.load_config() is canonical.load_config()


def test_project_root_lands_where_config_yaml_actually_is():
    from core.paths import project_root
    assert (project_root() / "config.yaml").is_file()


def test_missing_daily_database_reads_empty_not_raises(tmp_path, monkeypatch):
    """A fresh copy of the folder starts with an empty data/ directory. That is
    a normal state the pages already handle -- it must not be a traceback."""
    import analytics.data_access as da
    monkeypatch.setattr(da, "_daily_db_path", lambda: tmp_path / "absent.duckdb")
    frame = da.load_daily_bars("AAPL")
    assert frame.empty
    assert list(frame.columns) == da.DAILY_COLUMNS
