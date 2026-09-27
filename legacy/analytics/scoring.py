"""
Composite score combining annualized premium yield, IV rank, Stage 3
liquidity, technical health, and probability-OTM into a ranked candidate
list -- see docs/PROJECT_SPEC.md "Analysis engine" and config.yaml's `scoring`
section for the weights (a starting point, not a claimed-optimal
methodology; flagged as such in the build plan).

Liquidity and yield are scored by percentile rank *within the scanned
universe* rather than against a fixed reference constant -- this adapts to
whatever set of tickers is actually being compared (Tier 1 vs. Tier 2 vs. a
hand-picked list) without needing an arbitrary "good OI" number baked in.
"""
import numpy as np
import pandas as pd

from analytics.config import load_config
from legacy.analytics.data_access import load_daily_bars, load_chain_snapshot, list_snapshot_dates_for_ticker
from analytics.technicals import technical_health_flag
from analytics.options_math import probability_otm
from analytics.iv_history import iv_rank_and_percentile
from analytics.chain_utils import nearest_target_delta_put


def _percentile_rank(series: pd.Series, higher_is_better: bool = True) -> pd.Series:
    """0-1 percentile rank, NaNs excluded from the ranking and left as NaN
    (handled by the caller's neutral-fill, not silently ranked as worst)."""
    ranked = series.rank(pct=True, ascending=higher_is_better)
    return ranked


def candidate_components(ticker: str, cfg: dict) -> dict:
    """Per-ticker raw component values (yield, iv_rank, prob_otm, liquidity
    inputs, technical health) before universe-relative normalization."""
    thr = cfg["stage3_thresholds"]
    rate = cfg["analytics"]["risk_free_rate"]

    chain, underlying = load_chain_snapshot(ticker)
    daily = load_daily_bars(ticker)

    out = {
        "ticker": ticker, "annualized_yield": None, "put_strike": None, "put_dte": None,
        "prob_otm_theoretical": None, "oi_at_target": None, "spread_pct": None,
        "strikes_in_band": None, "iv_rank": None, "iv_percentile": None,
        "current_iv": None, "technical_health_score": None, "downtrend_flag": None,
    }

    if chain.empty or underlying is None or underlying.empty or daily.empty:
        return out

    snapshot_dates = list_snapshot_dates_for_ticker(ticker)
    if not snapshot_dates:
        return out
    snapshot_date = snapshot_dates[-1]

    # Strike/expiration selection uses the DTE window AS OF THE SNAPSHOT --
    # that's what was actually scanned into the 5-14 DTE window at capture
    # time (matches output/stage3_candidates.csv's own nearest_put_delta).
    # Yield/probability-OTM below then use DTE AS OF TODAY, since real
    # calendar time has passed since a possibly-stale snapshot was taken.
    nearest = nearest_target_delta_put(chain, snapshot_date, thr["target_delta"],
                                        thr["dte_min"], thr["dte_max"])
    spot = float(underlying["mark"].iloc[0]) if pd.notna(underlying["mark"].iloc[0]) else float(underlying["last"].iloc[0])

    if nearest is not None:
        real_dte = (nearest["expiration"] - pd.Timestamp.today().normalize()).days
        out["put_strike"] = nearest["strike"]
        out["put_dte"] = real_dte
        out["oi_at_target"] = nearest["put_open_interest"]
        if nearest["put_bid"] is not None and nearest["put_ask"] is not None and nearest["put_mark"]:
            out["spread_pct"] = (nearest["put_ask"] - nearest["put_bid"]) / nearest["put_mark"] * 100.0
        if real_dte > 0 and nearest["put_mark"] is not None and nearest["put_mark"] > 0:
            out["annualized_yield"] = (nearest["put_mark"] / nearest["strike"]) * (365.0 / real_dte)
        if real_dte > 0 and nearest["put_iv"] is not None:
            out["prob_otm_theoretical"] = probability_otm(
                spot, nearest["strike"], real_dte, nearest["put_iv"], rate, "put")

    ivr = iv_rank_and_percentile(ticker)
    out["iv_rank"], out["iv_percentile"], out["current_iv"] = ivr["iv_rank"], ivr["iv_percentile"], ivr["current_iv"]

    health = technical_health_flag(daily, cfg)
    out["technical_health_score"] = health.get("health_score")
    out["downtrend_flag"] = health.get("downtrend_flag")

    return out


def compute_composite_scores(tickers: list[str]) -> pd.DataFrame:
    """Builds the component table for a universe of tickers and combines it
    into a 0-100 composite_score column using config.yaml's scoring.weights.
    Any candidate missing a component (e.g. no IV history yet) gets that
    component neutral-filled at the universe median rather than zeroed out,
    so one missing input doesn't tank an otherwise-strong candidate."""
    cfg = load_config()
    weights = cfg["scoring"]["weights"]

    rows = [candidate_components(t, cfg) for t in tickers]
    df = pd.DataFrame(rows)

    yield_score = _percentile_rank(df["annualized_yield"], higher_is_better=True)
    iv_rank_score = df["iv_rank"]  # already 0-1; NaN handled by neutral-fill below
    prob_otm_score = df["prob_otm_theoretical"]  # already 0-1, higher = safer = better
    technical_score = df["technical_health_score"]  # already 0-1

    oi_rank = _percentile_rank(df["oi_at_target"], higher_is_better=True)
    spread_rank = _percentile_rank(df["spread_pct"], higher_is_better=False)
    band_rank = _percentile_rank(df["strikes_in_band"], higher_is_better=True)
    liquidity_score = pd.concat([oi_rank, spread_rank, band_rank], axis=1).mean(axis=1)

    components = pd.DataFrame({
        "yield": yield_score, "iv_rank": iv_rank_score, "liquidity": liquidity_score,
        "technical": technical_score, "prob_otm": prob_otm_score,
    })
    # Neutral-fill missing components at 0.5 (rather than dropping the
    # candidate or zeroing the component) -- most common early on for
    # iv_rank, which needs 10+ accumulated snapshot dates (see iv_history.py).
    components_filled = components.astype(float).fillna(0.5)

    weight_series = pd.Series(weights)[components.columns]
    df["composite_score"] = (components_filled * weight_series).sum(axis=1) * 100
    for col in components.columns:
        df[f"component_{col}"] = components[col]

    return df.sort_values("composite_score", ascending=False).reset_index(drop=True)
