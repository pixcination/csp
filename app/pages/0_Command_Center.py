"""
Command Center -- the single activation button.

Everything the run needs to tell you, in the order you need it:

  1. Can it run at all, and what is the market doing right now
  2. The button, with live per-stage progress
  3. What to do about positions you already have
  4. What the account can currently support
  5. New candidates

The session banner is deliberately specific. "Market is closed" tells you
nothing; "marks are from Friday's 16:00 close, 2 days stale, spreads will be
wider than they trade at Monday's open" tells you exactly how far to trust
the numbers below it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from analytics import regime, sizing  # noqa: E402
from analytics.exit_rules import recommended_starting_rules  # noqa: E402
from core import env  # noqa: E402
from core.market_calendar import classify, session_block  # noqa: E402
from core.paths import load_config, load_universe  # noqa: E402

st.set_page_config(page_title="Command Center", layout="wide")

from app.components.status import banner  # noqa: E402


# --- 1. Status -------------------------------------------------------------

st.title("Command Center")

info = classify()
severity, message = info.banner()
banner(severity, message,
       "Regular session open" if info.is_open else "Outside regular hours")

cred_ok, cred_report = env.doctor()
strays = env.stray_env_files()

status_cols = st.columns(4)
with status_cols[0]:
    st.metric("Session", info.state.value.replace("_", " "),
              delta=info.staleness_label, delta_color="off")
with status_cols[1]:
    st.metric("Credentials", "OK" if cred_ok else "Missing")
with status_cols[2]:
    reading = regime.current()
    st.metric("Volatility regime", reading.state.title(),
              delta=(f"{reading.term_ratio:.2f} term ratio"
                     if reading.term_ratio == reading.term_ratio else "no data"),
              delta_color="off")
with status_cols[3]:
    universe = load_universe()
    st.metric("Universe", f"{len(universe)} tickers")

if not cred_ok:
    banner("error", cred_report.splitlines()[-1], "Cannot run")
if strays:
    banner("warn",
           f"A second .env still exists at <code>{strays[0]}</code>. Nothing reads it, "
           f"but running an older script from that folder can rotate your live "
           f"TastyTrade token into it. Fix with "
           f"<code>python scripts/consolidate.py</code>.",
           "Duplicate credentials")
if reading.state != "calm":
    banner("warn" if reading.state == "stressed" else "info",
           reading.detail, reading.headline)

st.divider()

# --- 2. The button ---------------------------------------------------------

left, right = st.columns([1, 2])
with left:
    st.subheader("Refresh and analyse")
    quick = st.checkbox("Quick — analyse what is on disk", value=False,
                         help="Skips every refresh stage. Useful when you have "
                              "just run and only want to re-score.")
    force = st.checkbox("Force chain re-pull", value=False,
                         help="Ignores the staleness window. Outside regular hours "
                              "this only re-fetches the same closing marks.")
    subset = st.text_input("Tickers (blank = whole universe)", "")

    # Scan request (Phase 11): a saved request file, adjusted here.
    from analytics.scan_request import RequestError, ScanRequest
    from core import user_settings
    from core.paths import project_root
    with st.expander("Scan request", expanded=False):
        files = sorted((project_root() / "examples").glob("*.json"))
        source = st.selectbox("Start from", ["config default"] + [f.name for f in files])
        try:
            base = (ScanRequest.default() if source == "config default"
                    else ScanRequest.load(project_root() / "examples" / source))
        except (OSError, ValueError, RequestError) as exc:
            st.error(f"{source}: {exc}")
            base = ScanRequest.default()
        strategies = st.multiselect("Strategies", ["csp", "pcs"], default=base.strategies)
        lo, hi = base.dte_window()
        dte = st.slider("DTE window", 0, 90, (lo, hi))
        profiles = user_settings.profile_names()
        # Phase 17: an explicit choice, never a silent $3M default.
        preselect = base.account_profile if base.account_profile in profiles             and base.account_profile != "default" else None
        profile = st.selectbox("Account profile", profiles,
                               index=profiles.index(preselect) if preselect else None,
                               format_func=user_settings.profile_label,
                               placeholder="Choose the account to size for",
                               help="Required. Define profiles on the Settings page.")
        presets = list(user_settings.weight_presets())
        current = base.ranking_weights if isinstance(base.ranking_weights, str) else presets[0]
        weights = st.selectbox("Ranking weights", presets,
                               index=presets.index(current) if current in presets else 0,
                               help="Presets and your own weightings: Settings page.")
        all_names = st.checkbox("Pull chains for every eligible name",
                                value=base.top_n is None,
                                help="Most choice; ~7 min for the whole universe at "
                                     "weekly DTE. Untick to pull only the top N.")
        top_n = None if all_names else st.number_input(
            "Top N underlyings", 1, 200, value=base.top_n or 15)
    try:
        if profile is None:
            raise RequestError("choose an account profile")
        overrides = {"strategies": strategies, "account_profile": profile,
                     "ranking_weights": weights,
                     "top_n_underlyings": "all" if all_names else int(top_n)}
        if base.dte_targets and dte == (lo, hi):
            overrides["dte_targets"] = base.dte_targets
        else:
            overrides.update({"dte_targets": None, "dte_min": dte[0], "dte_max": dte[1]})
        scan = ScanRequest.from_dict({**base.to_dict(), **overrides})
        st.caption(f"Request: {scan.label()} · profile `{scan.account_profile}`")
    except RequestError as exc:
        scan = None
        st.error(f"Scan request: {exc}")
    go = st.button("Run", type="primary", width="stretch",
                    disabled=not cred_ok or scan is None)

with right:
    st.subheader("Progress")
    progress_box = st.container()

if go:
    from core.progress import StreamlitReporter
    from pipeline.run import STAGES, RunLocked, run

    tickers = [t.strip().upper() for t in subset.split(",") if t.strip()] or None
    reporter = StreamlitReporter(progress_box, STAGES)
    try:
        with st.spinner("Running..."):
            manifest = run(tickers, quick=quick, force_chains=force, reporter=reporter,
                           request=scan)
        st.session_state["last_manifest"] = manifest
        st.success(f"Finished in {manifest.elapsed_seconds:.0f}s — "
                   f"run {manifest.run_id}")
        for warning in manifest.warnings:
            st.warning(warning)
    except RunLocked as exc:
        st.error(str(exc))
    except Exception as exc:
        st.error(f"{type(exc).__name__}: {exc}")

st.divider()

# --- 3. Open positions -----------------------------------------------------

st.subheader("Open positions")

URGENCY_STYLE = {"act_now": ("Act now", "error"),
                  "attention": ("Watch", "warn"),
                  "routine": ("Routine", "ok")}

from app.components.run_state import active_run, analyse_stage, run_caption  # noqa: E402

manifest, _, source = active_run()
decisions = analyse_stage(manifest).get("open_positions", [])
run_caption(manifest, source)

if not decisions:
    st.caption("No open positions. Once the paper book has entries, every one is "
               "evaluated here on each run — hold, close, roll, or accept assignment, "
               "with the reasoning attached.")
else:
    order = {"act_now": 0, "attention": 1, "routine": 2}
    for item in sorted(decisions, key=lambda d: order.get(d.get("urgency"), 3)):
        label, style = URGENCY_STYLE.get(item.get("urgency", "routine"),
                                          ("Routine", "ok"))
        with st.container(border=True):
            head, action = st.columns([4, 1])
            head.markdown(f"**{item.get('headline','')}**")
            action.markdown(f"`{item.get('action','').upper()}`")
            st.caption(item.get("rationale", ""))
            metrics = st.columns(4)
            if item.get("prob_assignment") is not None:
                metrics[0].metric("Short-ITM odds" if item.get("strategy") == "pcs"
                                  else "Assignment odds", f"{item['prob_assignment']:.0%}")
            if item.get("edge_per_share") is not None:
                metrics[1].metric("Edge / share", f"${item['edge_per_share']:.2f}")
            if item.get("effective_n"):
                metrics[2].metric("Effective sample", f"{item['effective_n']}")
            if item.get("profit_pct") is not None:
                metrics[3].metric("Of max profit", f"{item['profit_pct']:.0%}",
                                  delta=f"${item.get('pnl_if_closed', 0):,.0f} net if closed",
                                  delta_color="off")
            rolls = item.get("roll_candidates")
            if rolls:
                st.markdown("Rolls for a net credit (same width, later expiry):")
                st.dataframe(pd.DataFrame(rolls), hide_index=True, width="stretch")
            elif item.get("action") == "roll" and item.get("strategy") == "pcs":
                st.caption("No roll in the stored chain pays a net credit: close instead.")

st.divider()

# --- 4. Capacity -----------------------------------------------------------

st.subheader("Account capacity")
capacity = sizing.capacity_report()
cfg = load_config().get("account", {})

cap_cols = st.columns(5)
cap_cols[0].metric("Net liq", f"${capacity['net_liquidating_value']:,.0f}")
cap_cols[1].metric("Deployable", f"${capacity['deployable_cash']:,.0f}",
                    delta=f"-${capacity['cash_buffer']:,.0f} buffer",
                    delta_color="off")
cap_cols[2].metric("Max strike", f"${capacity['max_tradable_strike']:,.0f}",
                    help="Highest strike one contract can be cash-secured at, "
                         "after the per-position concentration cap.")
cap_cols[3].metric("Positions",
                    f"{capacity['open_positions']}/{capacity['max_open_positions']}")
cap_cols[4].metric("Utilisation", f"{capacity['utilization']:.0%}")

_exec = load_config().get("execution", {})
st.caption(
    f"Signals generated here from **{_exec.get('data_source', 'tastytrade')}** data; "
    f"execution at **{_exec.get('venue', 'external')}**, entered by hand on the "
    f"Decisions page with the fill price overridable. Collateral is accounted "
    f"cash-secured (strike × 100) regardless of the executing account's margin "
    f"treatment — the conservative denominator, so a return is never flattered by "
    f"leverage that may not be extended. At "
    f"${capacity['net_liquidating_value']:,.0f} the per-position cap allows strikes "
    f"up to ${capacity['max_tradable_strike']:,.0f}, so capital no longer limits the "
    f"universe. **Liquidity does** — open interest and option volume now decide most "
    f"position sizes, and the Decisions page names which constraint bound each one.")

with st.expander("Which universe names fit the account?", expanded=False):
    import csv
    from core.paths import project_root
    tags_path = project_root() / "output" / "stage2_quality_tags_master.csv"
    if tags_path.exists() and universe:
        tags = {r["ticker"]: r for r in csv.DictReader(open(tags_path, encoding="utf-8"))}
        rows = [tags[t] for t in universe if t in tags and tags[t].get("last_price")]
        tradable, blocked = sizing.screen_universe_by_price(rows)
        a, b = st.columns(2)
        a.metric("Tradable", len(tradable))
        b.metric("Blocked on price", len(blocked))
        if blocked:
            st.caption("Blocked — one contract exceeds the per-position cap:")
            st.dataframe(
                [{"ticker": r["ticker"], "spot": float(r["last_price"]),
                  "collateral": r["collateral"],
                  "% of account": r["collateral"] / capacity["net_liquidating_value"]}
                 for r in sorted(blocked, key=lambda r: -r["collateral"])],
                hide_index=True, width="stretch")
    else:
        st.caption("Stage 2 tags not found — cannot compute the price screen.")

st.divider()

# --- 5. Rules --------------------------------------------------------------

with st.expander("Management rules currently in force"):
    st.caption(
        "Starting values derived from your cost structure and weekly cadence — not "
        "fitted optima. The canonical 45-DTE rules (manage at 50%, roll at 21 DTE) "
        "do not transfer to a 7-DTE book; the reasoning is in docs/. Everything here "
        "lives in config.yaml under `management:` and is meant to be swept once the "
        "wheel backtest exists.")
    for section, rules in recommended_starting_rules().items():
        st.markdown(f"**{section.title()}**")
        for rule, why in rules.items():
            st.markdown(f"- `{rule}` — {why}")

st.caption(f"Session block `{session_block()}` · "
           f"run `{manifest.run_id if manifest else 'none yet'}`")
