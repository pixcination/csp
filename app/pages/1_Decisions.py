"""
Decisions -- the ranked trade sheet, and the accept/override flow.

This is where a run becomes an action. Each proposed trade shows the strike,
the modelled fill, the size and what capped it, the empirical odds, and the
expected value after every fee. Accepting one records it in the paper book
with the fill you actually got in the executing account -- not the fill the
model assumed.

That override field is the most important control on the page. Every yield the
tool displays rests on a slippage assumption, and the only way to find out
whether it is right is to keep recording what really happened.

Phase 14: the trade selected on the Screener (or sent from Trade Detail) is
shown first, with the same accept/override form.

2026-09-29: the paper book's sections (ledger, outcomes, rolls, marks,
share lots, calibration) moved to the Tracking page.

Phase 11: the run's scan request is named at the top, the full sheet keeps
every accepted strike (with a "best per ticker" toggle), and the underlying
ranking that chose which chains to pull is shown with its component scores.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from analytics import paper  # noqa: E402
from core.market_calendar import classify  # noqa: E402
from core.paths import load_config  # noqa: E402

st.set_page_config(page_title="Decisions", layout="wide")
st.title("Decisions")

info = classify()
if not info.is_open:
    st.warning(f"{info.banner()[1]}", icon=":material/schedule:")

from app.components.run_state import active_run, analyse_stage, run_caption  # noqa: E402

manifest, run_results, source = active_run()
candidates = analyse_stage(manifest).get("candidates", [])
considered = analyse_stage(manifest).get("candidates_considered", 0)
run_caption(manifest, source)

# Phase 14: the trade picked on the Screener / Trade Detail comes first, with
# the same card and accept form as the run's own proposals.
picked = st.session_state.get("screener_selection") or {}
if picked.get("trade"):
    from analytics import trade_detail as td
    from pipeline.results import load_run
    picked_run = (run_results if run_results is not None and run_results.run_id == picked["run"]
                  else load_run(picked["run"]))
    picked_rec = td.record(picked_run, picked["trade"]) if picked_run is not None else None
    if picked_rec is not None:
        picked_rec["_run_id"] = picked["run"]
        picked_rec["_from_screener"] = True
        candidates = [picked_rec] + [c for c in candidates
                                     if c.get("trade_id") != picked["trade"]]
        cols = st.columns([4, 1])
        cols[0].info(f"Selected on the Screener: **{td.escape_md(td.trade_label(picked_rec))}** "
                     f"(run `{picked['run']}`) -- shown first below.",
                     icon=":material/filter_alt:")
        if cols[1].button("Clear selection"):
            st.session_state.pop("screener_selection", None)
            st.rerun()

request = (getattr(manifest, "request", None) or {}) if manifest else {}
if request:
    dte = (f"{', '.join(map(str, request['dte_targets']))} DTE"
           if request.get("dte_targets") else
           f"{request.get('dte_min')}-{request.get('dte_max')} DTE")
    st.caption(
        f"Scan request: **{request.get('name') or 'default'}** · "
        f"{' + '.join(s.upper() for s in request.get('strategies', []))} · {dte} · "
        f"risk mode `{request.get('risk_mode')}` · profile `{request.get('account_profile')}` · "
        f"top {request.get('top_n_underlyings')} underlyings")
    if "csp" not in request.get("strategies", []):
        st.info("This run did not request CSP. PCS trades are constructed from "
                "Phase 12; until then a PCS request ranks underlyings and pulls "
                "their chains only (see the ranking below).")

execution = load_config().get("execution", {})
allow_override = execution.get("allow_manual_fill_override", True)

# --- Proposed trades -------------------------------------------------------

st.subheader("Proposed trades")

if not candidates:
    st.info("No proposals yet. Run the pipeline from the Command Center — "
            "candidates are ranked at the end of each run.")
else:
    st.caption(f"{len(candidates)} proposed from {considered} candidate(s) that "
               f"passed every entry gate. Ranked by expected value annualised on "
               f"collateral, after fees and after charging the full empirical loss "
               f"tail — so the ranking never assumes a favourable second leg.")

    policies_all = run_results.prob_policies if run_results is not None else pd.DataFrame()
    metrics_all = run_results.prob_metrics if run_results is not None else pd.DataFrame()
    curves_all = run_results.prob_curves if run_results is not None else pd.DataFrame()

    def probability_panel(rec: dict) -> None:
        """Phase 13: the three models side by side, the blend, and every
        management policy net of fees."""
        trade_id = rec.get("trade_id")
        if not trade_id or metrics_all.empty:
            return
        if rec.get("pop_blend") is not None:
            st.markdown(
                f"**Probabilities (blend):** P(profit at expiry) {rec['pop_blend']:.0%} · "
                + " · ".join(f"P({t}%) {rec[f'p_hit_{t}_blend']:.0%}"
                             for t in (25, 30, 50, 100) if rec.get(f"p_hit_{t}_blend") is not None)
                + f" · headline `{rec.get('headline_policy')}`: EV "
                  f"${rec.get('headline_ev') or 0:,.0f} over ~{rec.get('headline_days') or 0:.0f} days")
        with st.expander("Probability table — models G / H / T, policies, curves"):
            m = metrics_all[metrics_all["trade_id"] == trade_id]
            if not m.empty:
                table = m.pivot_table(index="metric", columns="model", values="value",
                                      aggfunc="first")
                order = [c for c in ["G", "H", "T", "blend"] if c in table.columns]
                st.dataframe(table[order], width="stretch")
            st.caption(rec.get("prob_labels", "") + (f" · T: {rec['t_flag']}"
                                                     if rec.get("t_flag") else ""))
            st.caption("G = what option prices imply (risk-neutral at the short leg's IV: its "
                       "EV is ~minus costs by construction); H = this stock's own history at a "
                       "similar volatility; T = H from days in a similar technical state. "
                       "100% = expire worthless, only reached by holding to expiry. Touch is "
                       "measured on daily closes and understates intraday touches.")
            pol = policies_all[(policies_all["trade_id"] == trade_id)
                               & (policies_all["model"] == "blend")]
            if not pol.empty:
                st.markdown("**Management policies (blend, net of all fees)**")
                st.dataframe(pol[["policy", "ev", "p_profit", "days", "annualised",
                                  "net_gain_when_hit", "below_min_gain"]],
                             hide_index=True, width="stretch",
                             column_config={
                                 "ev": st.column_config.NumberColumn("EV $", format="$%.0f"),
                                 "p_profit": st.column_config.NumberColumn("P(profit)", format="percent"),
                                 "days": st.column_config.NumberColumn("Days held", format="%.1f"),
                                 "annualised": st.column_config.NumberColumn("Annualised", format="percent"),
                                 "net_gain_when_hit": st.column_config.NumberColumn("Net $ if target hit", format="$%.0f"),
                                 "below_min_gain": st.column_config.CheckboxColumn("Below min gain")})
            cur = curves_all[curves_all["trade_id"] == trade_id]
            if not cur.empty:
                import plotly.express as px
                fig = px.line(cur.sort_values("day"), x="day", y="prob", color="model",
                              facet_col="target", labels={"day": "calendar days",
                                                          "prob": "P(reached by day)"})
                fig.update_layout(height=260, margin=dict(l=10, r=10, t=30, b=10))
                st.plotly_chart(fig, width="stretch")

    def spread_accept_form(rec: dict, key: str) -> None:
        """Record a spread to the multi-leg paper book (Phase 15): the net
        credit you got, or the two leg fills."""
        with st.form(f"accept_spread_{key}"):
            fields = st.columns([1, 1, 1, 1])
            contracts = fields[0].number_input(
                "Contracts", min_value=1, value=max(int(rec["contracts"] or 0), 1), step=1,
                key=f"sc{key}")
            fill = fields[1].number_input(
                "Actual net credit", min_value=0.0, value=float(rec["modelled_fill"]),
                step=0.01, format="%.2f", key=f"sf{key}", disabled=not allow_override,
                help="Short leg sold minus long leg bought, per share. Calibrates the "
                     "package slippage model.")
            entry = fields[2].date_input("Entry date", value=dt.date.today(), key=f"sd{key}")
            note = fields[3].text_input("Note", "", key=f"sn{key}")
            submitted = st.form_submit_button("Accept and record the spread", type="primary")
        if submitted:
            try:
                used_real_fill = abs(fill - float(rec["modelled_fill"])) > 1e-9
                result = paper.accept(
                    rec, contracts=int(contracts),
                    actual_fill=float(fill) if used_real_fill else None, entry_date=entry,
                    run_id=rec.get("_run_id") or (manifest.run_id if manifest else None),
                    notes=note)
                st.success(result.message)
            except Exception as exc:
                st.error(f"{type(exc).__name__}: {exc}")

    def pcs_card(rec: dict, key: str = "") -> None:
        """A put credit spread proposal (Phase 12), recordable since Phase 15."""
        with st.container(border=True):
            head = st.columns([3, 1])
            head[0].markdown(
                f"### {rec['ticker']} &nbsp; <span style='font-size:.7em;opacity:.7'>"
                f"{rec['expiration']} &nbsp;${rec['strike']:g}/${rec['long_strike']:g} put "
                f"spread · {rec['width']:g} wide · {rec.get('tier', '')}</span>",
                unsafe_allow_html=True)
            head[1].metric("EV annualised on risk", f"{rec['ev_annualised']:.1%}")
            cols = st.columns(6)
            cols[0].metric("Contracts", f"{rec['contracts']:,}")
            cols[1].metric("Credit", f"${rec['modelled_fill']:.2f}",
                           delta=f"natural ${rec['natural']:.2f}", delta_color="off")
            cols[2].metric("Max loss", f"${rec['max_loss']:,.0f}")
            cols[3].metric("POP (> breakeven)", f"{rec['prob_otm_empirical']:.0%}"
                           if rec.get("prob_otm_empirical") is not None else "--")
            cols[4].metric("P(max loss)", f"{rec['prob_max_loss']:.0%}"
                           if rec.get("prob_max_loss") is not None else "--")
            cols[5].metric("Credit / width", f"{rec['credit_width']:.0%}")
            st.caption(rec.get("rationale", ""))
            probability_panel(rec)
            chips = [f"short strike: {rec.get('strike_rule_reason', '')}"]
            if rec.get("short_distance_em") == rec.get("short_distance_em") and                     rec.get("short_distance_em") is not None:
                chips.append(f"{rec['short_distance_em']:+.2f} EM ({rec.get('em_method')})")
            if rec.get("strong_support_id"):
                chips.append(f"strong support {rec['strong_support_id']} "
                             f"${rec['strong_support_level']:,.2f}")
            if rec.get("premium_flags"):
                chips.append(f"premium: {rec['premium_flags']}")
            if rec.get("fillability") is not None:
                chips.append(f"fillability {rec['fillability']:.2f} (weakest: "
                             f"{rec.get('weakest_leg', '')})")
            st.markdown(" · ".join(f"`{c}`" for c in chips))
            for warning in rec.get("warnings", []) or []:
                st.warning(warning)
            for note in rec.get("notes", []) or []:
                st.caption(note)
            spread_accept_form(rec, key)

    for i, rec in enumerate(candidates):
        if rec.get("strategy") == "pcs":
            pcs_card(rec, str(i))
            continue
        with st.container(border=True):
            head = st.columns([3, 1])
            head[0].markdown(
                f"### {rec['ticker']} &nbsp; "
                f"<span style='font-size:.7em;opacity:.7'>"
                f"{rec['expiration']} &nbsp;${rec['strike']:g} put</span>",
                unsafe_allow_html=True)
            head[1].metric("EV annualised",
                            f"{rec['ev_annualised']:.1%}"
                            if rec.get("ev_annualised") == rec.get("ev_annualised")
                            else "--")

            cols = st.columns(6)
            cols[0].metric("Contracts", f"{rec['contracts']:,}")
            cols[1].metric("Fill", f"${rec['modelled_fill']:.2f}",
                            delta=f"bid ${rec['bid']:.2f}" if rec.get("bid") else None,
                            delta_color="off")
            cols[2].metric("Net credit", f"${rec['net_credit']:,.0f}")
            cols[3].metric("Collateral", f"${rec['collateral']:,.0f}")
            cols[4].metric("P(finish OTM)",
                            f"{rec['prob_otm_empirical']:.0%}"
                            if rec.get("prob_otm_empirical") is not None else "--")
            cols[5].metric("IV / RV",
                            f"{rec['iv_rv_ratio']:.2f}"
                            if rec.get("iv_rv_ratio") else "--")

            st.caption(rec.get("rationale", ""))
            probability_panel(rec)

            chips = []
            if rec.get("ivr") is not None and rec.get("ivr") == rec.get("ivr"):
                chips.append(f"IV rank {rec['ivr']:.0%} · IV percentile "
                             f"{rec.get('ivp') or 0:.0%} (TastyTrade)")
            for event in rec.get("events") or ():
                chips.append(f"event: {event}")
            if not rec.get("scales", True):
                chips.append(f"⚠ size capped by {rec.get('binding_constraint','')} — "
                              f"this trade does not scale with more capital")
            if rec.get("prob_touch") is not None:
                chips.append(f"{rec['prob_touch']:.0%} chance of touching the strike "
                              f"intraday (management risk, not assignment risk)")
            if rec.get("cost_drag"):
                chips.append(f"fees are {rec['cost_drag']:.1%} of gross")
            if rec.get("basis_quality"):
                chips.append(f"assignment basis ${rec['effective_basis']:.2f} "
                              f"({rec['basis_quality']})")
            if rec.get("effective_n"):
                chips.append(f"sample: {rec.get('sample_label','')}, "
                              f"~{rec['effective_n']} independent windows")
            if chips:
                st.markdown(" · ".join(f"`{c}`" for c in chips))

            for warning in rec.get("warnings", []) or []:
                st.warning(warning)

            with st.form(f"accept_{i}"):
                fields = st.columns([1, 1, 1, 1])
                contracts = fields[0].number_input(
                    "Contracts", min_value=1, value=max(int(rec["contracts"] or 0), 1), step=1,
                    key=f"c{i}",
                    help="Override if you took a different size than recommended.")
                fill = fields[1].number_input(
                    "Actual fill", min_value=0.0, value=float(rec["modelled_fill"]),
                    step=0.01, format="%.2f", key=f"f{i}",
                    disabled=not allow_override,
                    help="What you really got in the executing account. This is what "
                         "calibrates the slippage model — leave it at the modelled "
                         "value only if you did not actually trade it.")
                entry = fields[2].date_input("Entry date", value=dt.date.today(),
                                              key=f"d{i}")
                note = fields[3].text_input("Note", "", key=f"n{i}")
                submitted = st.form_submit_button("Accept and record",
                                                   type="primary")
            if submitted:
                try:
                    used_real_fill = abs(fill - float(rec["modelled_fill"])) > 1e-9
                    result = paper.accept(
                        rec, contracts=int(contracts),
                        actual_fill=float(fill) if used_real_fill else None,
                        entry_date=entry,
                        run_id=rec.get("_run_id") or (manifest.run_id if manifest else None),
                        notes=note)
                    st.success(result.message)
                except Exception as exc:
                    st.error(f"{type(exc).__name__}: {exc}")

# --- The full sheet --------------------------------------------------------

sheet = run_results.candidates if run_results is not None else pd.DataFrame()
if not sheet.empty and run_results.has_full_sheet:
    passed = int(sheet["accepted"].sum()) if "accepted" in sheet else 0
    with st.expander(f"Every strike evaluated in this run ({len(sheet):,}; "
                     f"{passed:,} passed the gates)"):
        toggles = st.columns(2)
        only_passed = toggles[0].toggle("Only strikes that passed every gate", value=False)
        best_only = toggles[1].toggle(
            "Best per ticker", value=False,
            help="Only the top-ranked accepted strike of each ticker -- the rows "
                 "portfolio construction proposes from.")
        view = sheet[sheet["accepted"]] if only_passed else sheet
        if best_only:
            flag = "best_per_ticker" if "best_per_ticker" in view else "selected"
            view = view[view[flag].fillna(False).astype(bool)] if flag in view else view
        from analytics.probabilities import SORTS
        sorts = {k: v for k, v in SORTS.items() if k in view.columns}
        if sorts:
            sort_key = st.selectbox("Sort by", list(sorts), format_func=sorts.get,
                                    help="The default is the engine's ranking: blended EV per "
                                         "calendar day held, per dollar of buying power. "
                                         "Rejected strikes always sort last.")
            view = view.assign(_rejected=~view["accepted"].astype(bool)).sort_values(
                ["_rejected", sort_key], ascending=[True, False]).drop(columns="_rejected")
        view = view.copy()
        if "rejections" in view:
            view["why_not"] = view["rejections"].map(
                lambda r: "; ".join(r) if r is not None and len(r) else "")
        columns = [c for c in ["ticker", "strategy", "expiration", "strike", "long_strike",
                               "width", "tier", "dte_calendar", "modelled_fill", "delta",
                               "ev_per_day_bpr", "headline_policy", "headline_ev",
                               "headline_annualised", "pop_blend", "p_hit_50_blend",
                               "median_days_50_blend", "p_touch_blend",
                               "prob_otm_empirical", "prob_max_loss", "credit_width",
                               "ev_annualised", "short_distance_em", "strike_rule",
                               "iv_rv_ratio", "ivr", "ivp", "open_interest", "fillability",
                               "premium_flags", "accepted", "best_per_ticker",
                               "default_choice", "proposed", "why_not"]
                   if c in view.columns]
        st.dataframe(
            view[columns], hide_index=True, width="stretch",
            column_config={
                "prob_otm_empirical": st.column_config.NumberColumn("P(OTM) / POP", format="percent"),
                "prob_max_loss": st.column_config.NumberColumn("P(max loss)", format="percent"),
                "ev_per_day_bpr": st.column_config.NumberColumn("EV/day/BPR", format="%.5f"),
                "headline_ev": st.column_config.NumberColumn("Headline EV", format="$%.0f"),
                "headline_annualised": st.column_config.NumberColumn("Headline ann.", format="percent"),
                "pop_blend": st.column_config.NumberColumn("POP blend", format="percent"),
                "p_hit_50_blend": st.column_config.NumberColumn("P(50%)", format="percent"),
                "median_days_50_blend": st.column_config.NumberColumn("Days to 50%", format="%.1f"),
                "p_touch_blend": st.column_config.NumberColumn("P(touch)", format="percent"),
                "credit_width": st.column_config.NumberColumn("Credit/width", format="percent"),
                "short_distance_em": st.column_config.NumberColumn("Short (EM)", format="%.2f"),
                "ev_annualised": st.column_config.NumberColumn("EV ann.", format="percent"),
                "modelled_fill": st.column_config.NumberColumn("Fill", format="$%.2f"),
                "why_not": st.column_config.TextColumn("Rejected because", width="large"),
            })

# --- The underlying ranking (Phase 11) --------------------------------------

ranked = run_results.underlyings if run_results is not None else pd.DataFrame()
if not ranked.empty:
    chosen = int(ranked["selected"].sum()) if "selected" in ranked else 0
    with st.expander(f"Underlying ranking ({len(ranked)} names; chains pulled for "
                     f"the top {chosen})"):
        st.caption(
            "Scored from data on disk, without chains: IV rank/percentile, IV/RV, "
            "liquidity rating, trend state, nearest strong support in expected-move "
            "units, and drawdown. Weights are in config.yaml → underlying_rank; the Phase 14 "
            "calibration (Validation page) backs the IV-rank component only. Events and capital are gates, not "
            "penalties: an excluded name says why.")
        only_eligible = st.toggle("Only eligible names", value=True)
        view = ranked[ranked["eligible"]] if only_eligible else ranked
        columns = [c for c in ["rank", "symbol", "selected", "score", "coverage",
                               "strategies", "event_status", "score_iv_rank",
                               "score_iv_rv", "score_liquidity", "score_trend",
                               "score_support", "score_drawdown", "ivr", "iv_rv",
                               "liquidity_rating", "trend_state", "support_level_id",
                               "support_distance_em", "em_pct", "max_drawdown",
                               "event_notes", "exclusion"] if c in view.columns]
        st.dataframe(
            view[columns], hide_index=True, width="stretch",
            column_config={
                "score": st.column_config.ProgressColumn("Score", min_value=0.0,
                                                         max_value=1.0, format="%.2f"),
                "coverage": st.column_config.NumberColumn("Coverage", format="percent"),
                "ivr": st.column_config.NumberColumn("IVR", format="percent"),
                "em_pct": st.column_config.NumberColumn("EM", format="percent"),
                "max_drawdown": st.column_config.NumberColumn("Max DD", format="percent"),
                "support_distance_em": st.column_config.NumberColumn("Support (EM)",
                                                                     format="%.2f"),
                "event_notes": st.column_config.TextColumn("Events", width="large"),
                "exclusion": st.column_config.TextColumn("Excluded because", width="large"),
            })

st.divider()

# --- The book (moved to Tracking) ------------------------------------------

st.info("The paper book -- its performance, the ledger, outcomes, rolls, marks, share lots "
        "and the calibration panel -- is on the **Tracking** page now.")
if st.button("Open Tracking", icon=":material/arrow_forward:"):
    try:
        st.switch_page("pages/11_Tracking.py")
    except Exception:
        st.info("Open Tracking from the sidebar.")

st.caption(
    f"Execution mode: `{execution.get('mode', 'signal_only')}` — signals are "
    f"generated here and executed at `{execution.get('venue', 'external')}`. "
    f"Nothing on this page places an order.")
