"""
Screener -- the landing page (Phase 14, roadmap C.6).

Ask the question, run it, pick a trade:

  1. Status: the session banner from Command Center, credentials, the
     volatility regime and a data-freshness line that says what a full run
     will download before you press Run.
  2. The scan request form: strategy, DTE range or targets, risk mode and
     value, spread widths, profit targets, account profile, universe, top N,
     ranking weights, event-policy overrides, strike rule. Requests save and
     load by name (config/user_settings.yaml -> scan_presets); the files in
     examples/ are offered as read-only starting points.
  3. Run, with the same per-stage progress as Command Center.
  4. The results grid: every candidate of the run (several per ticker),
     blended probabilities as progress bars, filters, a best-per-ticker and a
     group-by-ticker toggle, CSV/Excel export. Selecting a row opens Trade
     Detail with `?run=<id>&trade=<trade id>`, so the URL is bookmarkable for
     as long as the run folder exists.

The grid reads the persisted run (pipeline.results), not session state, so a
browser refresh keeps it. Nothing on this page places an order.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from analytics import sizing, trade_detail as td  # noqa: E402
from analytics.scan_request import (RISK_MODES, STRIKE_RULES, UNIVERSE_KEYWORDS,  # noqa: E402
                                    RequestError, ScanRequest)
from app.components.run_state import active_run, run_caption  # noqa: E402
from app.components.status import banner, freshness_summary, session_banner  # noqa: E402
from core import env, user_settings  # noqa: E402
from core.paths import load_config  # noqa: E402

st.set_page_config(page_title="Screener", layout="wide")
st.title("Screener")

# --- 1. Status ----------------------------------------------------------------

info = session_banner()
cred_ok, cred_report = env.doctor()
fresh_level, fresh_text, fresh_rows = freshness_summary()
cols = st.columns([1, 1, 3])
cols[0].metric("Credentials", "OK" if cred_ok else "Missing")
try:
    from analytics import regime
    reading = regime.current()
    cols[1].metric("Volatility regime", reading.state.title())
except Exception:
    cols[1].metric("Volatility regime", "--")
with cols[2]:
    st.caption(("✓ " if fresh_level == "ok" else "⚠ ") + fresh_text)
    if fresh_rows:
        with st.expander("Freshness per cache"):
            st.dataframe(pd.DataFrame([{"cache": r.label, "status": r.status, "last": r.last,
                                        "age": r.age, "detail": r.detail}
                                       for r in fresh_rows]),
                         hide_index=True, width="stretch")
if not cred_ok:
    banner("error", cred_report.splitlines()[-1], "Cannot run")

st.divider()

# --- 2. The request form ---------------------------------------------------------

saved = user_settings.scan_presets()
examples = user_settings.example_requests()
sources = (["config default"] + [f"saved: {n}" for n in saved]
           + [f"example: {n}" for n in examples])

top = st.columns([2, 1, 1])
source = top[0].selectbox("Start from", sources,
                          help="A saved request (yours) or an example file. Every field "
                               "below can be changed before running.")
try:
    if source.startswith("saved: "):
        base = ScanRequest.from_dict(saved[source.removeprefix("saved: ")])
    elif source.startswith("example: "):
        base = ScanRequest.from_dict(examples[source.removeprefix("example: ")])
    else:
        base = ScanRequest.default()
except RequestError as exc:
    st.error(f"{source}: {exc}")
    base = ScanRequest.default()
if base.inherit_warning():
    st.warning(f"{source}: {base.inherit_warning()}. Saving it again writes every field.")
# Widget keys carry the source, so switching presets resets every field.
k = f"scr|{source}|"

with st.container(border=True):
    row = st.columns(4)
    strategy_choice = {"csp": "CSP", "pcs": "PCS"}
    current = ("Both" if set(base.strategies) == {"csp", "pcs"}
               else strategy_choice.get(base.strategies[0], "CSP"))
    strat = row[0].radio("Strategy", ["CSP", "PCS", "Both"],
                         index=["CSP", "PCS", "Both"].index(current), horizontal=True,
                         key=k + "strat")
    strategies = {"CSP": ["csp"], "PCS": ["pcs"], "Both": ["csp", "pcs"]}[strat]

    dte_mode = row[1].radio("DTE", ["Range", "Targets"],
                            index=1 if base.dte_targets else 0, horizontal=True,
                            key=k + "dtemode")
    lo, hi = base.dte_window()
    if dte_mode == "Range":
        dte_range = row[1].slider("DTE window", 0, 120,
                                  (base.dte_min if base.dte_min is not None else lo,
                                   base.dte_max if base.dte_max is not None else hi),
                                  key=k + "dte")
        dte_targets_text = ""
    else:
        dte_targets_text = row[1].text_input(
            "DTE targets", ", ".join(str(t) for t in (base.dte_targets or [7, 30, 45])),
            key=k + "dtet", help=f"Expirations within ±{base.tolerance} days of each target.")
        dte_range = None

    risk_mode = row[2].selectbox("Risk mode", RISK_MODES,
                                 index=RISK_MODES.index(base.risk_mode), key=k + "risk")
    delta_range = base.delta_range or [-0.30, -0.12]
    min_pop = base.min_pop
    max_loss = base.max_loss_per_trade
    max_pct = base.max_pct_capital
    if risk_mode == "delta_range":
        delta_range = list(row[2].slider("Short put delta", -0.60, -0.02,
                                         (float(delta_range[0]), float(delta_range[1])),
                                         step=0.01, key=k + "delta"))
    elif risk_mode == "min_pop":
        min_pop = row[2].slider("Minimum P(profit)", 0.50, 0.99, float(min_pop or 0.80),
                                step=0.01, key=k + "pop")
    elif risk_mode == "max_loss_per_trade":
        max_loss = row[2].number_input("Max loss per trade ($)", 50.0, 1_000_000.0,
                                       float(max_loss or 1000.0), step=50.0, key=k + "maxloss")
    else:
        max_pct = row[2].number_input("Max % of capital per trade", 0.1, 100.0,
                                      float((max_pct or 0.05) * 100), step=0.5,
                                      key=k + "maxpct") / 100.0

    rule = row[3].selectbox("Strike rule", STRIKE_RULES,
                            index=STRIKE_RULES.index(base.strike_rule), key=k + "rule",
                            help="conservative = the lowest short strike any rule gives.")
    em_multiple = base.em_multiple
    if rule in ("em_multiple", "conservative"):
        em_multiple = row[3].number_input("EM multiple", 0.25, 3.0, float(base.em_multiple),
                                          step=0.25, key=k + "emm")

    row = st.columns(4)
    no_pcs = "pcs" not in strategies
    width_unit = row[0].radio("Spread widths in", ["% of spot", "$"],
                              index=0 if base.spread_width_pct else 1, horizontal=True,
                              key=k + "wunit", disabled=no_pcs)
    width_default = (base.spread_width_pct if width_unit == "% of spot" and base.spread_width_pct
                     else [4.0] if width_unit == "% of spot" else base.spread_widths)
    widths_text = row[0].text_input(f"Spread widths ({width_unit})",
                                    ", ".join(f"{w:g}" for w in width_default),
                                    key=k + "widths" + width_unit, disabled=no_pcs)
    pcs_dte_text = row[0].text_input(
        "Spread DTE targets", ", ".join(str(t) for t in (base.pcs_dte_targets or [])),
        key=k + "pcsdte", disabled=no_pcs,
        help="Spreads enter at these DTEs (± tolerance) whatever the DTE window above. "
             "Leave blank to use the DTE window for spreads too.")
    targets = row[1].multiselect("Profit targets (% of max)", [10, 25, 30, 40, 50, 60, 75, 100],
                                 default=[t for t in base.profit_targets
                                          if t in (10, 25, 30, 40, 50, 60, 75, 100)],
                                 key=k + "targets")
    # Phase 17: no silent default. Sizing, caps and permissions all follow the
    # profile, so the Screener preselects one only when the loaded request names
    # a profile other than the shipped research `default` ($3M).
    profiles = user_settings.profile_names()
    preselect = base.account_profile if base.account_profile in profiles         and base.account_profile != "default" else None
    profile = row[2].selectbox("Account profile", profiles,
                               index=profiles.index(preselect) if preselect else None,
                               format_func=user_settings.profile_label,
                               placeholder="Choose the account to size for",
                               key=k + "profile",
                               help="Required. Profiles are defined on the Settings page; "
                                    "`default` is the $3M research account.")
    presets = list(user_settings.weight_presets())
    current_w = (base.ranking_weights if isinstance(base.ranking_weights, str)
                 else user_settings.default_weight_preset())
    weights = row[3].selectbox("Ranking weights", presets,
                               index=presets.index(current_w) if current_w in presets else 0,
                               key=k + "weights",
                               help="Which names get a chain pull. Calibration: Validation page.")

    row = st.columns(4)
    universe_options = list(UNIVERSE_KEYWORDS) + ["custom list"]
    base_universe = base.universe if isinstance(base.universe, str) else "custom list"
    if base_universe not in universe_options:
        universe_options.insert(0, base_universe)          # e.g. tag:<name>
    universe_pick = row[0].selectbox("Universe", universe_options,
                                     index=universe_options.index(base_universe),
                                     key=k + "universe")
    universe: str | list[str] = universe_pick
    if universe_pick == "custom list":
        tickers_text = row[0].text_input(
            "Tickers", ", ".join(base.universe) if isinstance(base.universe, list) else "",
            key=k + "ulist")
        universe = [t.strip().upper() for t in tickers_text.split(",") if t.strip()]
    all_names = row[1].checkbox("Chains for every eligible name", value=base.top_n is None,
                                key=k + "all",
                                help="Most choice; about 7 minutes for the whole universe at "
                                     "weekly DTE. Untick to pull the top N only.")
    top_n = None if all_names else row[1].number_input("Top N underlyings", 1, 300,
                                                       value=base.top_n or 15, key=k + "topn")
    name = row[2].text_input("Request name", base.name, key=k + "name")

    with row[3].popover("Event overrides"):
        policy = load_config().get("event_policy", {}) or {}
        overrides = {}
        st.caption("Change how an event inside the trade window is treated for this "
                   "request only. Defaults: config.yaml → event_policy.")
        for kind, rule_cfg in policy.items():
            default = (base.event_policy_overrides.get(kind, {}) or {}).get(
                "action", rule_cfg.get("action", "ignore"))
            choice = st.selectbox(kind, ["block", "warn", "ignore"],
                                  index=["block", "warn", "ignore"].index(default),
                                  key=k + f"ev_{kind}")
            if choice != rule_cfg.get("action"):
                overrides[kind] = {"action": choice}

    row = st.columns([2, 1, 1])
    try:
        from analytics import strategy_spec as _ss
        spec_names = list(_ss.load_all())
    except Exception:
        spec_names = []
    spec_pick = row[0].multiselect(
        "Also scan strategy specs", spec_names,
        default=[s for s in (base.specs or []) if s in spec_names], key=k + "specs",
        help="Phase 16: condors, calendars, strangles... from strategies/*.yaml, resolved "
             "on every name's chain. Results on the Strategies page.")
    recommend = row[1].checkbox("Recommend by conditions", value=bool(base.recommend),
                                key=k + "recommend",
                                help="Every spec whose IV-regime / trend / earnings "
                                     "conditions a name meets.")

# Build and validate the request.
scan, problem = None, None
try:
    fields = {**base.to_dict(),
              "strategies": strategies, "risk_mode": risk_mode, "delta_range": delta_range,
              "min_pop": min_pop, "max_loss_per_trade": max_loss, "max_pct_capital": max_pct,
              **({"spread_width_pct": [float(w) for w in widths_text.replace(" ", "").split(",")
                                       if w]}
                 if width_unit == "% of spot" else
                 {"spread_width_pct": None,
                  "spread_widths": [float(w) for w in widths_text.replace(" ", "").split(",")
                                    if w]}),
              "pcs_dte_targets": [int(t) for t in pcs_dte_text.replace(" ", "").split(",")
                                  if t] or None,
              "profit_targets": sorted(int(t) for t in targets) or [50],
              "account_profile": profile, "universe": universe,
              "top_n_underlyings": "all" if all_names else int(top_n),
              "ranking_weights": weights, "event_policy_overrides": overrides,
              "strike_rule": rule, "em_multiple": float(em_multiple), "name": name,
              "specs": list(spec_pick), "recommend": bool(recommend)}
    if dte_range is not None:
        fields.update({"dte_targets": None, "dte_min": int(dte_range[0]),
                       "dte_max": int(dte_range[1])})
    else:
        fields.update({"dte_targets": [int(t) for t in dte_targets_text.replace(" ", "")
                                       .split(",") if t], "dte_min": None, "dte_max": None})
    if profile is None:
        raise RequestError("choose an account profile -- contract counts, caps and "
                           "permissions all follow it")
    scan = ScanRequest.from_dict(fields)
except (RequestError, ValueError) as exc:
    problem = str(exc)

if problem:
    st.error(f"Scan request: {problem}")
else:
    if (sizing.account_config(scan.account_profile) or {}).get("placeholder"):
        st.warning(f"Profile `{scan.account_profile}` still has placeholder values -- "
                   f"enter the real account value and limits on the Settings page.")
    st.caption(f"Request: **{scan.label()}** · profile `{scan.account_profile}` · "
               f"universe `{scan.universe if isinstance(scan.universe, str) else len(scan.universe)}`")

act = st.columns([1, 1, 1, 2])
with act[0].popover("Save request", disabled=scan is None):
    save_name = st.text_input("Save as", (source.removeprefix("saved: ")
                                          if source.startswith("saved: ") else ""),
                              help="lower-case letters, digits and _")
    if st.button("Save", key="save_preset"):
        try:
            stored = user_settings.save_scan_preset(save_name, scan.to_dict())
            st.success(f"Saved `{stored}`.")
        except user_settings.SettingsError as exc:
            st.error(str(exc))
if source.startswith("saved: "):
    if act[1].button("Delete saved request"):
        user_settings.delete_scan_preset(source.removeprefix("saved: "))
        st.rerun()
with act[2].popover("Run options"):
    quick = st.checkbox("Quick: analyse what is on disk", value=False,
                        help="Skips every refresh stage.")
    force = st.checkbox("Force chain re-pull", value=False)
    subset = st.text_input("Only these tickers (blank = the request's universe)", "")
go = act[3].button("Run scan", type="primary", width="stretch",
                   disabled=not cred_ok or scan is None)

progress_box = st.container()
if go:
    from core.progress import StreamlitReporter
    from pipeline.run import STAGES, RunLocked, run

    tickers = [t.strip().upper() for t in subset.split(",") if t.strip()] or None
    with progress_box:
        st.caption(("Data current -- no refresh needed. " if fresh_level == "ok" or quick
                    else fresh_text + " ") + "Progress per stage:")
    reporter = StreamlitReporter(progress_box, STAGES)
    try:
        with st.spinner("Running..."):
            manifest = run(tickers, quick=quick, force_chains=force, reporter=reporter,
                           request=scan)
        st.session_state["last_manifest"] = manifest
        st.success(f"Finished in {manifest.elapsed_seconds:.0f}s — run {manifest.run_id}")
        for warning in manifest.warnings:
            st.warning(warning)
    except RunLocked as exc:
        st.error(str(exc))
    except Exception as exc:
        st.error(f"{type(exc).__name__}: {exc}")

st.divider()

# --- 3. Results ----------------------------------------------------------------------

st.subheader("Results")
manifest, results, source_label = active_run()
run_caption(manifest, source_label)
if results is None or results.candidates.empty:
    st.info("No results yet. Run a scan above; the grid lists every candidate it built.")
    st.stop()

request = results.request or {}
if request:
    st.caption(f"This run asked: **{request.get('name') or 'unnamed request'}** · "
               f"{' + '.join(s.upper() for s in request.get('strategies', []))} · "
               + (f"{', '.join(map(str, request['dte_targets']))} DTE"
                  if request.get("dte_targets") else
                  f"{request.get('dte_min')}-{request.get('dte_max')} DTE")
               + f" · risk `{request.get('risk_mode')}` · profile "
                 f"`{request.get('account_profile')}`")

grid = td.screener_grid(results.candidates)
f = st.columns([1.2, 2, 1, 1, 1])
strategies_present = sorted(grid["strategy"].unique())
pick_strat = f[0].multiselect("Strategy", strategies_present, default=strategies_present)
pick_tickers = f[1].multiselect("Tickers", sorted(grid["ticker"].unique()))
min_pop_f = f[2].number_input("Min POP", 0.0, 1.0, 0.0, step=0.05)
max_dte_f = f[3].number_input("Max DTE", 0, 365, int(grid["dte_calendar"].max() or 0))
from analytics.probabilities import SORTS  # noqa: E402
sorts = {key: label for key, label in SORTS.items() if key in grid.columns}
sort_key = f[4].selectbox("Sort", list(sorts), format_func=sorts.get)

t = st.columns(5)
accepted_only = t[0].toggle("Only rows that passed every gate", value=True)
log_mode = t[4].toggle("Log mode", value=False, key="screener_log_mode",
                       help="Phase 18: select several rows and log them as tracked forward "
                            "tests (Tracking page). Off: selecting a row opens Trade Detail.")
best_only = t[1].toggle("Best per ticker", value=False)
group = t[2].toggle("Group by ticker", value=False)
proposed_only = t[3].toggle("Only proposed", value=False)

view = td.filter_grid(grid, pick_strat, pick_tickers, accepted_only, proposed_only,
                      best_only, min_pop_f or None, max_dte_f or None, sort_key, group)
st.caption(f"{len(view):,} of {len(grid):,} rows · "
           + ("select rows to log them." if log_mode else
              "select a row to open its Trade Detail."))

pct = "percent"
config = {
    "trade_id": None,
    "ticker": st.column_config.TextColumn("Ticker", pinned=True),
    "strategy": st.column_config.TextColumn("Strategy"),
    "expiration": st.column_config.DateColumn("Expiry"),
    "dte_calendar": st.column_config.NumberColumn("DTE"),
    "strike": st.column_config.NumberColumn("Short K", format="%.2f"),
    "long_strike": st.column_config.NumberColumn("Long K", format="%.2f"),
    "width": st.column_config.NumberColumn("Width", format="%.2f"),
    "modelled_fill": st.column_config.NumberColumn("Credit (model)", format="$%.2f"),
    "net_mid": st.column_config.NumberColumn("Credit (mid)", format="$%.2f"),
    "max_loss": st.column_config.NumberColumn("Max loss", format="dollar"),
    "collateral": st.column_config.NumberColumn("BPR", format="dollar"),
    "return_on_risk": st.column_config.NumberColumn("RoR", format=pct),
    "headline_annualised": st.column_config.NumberColumn("Annualised", format=pct),
    "headline_ev": st.column_config.NumberColumn("EV", format="dollar"),
    "ev_per_day_bpr": st.column_config.NumberColumn("EV/day/BPR", format="%.5f"),
    "pop_blend": st.column_config.ProgressColumn("POP", min_value=0.0, max_value=1.0,
                                                 format="percent"),
    "p_hit_25_blend": st.column_config.ProgressColumn("P(25%)", min_value=0.0, max_value=1.0,
                                                      format="percent"),
    "p_hit_30_blend": st.column_config.ProgressColumn("P(30%)", min_value=0.0, max_value=1.0,
                                                      format="percent"),
    "p_hit_50_blend": st.column_config.ProgressColumn("P(50%)", min_value=0.0, max_value=1.0,
                                                      format="percent"),
    "p_hit_100_blend": st.column_config.ProgressColumn("P(100%)", min_value=0.0,
                                                       max_value=1.0, format="percent"),
    "median_days_50_blend": st.column_config.NumberColumn("Days to 50%", format="%.1f"),
    "ivr": st.column_config.NumberColumn("IVR", format=pct),
    "iv_rv_ratio": st.column_config.NumberColumn("IV/RV", format="%.2f"),
    "short_distance_em": st.column_config.NumberColumn("EM distance", format="%.2f"),
    "support": st.column_config.TextColumn("Nearest support"),
    "fillability": st.column_config.ProgressColumn("Liquidity", min_value=0.0, max_value=1.0,
                                                   format="%.2f"),
    "event_flags": st.column_config.TextColumn("Events", width="medium"),
    "accepted": st.column_config.CheckboxColumn("Passed"),
    "proposed": st.column_config.CheckboxColumn("Proposed"),
    "best_per_ticker": st.column_config.CheckboxColumn("Best of ticker"),
    "why_not": st.column_config.TextColumn("Rejected because", width="large"),
}
event = st.dataframe(view, hide_index=True, width="stretch", height=520,
                     column_config=config, on_select="rerun",
                     selection_mode="multi-row" if log_mode else "single-row",
                     key=f"grid|{results.run_id}|{'log' if log_mode else 'open'}")

exp = st.columns([1, 1, 4])
exp[0].download_button("CSV", view.drop(columns="trade_id").to_csv(index=False).encode(),
                       file_name=f"screener_{results.run_id}.csv", mime="text/csv")
exp[1].download_button("Excel", td.export_excel(view.drop(columns="trade_id")),
                       file_name=f"screener_{results.run_id}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

rows = getattr(getattr(event, "selection", None), "rows", None) or []
if log_mode:
    # Phase 18: log rows as tracked forward tests (analytics/tracking.py).
    from analytics import tracking
    full = results.candidates.set_index("trade_id", drop=False)
    preset = request.get("name") or None
    lc = st.columns(3)
    chosen_ids = [str(view.iloc[i]["trade_id"]) for i in rows]
    outcome = None
    if lc[0].button(f"Log selected ({len(chosen_ids)})", disabled=not chosen_ids,
                    type="primary"):
        outcome = tracking.log(full.loc[chosen_ids], run_id=results.run_id, preset=preset)
    passing = [t for t in view.loc[view["accepted"].astype(bool), "trade_id"]]
    if lc[1].button(f"Log every passing row shown ({len(passing)})", disabled=not passing):
        outcome = tracking.log(full.loc[passing], run_id=results.run_id, preset=preset)
    cfg_t = load_config().get("tracking", {}) or {}
    if lc[2].button(f"Log all: top {cfg_t.get('top_k', 5)} + control sample",
                    help="Review C.3: the top K passing rows of the whole run, M random "
                         "passing rows below them and M near-miss rejects, so the accuracy "
                         "log measures the model and not only the favourites."):
        outcome = tracking.log_all(results.candidates, run_id=results.run_id, preset=preset)
    if outcome is not None:
        opened = sum(o["action"] == "opened" for o in outcome)
        seen = sum(o["action"] == "observed" for o in outcome)
        st.success(f"{opened} new tracked position(s), {seen} already tracked (observation "
                   f"added). See the Tracking page.")
        for o in outcome:
            if o["action"] == "skipped":
                st.warning(o["message"])
elif not rows:
    st.session_state.pop("screener_opened", None)
else:
    trade_id = str(view.iloc[rows[0]]["trade_id"])
    selection = {"run": results.run_id, "trade": trade_id}
    # A selection kept in widget state after "back" must not bounce straight
    # to Trade Detail again: open each selection once.
    if st.session_state.get("screener_opened") != selection:
        st.session_state["screener_opened"] = selection
        st.session_state["screener_selection"] = selection
        st.switch_page("pages/9_Trade_Detail.py", query_params=selection)
    st.caption(f"Selected `{trade_id}` -- open it on the Trade Detail page, or send it to "
               f"Decisions from there.")
