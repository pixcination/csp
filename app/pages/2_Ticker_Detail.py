"""Ticker detail page -- drill-in view for a single candidate: price/vol
chart, IV rank, P&L diagrams, net Greeks, probability cone, strike/DTE
what-if explorer, chain table with volume/OI overlay. See
docs/PROJECT_SPEC.md Page 2."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import streamlit as st

from analytics.config import load_config
from analytics.data_access import load_scanner_universe, load_chain_snapshot, load_daily_bars, list_snapshot_dates_for_ticker
from analytics.volatility import close_to_close_series
from analytics.options_math import bs_price_greeks, probability_otm
from analytics.iv_history import load_iv_history, iv_rank_and_percentile
from analytics.chain_utils import nearest_target_delta_put
from analytics.backtest import run_backtest
from app.components.formatting import fmt_currency, fmt_pct, fmt_delta
from app.components.charts import (
    price_vol_chart, iv_vs_rv_chart, pnl_at_expiration_chart, pnl_over_time_chart,
    greeks_over_time_chart, probability_cone_chart, chain_volume_oi_chart,
)


@st.cache_data(ttl=3600, show_spinner="Running backtest...")
def _cached_backtest(ticker: str) -> dict:
    result = run_backtest(ticker)
    return result["summary"]


st.title("Ticker Detail")
cfg = load_config()
rate = cfg["analytics"]["risk_free_rate"]

universe = load_scanner_universe("all")
if universe.empty:
    st.warning("No Stage 3 candidates available.")
    st.stop()

tickers = sorted(universe["ticker"].unique())
top_l, top_r = st.columns([2, 1])
with top_l:
    ticker = st.selectbox("Ticker", tickers)
with top_r:
    commission = st.number_input("Commission per contract ($)", min_value=0.0, value=0.65, step=0.05)

daily = load_daily_bars(ticker)
chain, underlying = load_chain_snapshot(ticker)
snapshot_dates = list_snapshot_dates_for_ticker(ticker)

if daily.empty:
    st.warning(f"No daily price history for {ticker}.")
    st.stop()

spot = float(underlying["mark"].iloc[0]) if (underlying is not None and not underlying.empty and pd.notna(underlying["mark"].iloc[0])) else float(daily["close"].iloc[-1])

if chain.empty:
    st.warning(f"No Stage 3 chain snapshot for {ticker} yet -- what-if explorer, Greeks, "
               f"and P&L diagrams need a chain scan. Run one from the Scanner page.")
    st.stop()

snapshot_date = snapshot_dates[-1]
chain = chain.copy()
chain["expiration"] = pd.to_datetime(chain["expiration"])

# --- What-if controls: strike / expiration ---
expirations = sorted(chain["expiration"].unique())
thr = cfg["stage3_thresholds"]
default_nearest = nearest_target_delta_put(chain, snapshot_date, thr["target_delta"],
                                            thr["dte_min"], thr["dte_max"])
default_exp = default_nearest["expiration"] if default_nearest else expirations[0]

wc1, wc2 = st.columns(2)
with wc1:
    expiration = st.selectbox("Expiration", expirations,
                               index=expirations.index(default_exp) if default_exp in expirations else 0,
                               format_func=lambda d: d.strftime("%Y-%m-%d"))
with wc2:
    put_strikes = sorted(chain.loc[chain["expiration"] == expiration, "strike_price"].dropna().unique())
    default_strike = default_nearest["strike"] if default_nearest and default_nearest["expiration"] == expiration else (
        put_strikes[len(put_strikes) // 2] if put_strikes else None)
    strike = st.selectbox("Strike (put)", put_strikes,
                           index=put_strikes.index(default_strike) if default_strike in put_strikes else 0)

real_dte = max((expiration - pd.Timestamp.today().normalize()).days, 0)
row = chain[(chain["expiration"] == expiration) & (chain["strike_price"] == strike)]
row = row.iloc[0] if not row.empty else None

chain_iv = float(row["put_iv"]) if row is not None and pd.notna(row.get("put_iv")) else None
rv20 = float(close_to_close_series(daily, 20).iloc[-1]) if len(daily) > 21 else None
vol = chain_iv if chain_iv and chain_iv > 0 else (rv20 or 0.30)
vol_source = "chain IV" if (chain_iv and chain_iv > 0) else "20d realized vol (fallback -- no chain IV)"

mark = None
if row is not None and pd.notna(row.get("put_mark")) and row.get("put_mark") > 0:
    mark = float(row["put_mark"])
premium_source = "chain mark price"
if mark is None:
    mark = bs_price_greeks(spot, strike, real_dte, vol, rate, "put").price
    premium_source = "Black-Scholes theoretical (no live mark in snapshot)"

greeks = bs_price_greeks(spot, strike, real_dte, vol, rate, "put")
pop_theo = probability_otm(spot, strike, real_dte, vol, rate, "put")
ann_yield = (mark / strike) * (365.0 / real_dte) if real_dte > 0 else None

st.caption(f"Snapshot: {snapshot_date} · Vol input: {vol_source} ({vol:.1%}) · "
           f"Premium: {premium_source} (${mark:.2f})")

# --- Header metrics ---
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Spot", fmt_currency(spot))
m2.metric("DTE (today)", real_dte)
m3.metric("Put delta", fmt_delta(greeks.delta))
m4.metric("Ann. yield", fmt_pct(ann_yield) if ann_yield else "--")
m5.metric("Prob. OTM (theo.)", fmt_pct(pop_theo))

st.divider()

# --- Price + vol chart ---
st.plotly_chart(price_vol_chart(daily, strike=strike, expiration=expiration), width="stretch")

# --- IV vs RV / Net Greeks ---
col_l, col_r = st.columns(2)
with col_l:
    iv_hist = load_iv_history(ticker)
    rv_series = close_to_close_series(daily, 20).dropna() if len(daily) > 21 else None
    st.plotly_chart(iv_vs_rv_chart(iv_hist, rv_series), width="stretch")
    ivr = iv_rank_and_percentile(ticker)
    if ivr["iv_rank"] is not None:
        st.caption(f"IV rank: {fmt_pct(ivr['iv_rank'])} · IV percentile: {fmt_pct(ivr['iv_percentile'])} "
                   f"({ivr['n_observations']} observations)")
    else:
        st.caption(ivr["note"])

with col_r:
    st.plotly_chart(greeks_over_time_chart(spot, strike, real_dte, vol, rate), width="stretch")
    g1, g2, g3, g4 = st.columns(4)
    g1.metric("Delta", fmt_delta(-greeks.delta))
    g2.metric("Gamma", fmt_delta(-greeks.gamma, 4))
    g3.metric("Theta/day", fmt_currency(-greeks.theta * 100))
    g4.metric("Vega", fmt_currency(-greeks.vega * 100))
    st.caption("Short-put position (net of one contract, 100 shares).")

st.divider()

# --- P&L diagrams ---
pnl_l, pnl_r = st.columns(2)
with pnl_l:
    st.plotly_chart(pnl_at_expiration_chart(strike, mark, commission, spot), width="stretch")
with pnl_r:
    st.plotly_chart(pnl_over_time_chart(spot, strike, mark, real_dte, vol, rate, commission), width="stretch")

st.divider()

# --- Probability cone + backtest-calibrated stats ---
cone_l, cone_r = st.columns([2, 1])
with cone_l:
    st.plotly_chart(probability_cone_chart(daily, real_dte, vol), width="stretch")
with cone_r:
    st.markdown("**Backtest-calibrated (this ticker)**")
    bt = _cached_backtest(ticker)
    if "error" in bt:
        st.caption(f"Not available: {bt['error']}")
    else:
        st.metric("Win rate (expired OTM)", fmt_pct(bt["win_rate_expired_otm"]))
        st.metric("Assignment frequency", fmt_pct(bt["assignment_frequency"]))
        st.metric("Avg return / trade (ann.)", fmt_pct(bt["avg_realized_return_annualized"]))
        st.metric("Worst drawdown", fmt_currency(bt["worst_drawdown_dollars"]))
        st.caption(
            f"{bt['n_trades']} simulated weekly {bt['target_delta']:.2f}-delta / "
            f"{bt['target_dte']}-DTE trades. Simulated pricing (Black-Scholes off "
            f"trailing realized vol, {bt['vol_risk_premium_multiplier']:g}x risk-premium "
            f"multiplier) -- not real historical chains, see config.yaml `backtest` section."
        )

st.divider()

# --- Nearby strikes what-if table ---
st.subheader("Nearby strikes")
lo, hi = thr["delta_band"]
band = chain[(chain["expiration"] == expiration) & (chain["put_delta"].notna())
             & (chain["put_delta"] >= lo) & (chain["put_delta"] <= hi)].sort_values("strike_price")
if not band.empty:
    band = band.copy()
    band["ann_yield"] = band.apply(
        lambda r: (r["put_mark"] / r["strike_price"]) * (365.0 / real_dte)
        if real_dte > 0 and pd.notna(r.get("put_mark")) and r["put_mark"] > 0 else None, axis=1)
    band["prob_otm"] = band.apply(
        lambda r: probability_otm(spot, r["strike_price"], real_dte, r["put_iv"], rate, "put")
        if real_dte > 0 and pd.notna(r.get("put_iv")) and r["put_iv"] > 0 else None, axis=1)
    nearby = pd.DataFrame({
        "Strike": band["strike_price"],
        "Delta": band["put_delta"].map(fmt_delta),
        "Mark": band["put_mark"].map(fmt_currency),
        "OI": band["put_open_interest"],
        "Ann. yield": band["ann_yield"].map(lambda x: fmt_pct(x) if x == x else "--"),
        "Prob. OTM": band["prob_otm"].map(lambda x: fmt_pct(x) if x == x else "--"),
        "Selected": band["strike_price"].map(lambda s: "●" if s == strike else ""),
    })
    st.dataframe(nearby, width="stretch", hide_index=True)
else:
    st.info("No strikes in the configured delta band for this expiration.")

st.divider()

# --- Chain table with volume/OI overlay ---
st.subheader("Chain snapshot")
st.plotly_chart(chain_volume_oi_chart(chain, expiration), width="stretch")
exp_chain = chain[chain["expiration"] == expiration].sort_values("strike_price")
st.dataframe(exp_chain, width="stretch", height=400)
