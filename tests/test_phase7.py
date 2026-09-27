"""
Phase 7 tests: gap risk, skew, term structure, and the diagnostics that stop a
zero-candidate run being silent.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics import gaps, skew  # noqa: E402
from analytics.candidates import Recommendation, rejection_census  # noqa: E402


# --- Gap decomposition -----------------------------------------------------

def _sessions(seed=1, gap_vol=0.010, intraday_vol=0.010, n=1200):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2019-01-01", periods=n)
    overnight = rng.standard_t(3, n) * gap_vol
    intraday = rng.normal(0, intraday_vol, n)
    close, rows = 100.0, []
    for i in range(n):
        open_ = close * (1 + overnight[i])
        new_close = open_ * (1 + intraday[i])
        rows.append((dates[i], open_, new_close,
                      min(open_, new_close) * 0.995, max(open_, new_close) * 1.005))
        close = new_close
    return pd.DataFrame(rows, columns=["date", "rth_open", "rth_close",
                                        "rth_low", "rth_high"])


@pytest.fixture
def gappy(monkeypatch):
    frame = _sessions(seed=1, gap_vol=0.018, intraday_vol=0.008)
    monkeypatch.setattr(gaps, "session_frame", lambda t, years=20: frame)
    return frame


@pytest.fixture
def smooth(monkeypatch):
    frame = _sessions(seed=2, gap_vol=0.003, intraday_vol=0.012)
    monkeypatch.setattr(gaps, "session_frame", lambda t, years=20: frame)
    return frame


def test_gap_decomposition_is_exact(gappy):
    series = gaps.gap_series("T")
    reconstructed = (1 + series["overnight"]) * (1 + series["intraday"]) - 1
    assert np.allclose(reconstructed, series["total"], atol=1e-9)


def test_gappy_name_shows_high_overnight_variance_share(gappy):
    result = gaps.profile("T")
    assert result.overnight_variance_share > 0.7


def test_smooth_name_shows_low_overnight_variance_share(smooth):
    result = gaps.profile("T")
    assert result.overnight_variance_share < 0.4


def test_gap_tail_is_fatter_than_intraday_tail(gappy):
    """The whole reason for this module: daily bars hide the overnight tail."""
    result = gaps.profile("T")
    assert result.gap_kurtosis > result.intraday_kurtosis
    assert result.tail_ratio > 1.0


def test_variance_shares_use_variance_not_volatility(gappy):
    """Volatilities do not add; variances do. Getting this wrong inflates the
    overnight share."""
    series = gaps.gap_series("T")
    result = gaps.profile("T")
    expected = np.var(series["overnight"]) / np.var(series["total"])
    assert result.overnight_variance_share == pytest.approx(expected, rel=1e-6)


def test_insufficient_history_returns_none(monkeypatch):
    monkeypatch.setattr(gaps, "session_frame",
                         lambda t, years=20: _sessions(n=40))
    assert gaps.profile("T") is None


def test_trade_gap_risk_rises_with_exposure_count(gappy):
    near = gaps.trade_gap_risk("T", 100.0, 95.0, trading_days=2)
    far = gaps.trade_gap_risk("T", 100.0, 95.0, trading_days=10)
    assert far.prob_any_gap_through > near.prob_any_gap_through


def test_trade_gap_risk_falls_further_out_of_the_money(gappy):
    near = gaps.trade_gap_risk("T", 100.0, 98.0, trading_days=7)
    far = gaps.trade_gap_risk("T", 100.0, 85.0, trading_days=7)
    assert far.prob_any_gap_through < near.prob_any_gap_through


def test_joint_probability_stays_a_probability(gappy):
    result = gaps.trade_gap_risk("T", 100.0, 99.0, trading_days=20)
    assert 0.0 <= result.prob_any_gap_through <= 1.0


# --- Skew ------------------------------------------------------------------

def _chain(put_bump, call_bump, near_iv=0.30, far_iv=0.30, spot=100.0,
            step=2, spread=0.02, dtes=(7, 35)):
    """A chain built the way a real one is: quotes, not implied vols.

    `measure` no longer reads the feed's IV columns -- it solves vol from the
    bid/ask against a parity-implied forward -- so a fixture that supplies only
    IVs would exercise nothing. Here one true vol per strike is priced into
    both the call and the put, which makes put-call parity hold exactly, and
    the quotes are placed symmetrically around those prices.

    `spread` is the half-width as a fraction of spot. Small means a tight
    market; large means the quotes cannot resolve the skew, which is itself a
    thing worth testing.
    """
    from analytics.surface import _black76

    rows = []
    for dte, base in zip(dtes, (near_iv, far_iv)):
        expiration = pd.Timestamp(dt.date.today() + dt.timedelta(days=dte))
        years = dte / 365.0
        for strike in np.arange(spot * 0.8, spot * 1.2 + step, step):
            moneyness = (strike - spot) / spot
            vol = base + put_bump * max(-moneyness, 0) + call_bump * max(moneyness, 0)
            call = _black76(spot, float(strike), years, vol, "call")
            put = _black76(spot, float(strike), years, vol, "put")
            half = spread * spot / 2.0
            rows.append({
                "expiration": expiration, "strike_price": float(strike),
                "call_bid": max(call - half, 0.0), "call_ask": call + half,
                "put_bid": max(put - half, 0.0), "put_ask": put + half,
                # Feed columns, kept so anything still reading them sees values
                # consistent with the quotes rather than contradicting them.
                "call_iv": vol, "put_iv": vol,
                "call_delta": np.nan, "put_delta": np.nan})
    return pd.DataFrame(rows)


def test_rich_skew_is_detected_and_favourable():
    reading = skew.measure(_chain(3.0, 0.0, spread=0.0005), "T", 100.0)
    assert reading.classification == "rich"
    assert reading.favourable
    assert reading.normalised_skew > 0.15


def test_inverted_skew_is_detected_and_unfavourable():
    reading = skew.measure(_chain(0.0, 2.0, spread=0.0005), "T", 100.0)
    assert reading.classification == "inverted"
    assert not reading.favourable
    assert reading.normalised_skew < 0


def test_skew_picks_strikes_near_25_delta():
    reading = skew.measure(_chain(3.0, 0.0, spread=0.0005), "T", 100.0)
    assert reading.put_strike < 100 < reading.call_strike


def test_skew_score_is_bounded_and_monotonic():
    rich = skew.measure(_chain(3.0, 0.0, spread=0.0005), "T", 100.0).score
    flat = skew.measure(_chain(0.0, 0.0, spread=0.0002, step=1.0), "T", 100.0).score
    inverted = skew.measure(_chain(0.0, 2.0, spread=0.0005), "T", 100.0).score
    assert 0.0 <= inverted <= flat <= rich <= 1.0


def test_skew_needs_both_sides():
    frame = _chain(3.0, 0.0, spread=0.0005)
    frame["call_bid"] = np.nan
    frame["call_ask"] = np.nan
    assert skew.measure(frame, "T", 100.0) is None


def test_backwardation_flags_a_suspected_event():
    structure = skew.term_structure(
        _chain(0.3, 0.0, near_iv=0.55, far_iv=0.30, spread=0.0005), "T", 100.0)
    assert structure.state == "backwardation"
    assert structure.event_suspected


def test_contango_is_the_normal_state():
    structure = skew.term_structure(
        _chain(0.3, 0.0, near_iv=0.28, far_iv=0.32, spread=0.0005), "T", 100.0)
    assert structure.state == "contango"
    assert not structure.event_suspected


def test_term_structure_needs_two_expirations():
    single = _chain(0.3, 0.0, spread=0.0005)
    single = single[single["expiration"] == single["expiration"].min()]
    assert skew.term_structure(single, "T", 100.0) is None


# --- Diagnostics -----------------------------------------------------------

def _rec(ticker, accepted, reasons=()):
    rec = Recommendation.__new__(Recommendation)
    object.__setattr__(rec, "ticker", ticker)
    object.__setattr__(rec, "accepted", accepted)
    object.__setattr__(rec, "rejections", tuple(reasons))
    return rec


def test_census_names_the_dominant_gate():
    rows = [_rec(f"T{i}", False, ("earnings report falls before expiration",))
            for i in range(58)]
    rows += [_rec("OK", True)]
    census = rejection_census(rows)
    assert census["by_reason"][0][0] == "earnings before expiration"
    assert census["by_reason"][0][1] == 58
    assert "check the input behind it" in census["headline"].lower()


def test_census_reports_when_nothing_was_evaluated():
    census = rejection_census([])
    assert census["accepted"] == 0
    assert "No strikes were evaluated" in census["headline"]


def test_census_counts_accepted():
    rows = [_rec("A", True), _rec("B", True),
            _rec("C", False, ("IV/RV is 0.9, below the 1.05 floor",))]
    census = rejection_census(rows)
    assert census["accepted"] == 2


def test_earnings_gate_degrades_when_the_calendar_is_unhealthy():
    """Blocking on an unknown date is right for one ticker and useless when it
    blocks all of them."""
    from data_sources.yfinance_sync import earnings_guard
    strict = earnings_guard("__NOSUCH__", dt.date(2026, 9, 30), calendar_healthy=True)
    degraded = earnings_guard("__NOSUCH__", dt.date(2026, 9, 30), calendar_healthy=False)
    assert strict.blocks_expiry
    assert not degraded.blocks_expiry
    assert "NOT earnings-checked" in degraded.note


def test_calendar_health_reports_coverage():
    from data_sources.yfinance_sync import earnings_calendar_health
    health = earnings_calendar_health(["AAA", "BBB"])
    assert "coverage" in health and "healthy" in health


# --- Regressions: the two contaminated signals -----------------------------
#
# The first live run of the signals layer produced two results that were
# artifacts of the measurement rather than facts about the market:
#
#   * 24 of 61 names read as INVERTED skew, which is close to impossible in
#     equities. Cause: nearest-listed-strike snapping put the two legs at
#     unequal moneyness (put -2.9% OTM against call +3.9% OTM on average) and
#     the steep short-dated smile flipped the sign.
#   * AAPL's worst overnight gap read -76.5% on 2018-02-01, dragging its
#     overnight vol to 78% against a true figure near 30%. Cause: a 4:1 split
#     at a seam between two differently-adjusted eras of the 1-minute archive.
#
# The tests below are the standing guards against both returning.

def _symmetric_chain(step, spot=100.0, dte=7):
    """A perfectly symmetric smile. Its true skew is exactly zero."""
    rows = []
    expiration = pd.Timestamp(dt.date.today() + dt.timedelta(days=dte))
    for strike in np.arange(spot * 0.8, spot * 1.2 + step, step):
        moneyness = (strike - spot) / spot
        rows.append({
            "expiration": expiration, "strike_price": float(strike),
            "put_delta": -float(np.clip(0.5 + moneyness * 4, 0.02, 0.98)),
            "call_delta": float(np.clip(0.5 - moneyness * 4, 0.02, 0.98)),
            "put_iv": 0.30 + 0.4 * abs(moneyness),
            "call_iv": 0.30 + 0.4 * abs(moneyness)})
    return pd.DataFrame(rows)


@pytest.mark.parametrize("step", [1.0, 2.0, 5.0, 10.0])
def test_symmetric_smile_never_reads_inverted(step):
    """The 24-inverted-names regression. A symmetric smile has true skew of
    exactly zero and must never be called inverted on any strike grid.

    Coarse grids may legitimately have no 25-delta strike at all at 7 DTE --
    declining is a correct answer. Claiming an inversion is not."""
    reading = skew.measure(_chain(0.0, 0.0, spread=0.0002, step=step), "T", 100.0)
    assert reading is None or reading.classification != "inverted"
    if reading is not None and reading.measurable:
        assert reading.classification == "flat"
        assert abs(reading.normalised_skew) < 0.01


def test_legs_are_symmetric_about_the_forward():
    reading = skew.measure(_chain(3.0, 0.0, spread=0.0005), "T", 100.0)
    put_otm = reading.put_strike / reading.forward - 1.0
    call_otm = reading.call_strike / reading.forward - 1.0
    assert abs(put_otm + call_otm) < skew.MAX_MONEYNESS_ASYMMETRY


def test_extrapolation_off_the_end_of_the_curve_is_refused():
    """A chain that never reaches 25 delta gets None, not a guess."""
    narrow = _chain(3.0, 0.0, spread=0.0005, step=1.0)
    narrow = narrow[(narrow["strike_price"] >= 98) & (narrow["strike_price"] <= 102)]
    assert skew.measure(narrow, "T", 100.0) is None


def test_expiration_is_chosen_inside_the_trading_window():
    """SPY, MSFT, AMZN and IBIT all measured at 1 DTE in the first run, where
    the quote is dominated by minimum-tick effects."""
    base = _chain(3.0, 0.0, spread=0.0005)          # 7 DTE and 35 DTE
    tomorrow = _chain(3.0, 0.0, spread=0.0005, dtes=(1, 35))
    tomorrow = tomorrow[tomorrow["expiration"] == tomorrow["expiration"].min()]
    reading = skew.measure(pd.concat([tomorrow, base], ignore_index=True), "T", 100.0)
    assert reading is not None
    assert reading.dte == 7                          # the window, not the nearest


# --- Regression: the quotes have to be able to answer ----------------------
#
# The interpolation fix removed the strike-grid artifact but 22 of 54 names
# still read inverted. The second cause was the IV itself: `put_iv`/`call_iv`
# arrive on the DXLink greeks feed carrying each symbol's *own last update*,
# which in a closed session is scattered across the previous trading day. Skew
# is a difference between two wings, so wings sampled hours apart cannot
# measure it. Against the 2026-08-21 capture the feed sat 4.6 points of ATM vol
# below what the same snapshot's quotes imply and disagreed on the sign for 11
# of 29 names.
#
# Everything is now solved from the snapshot's own bid/ask, and every reading
# carries the interval those quotes permit.

def test_wide_quotes_cannot_measure_skew():
    """The same true smile, quoted wide, must decline rather than classify."""
    reading = skew.measure(_chain(3.0, 0.0, spread=0.01), "T", 100.0)
    assert reading is None or not reading.measurable
    if reading is not None:
        assert reading.classification == "unmeasurable"
        assert not reading.favourable
        assert np.isnan(reading.score)


def test_tight_quotes_on_the_same_smile_recover_it():
    """The pair to the test above: the fix must not simply refuse everything."""
    reading = skew.measure(_chain(3.0, 0.0, spread=0.0005), "T", 100.0)
    assert reading.measurable
    assert reading.classification == "rich"
    assert reading.skew_low <= reading.normalised_skew <= reading.skew_high


def test_band_narrows_as_the_spread_narrows():
    wide = skew.measure(_chain(3.0, 0.0, spread=0.002), "T", 100.0)
    tight = skew.measure(_chain(3.0, 0.0, spread=0.0005), "T", 100.0)
    assert (tight.skew_high - tight.skew_low) < (wide.skew_high - wide.skew_low)


def test_flat_skew_stays_reportable():
    """A flat reading's band straddles zero by construction. Requiring the band
    to exclude zero would make 'flat' unreportable forever -- and flat is the
    reading the strategy most needs to hear, because it means you are not being
    paid for the tail you are selling."""
    reading = skew.measure(_chain(0.0, 0.0, spread=0.0002, step=1.0), "T", 100.0)
    assert reading.measurable
    assert reading.classification == "flat"
    assert reading.skew_low < 0 < reading.skew_high


def test_unmeasurable_skew_warns_but_never_blocks():
    """A gate that blocks on 'we could not tell' reproduces the Phase 6
    earnings cascade: every refusal reasonable, the aggregate useless."""
    import inspect
    from analytics.candidates import evaluate_strike
    source = inspect.getsource(evaluate_strike)
    assert "unmeasurable" in source, "the gate does not know about the new class"
    branch = source.split('== "unmeasurable"')[1].split("\n    #")[0]
    assert "warnings.append" in branch
    assert "rejections.append" not in branch


# --- Surface layer ---------------------------------------------------------

def test_forward_comes_from_parity_not_spot():
    """A chain priced off a forward 2% above spot must report that forward."""
    from analytics.surface import implied_forward
    chain = _chain(0.0, 0.0, spread=0.0002, step=1.0, spot=102.0)
    near = chain[chain["expiration"] == chain["expiration"].min()]
    forward = implied_forward(near, 7 / 365)
    assert forward == pytest.approx(102.0, abs=0.05)


def test_in_the_money_options_are_excluded_from_the_surface():
    """A deep ITM option is almost all intrinsic, so its implied vol is the
    spread divided by a tiny vega -- that is how a wide quote on a deep call
    became a 56% vol reading."""
    from analytics.surface import otm_surface
    chain = _chain(0.0, 0.0, spread=0.0002, step=1.0)
    near = chain[chain["expiration"] == chain["expiration"].min()]
    surface = otm_surface(near, 100.0, 7 / 365)
    assert (surface.loc[surface["side"] == "put", "strike"] < 100).all()
    assert (surface.loc[surface["side"] == "call", "strike"] >= 100).all()


def test_a_price_at_intrinsic_yields_no_vol():
    """A zero bid carries no volatility information. NaN says so; a floor
    value would launder it into data."""
    from analytics.surface import solve_iv
    assert np.isnan(solve_iv(0.0, 100.0, 90.0, 7 / 365, "put"))
    assert np.isnan(solve_iv(10.0, 100.0, 90.0, 7 / 365, "call"))   # = intrinsic
    assert solve_iv(11.0, 100.0, 90.0, 7 / 365, "call") > 0


# --- Regression: split artifacts in the 1-minute archive -------------------

def _sessions_with_split(ratio=0.25, split_index=600):
    """Clean sessions with one day where the archive changes price basis."""
    frame = _sessions(seed=7, gap_vol=0.006, intraday_vol=0.006, n=1200)
    for column in ("rth_open", "rth_close", "rth_low", "rth_high"):
        frame.loc[split_index:, column] = frame.loc[split_index:, column] * ratio
    return frame


def _adjusted_from(frame):
    """Ground truth: daily bars that are consistently adjusted throughout."""
    clean = _sessions(seed=7, gap_vol=0.006, intraday_vol=0.006, n=1200)
    return pd.DataFrame({"date": clean["date"], "close": clean["rth_close"]})


def test_split_artifact_is_excluded(monkeypatch):
    """AAPL -76.5% on 2018-02-01 was a 4:1 split, not a market move."""
    frame = _sessions_with_split()
    monkeypatch.setattr(gaps, "session_frame", lambda t, years=20: frame)
    import data_sources.yfinance_sync as ys
    monkeypatch.setattr(ys, "load_daily",
                        lambda t, **k: _adjusted_from(frame))

    raw = gaps.gap_series("T", exclude_corporate_actions=False)
    cleaned = gaps.gap_series("T", exclude_corporate_actions=True)

    assert raw["overnight"].min() < -0.60          # the artifact is there
    assert cleaned.attrs["excluded_days"] >= 1     # and it was caught
    assert cleaned["overnight"].min() > -0.20      # and it is gone


def test_split_artifact_does_not_inflate_overnight_volatility(monkeypatch):
    frame = _sessions_with_split()
    monkeypatch.setattr(gaps, "session_frame", lambda t, years=20: frame)
    import data_sources.yfinance_sync as ys
    monkeypatch.setattr(ys, "load_daily",
                        lambda t, **k: _adjusted_from(frame))
    result = gaps.profile("T")
    assert result.excluded_days >= 1
    assert result.overnight_vol < 0.40


def test_ordinary_days_survive_the_filter(monkeypatch):
    """The filter must not quietly eat real volatility."""
    frame = _sessions(seed=7, gap_vol=0.006, intraday_vol=0.006, n=1200)
    monkeypatch.setattr(gaps, "session_frame", lambda t, years=20: frame)
    import data_sources.yfinance_sync as ys
    monkeypatch.setattr(ys, "load_daily",
                        lambda t, **k: _adjusted_from(frame))
    cleaned = gaps.gap_series("T")
    assert cleaned.attrs["excluded_days"] == 0
    assert len(cleaned) == len(gaps.gap_series("T", exclude_corporate_actions=False))


def test_filter_falls_back_when_no_ground_truth_exists(monkeypatch):
    """Without adjusted bars the 50% absolute bound still catches a 4:1 split."""
    frame = _sessions_with_split()
    monkeypatch.setattr(gaps, "session_frame", lambda t, years=20: frame)
    import data_sources.yfinance_sync as ys
    monkeypatch.setattr(ys, "load_daily",
                        lambda t, **k: pd.DataFrame())
    cleaned = gaps.gap_series("T")
    assert cleaned.attrs["excluded_days"] == 1
    assert cleaned["overnight"].min() > -0.20
