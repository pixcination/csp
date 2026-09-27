"""Scanner page -- ranked/filterable CSP candidate table (Option Samurai's
"Scan the market" equivalent, scoped to wheel-strategy-only). See
docs/PROJECT_SPEC.md Page 1."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import streamlit as st

from analytics.config import load_config, project_root
from analytics.data_access import load_scanner_universe, list_snapshot_dates
from analytics.scoring import compute_composite_scores
from app.components.formatting import fmt_pct, fmt_delta, fmt_currency, fmt_dollars_compact
from app.components.jobs import start_background_script, job_status, is_running


@st.cache_data(ttl=300, show_spinner="Scoring candidates...")
def _scored_universe(tickers: tuple[str, ...]) -> pd.DataFrame:
    return compute_composite_scores(list(tickers))

st.title("Scanner")
st.caption("Ranked CSP candidates from the Stage 1-3 screening pipeline "
           "(output/stage3_candidates.csv), refreshed on demand below.")

cfg = load_config()
proj = project_root()
presets = cfg.get("scanner_presets", [])

# --- Controls row ---
ctrl_l, ctrl_r = st.columns([2, 1])

with ctrl_l:
    preset_names = [p["name"] for p in presets]
    preset_name = st.selectbox("Saved scan preset", preset_names, index=0)
    preset = next(p for p in presets if p["name"] == preset_name)

    universe_options = ["All", "Tier 1", "Tier 2", "Custom"]
    default_idx = {"all": 0}.get(preset["universe"], 3)
    universe_choice = st.radio("Universe", universe_options, index=default_idx, horizontal=True)

    custom_tickers = ""
    if universe_choice == "Custom":
        custom_tickers = st.text_input("Tickers (comma-separated)", placeholder="AAPL, SPY, MSFT")

with ctrl_r:
    dates = list_snapshot_dates()
    last_scan = dates[-1] if dates else "never"
    st.metric("Latest Stage 3 chain scan", last_scan)

st.divider()

# --- Data refresh controls ---
with st.expander("Data refresh controls", expanded=False):
    r1, r2, r3 = st.columns(3)

    with r1:
        st.write("**Sync 1-minute price data**")
        st.caption("Copies fresh 1-minute files for the finalized universe "
                   "from the source drive (scripts/03).")
        if st.button("Run sync", disabled=is_running("sync_1m"), key="btn_sync_1m"):
            start_background_script(
                "sync_1m", proj / "scripts" / "03_copy_selected_tickers.py", proj)
            st.rerun()

    with r2:
        st.write("**Run Stage 3 chain scan**")
        st.caption("Pulls fresh option chains via TastyTrade. "
                   "Takes roughly 60-100 minutes for the full universe.")
        if st.button("Run scan", disabled=is_running("stage3_scan"), key="btn_scan"):
            start_background_script(
                "stage3_scan", proj / "scripts" / "04_stage3_chain_scan.py", proj)
            st.rerun()

    with r3:
        st.write("**Re-apply liquidity filter**")
        st.caption("Re-screens the most recent chain scan against current "
                   "config.yaml thresholds without re-pulling data.")
        if st.button("Re-screen", disabled=is_running("rescreen"), key="btn_rescreen"):
            start_background_script(
                "rescreen", proj / "scripts" / "05_stage3_screen.py", proj)
            st.rerun()

    for key, label in [("sync_1m", "Sync"), ("stage3_scan", "Stage 3 scan"), ("rescreen", "Re-screen")]:
        status, tail = job_status(key)
        if status == "idle":
            continue
        badge = {"running": "🔵 running", "done": "🟢 done", "failed": "🔴 failed"}[status]
        st.write(f"**{label}:** {badge}")
        if tail:
            st.code(tail, language=None)
        if status == "running":
            st.rerun()

st.divider()

# --- Build the table ---
universe_arg = {
    "All": "all", "Tier 1": "tier1", "Tier 2": "tier2", "Custom": custom_tickers,
}[universe_choice]

already_explained = False
if preset["universe"] == "trade_log_assigned":
    st.info("This preset ranks positions currently marked \"assigned\" in the "
            "Trade Log as covered-call roll candidates. Log some positions on "
            "the Trade Log page first.")
    df, already_explained = pd.DataFrame(), True
elif universe_choice == "Custom" and not custom_tickers.strip():
    st.info("Enter one or more tickers above to filter the scan.")
    df, already_explained = pd.DataFrame(), True
else:
    try:
        df = load_scanner_universe(universe_arg)
    except FileNotFoundError as e:
        st.error(str(e))
        df, already_explained = pd.DataFrame(), True

if df.empty:
    if not already_explained:
        st.warning("No candidates match this universe/preset.")
else:
    scored = _scored_universe(tuple(sorted(df["ticker"].tolist())))
    df = df.merge(scored, on="ticker", how="left")
    df = df.sort_values("composite_score", ascending=False)

    display = pd.DataFrame({
        "Ticker": df["ticker"],
        "Score": df.get("composite_score").round(1),
        "Quality tier": df.get("quality_tier"),
        "Category": df.get("category"),
        "ADV ($)": df.get("adv_90d_dollars").map(fmt_dollars_compact),
        "Last price": df.get("last_price").map(fmt_currency),
        "Ann. yield": df.get("annualized_yield").map(lambda x: fmt_pct(x)),
        "IV rank": df.get("iv_rank").map(lambda x: fmt_pct(x) if x == x else "n/a (<10 scans)"),
        "20d RV": df.get("rv_20d_annualized").map(lambda x: fmt_pct(x)),
        "Prob. OTM (theo.)": df.get("prob_otm_theoretical").map(lambda x: fmt_pct(x)),
        "Put strike": df.get("nearest_put_strike").map(fmt_currency),
        "Put delta": df.get("nearest_put_delta").map(fmt_delta),
        "DTE (now)": df.get("put_dte"),
        "OI @ target delta": df.get("oi_at_target_delta"),
        "Spread %": df.get("spread_pct_at_target_delta").map(lambda x: fmt_pct(x, already_pct=True)),
        "Downtrend flag": df.get("downtrend_flag").map(lambda x: "⚠️" if x is True else ("" if x is False else "n/a")),
    })
    st.caption(
        f"{len(display)} candidate(s), ranked by composite score "
        f"(yield {cfg['scoring']['weights']['yield']:.0%} / IV rank {cfg['scoring']['weights']['iv_rank']:.0%} / "
        f"liquidity {cfg['scoring']['weights']['liquidity']:.0%} / technical {cfg['scoring']['weights']['technical']:.0%} / "
        f"prob-OTM {cfg['scoring']['weights']['prob_otm']:.0%}). IV rank needs 10+ accumulated Stage 3 "
        f"scans per ticker to populate -- see config.yaml to retune weights."
    )
    st.dataframe(display, width="stretch", hide_index=True, height=560)
