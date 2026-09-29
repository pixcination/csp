"""
Symbol Lookup (Phase 20B) -- any ticker's Outlook and candidate trades.

pipeline/lookup.py does the work with the modules the pipeline already
runs: check the symbol at Yahoo and TastyTrade, register it (tag `adhoc`,
kept out of `universe: all` and every auto preset until "Add to universe"),
bars / metrics / events, technicals and the level study, the Outlook dials,
a targeted chain capture, then CSP and put-spread candidates plus the
recommender's strategies. Shares the run lock and refuses while a scheduled
job runs or is due within 15 minutes. Anything logged from here -- or from
Trade Detail on a lookup run -- is sample `lookup`, left out of the
accuracy statistics.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from analytics.scan_request import ScanRequest  # noqa: E402
from core import user_settings  # noqa: E402
from pipeline import lookup  # noqa: E402

st.set_page_config(page_title="Symbol Lookup", layout="wide")
st.title("Symbol Lookup")
st.caption("Any ticker's Outlook and candidate trades, on demand. A symbol outside the "
           "universe is registered as **ad hoc**: it is left out of `universe: all`, the "
           "nightly jobs and every auto preset until you add it. Research tool only: nothing "
           "here places an order.")

# --- The form ------------------------------------------------------------------------------

defaults = ScanRequest.default()
from pipeline import scheduler  # noqa: E402

try:
    busy = scheduler.busy(within_minutes=15)
except Exception:
    busy = None
if busy:
    st.warning(f"Not now: {busy}. Lookups share the scheduled jobs' run lock; try again "
               f"after it finishes.")

with st.form("lookup"):
    c = st.columns([2, 2, 1, 1, 1, 1])
    symbol = c[0].text_input("Ticker", placeholder="e.g. PLTR, BRK.B, SPX")
    profiles = user_settings.profile_names()
    profile = c[1].selectbox("Account profile", profiles,
                             index=profiles.index(defaults.account_profile)
                             if defaults.account_profile in profiles else 0)
    dte_min = c[2].number_input("CSP DTE min", 0, 365, int(defaults.dte_min or 3))
    dte_max = c[3].number_input("CSP DTE max", 0, 365, int(defaults.dte_max or 11))
    pcs_dte = c[4].number_input("Spread DTE", 1, 365,
                                int((defaults.pcs_dte_targets or [45])[0]))
    width = c[5].number_input("Width % of spot", 0.5, 25.0,
                              float((defaults.spread_width_pct or [4.0])[0]), step=0.5)
    go = st.form_submit_button("Look up", type="primary", disabled=bool(busy))

progress = st.container()
if go and symbol.strip():
    from core.progress import StreamlitReporter
    from pipeline.run import RunLocked
    reporter = StreamlitReporter(progress, lookup.STAGES)
    try:
        with st.spinner(f"Looking up {symbol.strip().upper()}..."):
            out = lookup.run(symbol, profile, int(dte_min), int(dte_max), int(pcs_dte),
                             float(width), reporter=reporter)
        st.session_state["lookup_run"] = out.run_id
    except (lookup.LookupError_, RunLocked) as exc:
        st.error(str(exc))
    except Exception as exc:
        st.error(f"{type(exc).__name__}: {exc}")

recent = lookup.recent()
if recent:
    ids = [r["run_id"] for r in recent]
    current = st.session_state.get("lookup_run")
    picked = st.selectbox(
        "Lookup", ids, index=ids.index(current) if current in ids else 0,
        format_func=lambda i: next(f"{r['symbol']} · {r['finished_at'] or 'unfinished'} · "
                                   f"{(r['seconds'] or 0):.0f}s" for r in recent
                                   if r["run_id"] == i))
    st.session_state["lookup_run"] = picked
run_id = st.session_state.get("lookup_run")
if not run_id:
    st.info("Enter a ticker to look it up.")
    st.stop()

from pipeline.results import load_run  # noqa: E402

results = load_run(run_id)
if results is None:
    st.error(f"Lookup `{run_id}` not found.")
    st.stop()
manifest = results.manifest
sym = manifest.symbol
info = (manifest.stages or {}).get("lookup", {}) or {}
seconds = info.get("seconds") or manifest.elapsed_seconds or 0

# --- Header: registry state, time, notes ----------------------------------------------------

from data_sources import universe  # noqa: E402

reg = universe.get(sym) or {}
head = st.columns([3, 2])
head[0].subheader(sym)
head[0].caption(f"{reg.get('asset_class', '?')} · {reg.get('settlement', '?')}-settled · "
                f"profile `{results.request.get('account_profile')}` · run `{run_id}`")
timing = info.get("timings") or {}
head[1].metric("Lookup time", f"{seconds:.0f}s",
               delta=f"target {lookup.TARGET_SECONDS}s", delta_color="off",
               help="Per step: " + ", ".join(f"{k} {v:.0f}s" for k, v in timing.items()))
if universe.is_adhoc(reg.get("tags")):
    a = st.columns([1, 4])
    if a[0].button("Add to universe", type="primary"):
        universe.promote_adhoc(sym)
        st.success(f"{sym} is now part of the universe: `universe: all`, the nightly jobs and "
                   f"the archive include it from the next run.")
        st.rerun()
    a[1].caption("Ad hoc: outside `universe: all`, the nightly data stages, the chain archive "
                 "and every auto preset.")
for note in manifest.warnings or []:
    st.warning(note)

# --- Outlook ------------------------------------------------------------------------------

from analytics import outlook  # noqa: E402
from app.components import outlook_view  # noqa: E402

st.divider()
st.subheader("Outlook")
table = lookup.load_outlook(run_id)
rec = None
if table.empty:
    st.caption("No Outlook: it needs 300 daily bars.")
else:
    grid = [int(h) for h in sorted(table["horizon"].unique())]
    horizon = st.select_slider("Horizon (calendar days)", grid,
                               value=30 if 30 in grid else grid[0], key="lookup_h")
    mine = table[table["horizon"] == horizon]
    rec = mine.iloc[0].to_dict() if not mine.empty else None
    if info.get("skill_source") == "pooled":
        st.info("Skill here is the **pooled** estimate: this symbol has no walk-forward "
                "validation of its own, so its dials are shrunk by the universe's skill.")
    outlook_view.gauges(rec, key=f"lookup|{run_id}")

# --- Technicals, IV, events ---------------------------------------------------------------

from analytics import technical_study  # noqa: E402
from data_sources import events, tasty_metrics  # noqa: E402


def _num(value):
    value = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(value) else float(value)


st.divider()
st.subheader("Technicals and volatility")
tech = technical_study.load_latest(sym)
trow = tech.iloc[0].to_dict() if not tech.empty else {}
metrics = tasty_metrics.for_symbol(sym) or {}
m = st.columns(6)
m[0].metric("Trend", str(trow.get("trend_state") or "--"))
rsi, w_rsi = _num(trow.get("rsi_14")), _num(trow.get("w_rsi_14"))
m[1].metric("RSI 14 (daily)", f"{rsi:.0f}" if rsi is not None else "--")
m[2].metric("RSI 14 (weekly)", f"{w_rsi:.0f}" if w_rsi is not None else "--")
ivr, ivp = _num(metrics.get("ivr")), _num(metrics.get("ivp"))
m[3].metric("IV rank", f"{ivr:.0%}" if ivr is not None else "--")
m[4].metric("IV percentile", f"{ivp:.0%}" if ivp is not None else "--")
ratio = _num((rec or {}).get("vol_ratio"))
m[5].metric("IV / forecast RV", f"{ratio:.2f}x" if ratio is not None else "--",
            help="Relative richness at the selected horizon: IV over the forecast realised "
                 "vol (the Volatility dial ranks this ratio across the universe).")

support = technical_study.load_support(sym)
spot = _num((rec or {}).get("spot"))
st.markdown("**Support map** (Phase 10 level study)")
if support.empty:
    st.caption("No levels studied.")
else:
    view = support.sort_values("level", ascending=False)
    st.dataframe(view[[c_ for c_ in ["level_id", "timeframe", "level", "distance_pct",
                                     "slope_now", "n", "hold_rate", "edge_vs_placebo",
                                     "edge_ci_lo", "status", "strong", "summary"]
                       if c_ in view]],
                 hide_index=True, width="stretch", column_config={
                     "level": st.column_config.NumberColumn("Level", format="$%.2f"),
                     "distance_pct": st.column_config.NumberColumn("From spot", format="percent"),
                     "hold_rate": st.column_config.NumberColumn("Held", format="percent"),
                     "edge_vs_placebo": st.column_config.NumberColumn("Edge", format="percent"),
                     "edge_ci_lo": st.column_config.NumberColumn("Edge CI low",
                                                                 format="percent"),
                     "summary": st.column_config.TextColumn("Summary", width="large")})
    st.caption("Strong = an edge-vs-placebo CI above zero. Phase 10 found most MA support in "
               "this universe to be chance.")

st.markdown("**Upcoming events** (60 days; `*` = market-wide)")
upcoming = events.upcoming(sym, days=60)
if upcoming.empty:
    st.caption("None in the next 60 days.")
else:
    st.dataframe(upcoming[[c_ for c_ in ["date", "symbol", "type", "time_of_day", "confirmed",
                                         "note"] if c_ in upcoming]],
                 hide_index=True, width="stretch")

# --- Trades -------------------------------------------------------------------------------

st.divider()
st.subheader("Candidate trades")
sheet = results.candidates
census = ((manifest.stages or {}).get("analyse") or {}).get("census") or {}
if sheet.empty:
    st.caption(census.get("headline") or "No candidates: the chain produced no strikes in the "
                                          "window.")
else:
    if census.get("headline") and not sheet["accepted"].any():
        st.caption(census["headline"])
    sheet = sheet.sort_values(["accepted", "rank_key"] if "rank_key" in sheet else ["accepted"],
                              ascending=False).reset_index(drop=True)
    cols = [c_ for c_ in ["trade_id", "strategy", "expiration", "dte_calendar", "strike",
                          "long_strike", "width", "modelled_fill", "max_loss", "collateral",
                          "return_on_risk", "headline_annualised", "headline_ev", "pop_blend",
                          "p_hit_50_blend", "short_distance_em", "accepted", "why_not"]
            if c_ in sheet]
    pct = "percent"
    config = {
        "trade_id": None,
        "strategy": st.column_config.TextColumn("Strategy", pinned=True),
        "expiration": st.column_config.DateColumn("Expiry"),
        "dte_calendar": st.column_config.NumberColumn("DTE"),
        "strike": st.column_config.NumberColumn("Short K", format="%.2f"),
        "long_strike": st.column_config.NumberColumn("Long K", format="%.2f"),
        "width": st.column_config.NumberColumn("Width", format="%.2f"),
        "modelled_fill": st.column_config.NumberColumn("Credit (model)", format="$%.2f"),
        "max_loss": st.column_config.NumberColumn("Max loss", format="dollar"),
        "collateral": st.column_config.NumberColumn("BPR", format="dollar"),
        "return_on_risk": st.column_config.NumberColumn("RoR", format=pct),
        "headline_annualised": st.column_config.NumberColumn("Annualised", format=pct),
        "headline_ev": st.column_config.NumberColumn("EV", format="dollar"),
        "pop_blend": st.column_config.ProgressColumn("POP", min_value=0.0, max_value=1.0,
                                                     format="percent"),
        "p_hit_50_blend": st.column_config.ProgressColumn("P(50%)", min_value=0.0,
                                                          max_value=1.0, format="percent"),
        "short_distance_em": st.column_config.NumberColumn("EM distance", format="%.2f"),
        "accepted": st.column_config.CheckboxColumn("Passed"),
        "why_not": st.column_config.TextColumn("Rejected because", width="large"),
    }
    track = st.toggle("Track rows", value=False,
                      help="Select rows to log as tracked forward tests (sample `lookup`: "
                           "left out of the accuracy statistics).")
    event = st.dataframe(sheet[cols], hide_index=True, width="stretch", column_config=config,
                         on_select="rerun",
                         selection_mode="multi-row" if track else "single-row",
                         key=f"lookup_grid|{run_id}|{'track' if track else 'open'}")
    rows = getattr(getattr(event, "selection", None), "rows", None) or []
    if track:
        chosen = sheet.iloc[rows] if rows else sheet.iloc[0:0]
        if st.button(f"Track selected ({len(chosen)})", disabled=chosen.empty, type="primary"):
            from analytics import tracking
            outcome = tracking.log(chosen, run_id=run_id, preset="lookup")
            opened = sum(o["action"] == "opened" for o in outcome)
            st.success(f"{opened} tracked (sample lookup); see the Tracking page.")
            for o in outcome:
                if o["action"] == "skipped":
                    st.warning(o["message"])
    elif not rows:
        st.session_state.pop("lookup_opened", None)
    else:
        selection = {"run": run_id, "trade": str(sheet.iloc[rows[0]]["trade_id"])}
        if st.session_state.get("lookup_opened") != selection:
            st.session_state["lookup_opened"] = selection
            st.session_state["screener_selection"] = selection
            st.switch_page("pages/9_Trade_Detail.py", query_params=selection)
    st.caption("Select a row to open its Trade Detail. Trades recorded from a lookup, here or "
               "on Trade Detail, are sample `lookup`.")

strategies = results.strategies
st.markdown("**Strategy recommender**")
if strategies is None or strategies.empty:
    conditions = results.strategy_conditions
    st.caption("No strategy's entry conditions fit this symbol today."
               if conditions is None or conditions.empty else
               "No strategy passed; the conditions table below says why.")
    if conditions is not None and not conditions.empty:
        st.dataframe(conditions, hide_index=True, width="stretch")
else:
    st.dataframe(strategies[[c_ for c_ in ["label", "legs", "expiration", "headline_policy",
                                           "headline_ev", "pop_blend", "accepted",
                                           "why_not"] if c_ in strategies]],
                 hide_index=True, width="stretch", column_config={
                     "headline_ev": st.column_config.NumberColumn("EV", format="dollar"),
                     "pop_blend": st.column_config.ProgressColumn(
                         "POP", min_value=0.0, max_value=1.0, format="percent")})
