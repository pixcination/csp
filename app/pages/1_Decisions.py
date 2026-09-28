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

    for i, rec in enumerate(candidates):
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
                    "Contracts", min_value=1, value=int(rec["contracts"]), step=1,
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
                        run_id=manifest.run_id if manifest else None, notes=note)
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
        view = view.copy()
        if "rejections" in view:
            view["why_not"] = view["rejections"].map(
                lambda r: "; ".join(r) if r is not None and len(r) else "")
        columns = [c for c in ["ticker", "expiration", "strike", "dte_calendar",
                               "modelled_fill", "delta", "prob_otm_empirical",
                               "ev_annualised", "iv_rv_ratio", "ivr", "ivp", "open_interest",
                               "accepted", "best_per_ticker", "proposed", "why_not"]
                   if c in view.columns]
        st.dataframe(
            view[columns], hide_index=True, width="stretch",
            column_config={
                "prob_otm_empirical": st.column_config.NumberColumn("P(OTM)", format="percent"),
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
            "units, and drawdown. Weights are in config.yaml → underlying_rank and are "
            "**not yet validated** (Phase 14). Events and capital are gates, not "
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

# --- The book --------------------------------------------------------------

st.subheader("Paper book")

positions = paper.list_positions()
if positions.empty:
    st.caption("Nothing recorded yet.")
else:
    open_rows = positions[positions["status"] == "open"]
    stats = paper.performance()

    metrics = st.columns(5)
    metrics[0].metric("Open", f"{len(open_rows)}")
    metrics[1].metric("Closed", f"{stats.get('n_closed', 0)}")
    if stats.get("n_closed"):
        metrics[2].metric("Win rate", f"{stats['win_rate']:.0%}")
        metrics[3].metric("Realised", f"${stats['total_realized']:,.0f}")
        metrics[4].metric("Mean annualised", f"{stats['mean_annualised']:.1%}")

    show = ["id", "ticker", "expiration", "strike", "contracts", "modelled_fill",
            "actual_fill", "slippage", "status", "collateral", "rec_prob_otm"]
    st.dataframe(positions[[c for c in show if c in positions.columns]],
                  hide_index=True, width="stretch")

    if not open_rows.empty:
        with st.expander("Mark an outcome"):
            with st.form("close_position"):
                cols = st.columns([1, 1, 1, 1])
                pid = cols[0].selectbox(
                    "Position",
                    open_rows["id"].tolist(),
                    format_func=lambda i: (
                        f"#{i} " + " ".join(str(v) for v in open_rows.loc[
                            open_rows['id'] == i,
                            ['ticker', 'expiration', 'strike']].iloc[0])))
                status = cols[1].selectbox("Outcome",
                                            ["expired_otm", "closed_early",
                                             "assigned", "rolled"])
                price = cols[2].number_input("Paid to close", min_value=0.0,
                                              value=0.0, step=0.01, format="%.2f",
                                              help="0 for expiry or assignment.")
                when = cols[3].date_input("Date", value=dt.date.today())
                if st.form_submit_button("Record outcome"):
                    try:
                        paper.close_position(int(pid), status, when, price)
                        st.success(f"Position #{pid} marked {status}.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"{type(exc).__name__}: {exc}")

    lots = paper.list_share_lots()
    if not lots.empty:
        st.markdown("**Shares held from assignment**")
        st.caption("Adjusted basis is the strike less every premium collected against "
                    "the cycle. The covered-call side must never write below it "
                    "without flagging the locked-in loss.")
        st.dataframe(lots[["ticker", "shares", "acquired_date",
                            "acquisition_price", "adjusted_basis"]],
                      hide_index=True, width="stretch")

st.divider()

# --- Calibration -----------------------------------------------------------

st.subheader("Is the model telling the truth?")
left, right = st.columns(2)

with left:
    st.markdown("**Fill quality**")
    slip = paper.slippage_report()
    if slip.get("n"):
        st.metric("Mean slippage vs model", f"${slip['mean_slippage']:+.3f}",
                   delta=f"{slip['n']} recorded fills", delta_color="off")
        st.caption(slip["note"] + f" Current assumption: give up "
                                   f"{slip['assumed_fraction']:.0%} of the half-spread.")
    else:
        st.caption(slip.get("note", ""))

with right:
    st.markdown("**Probability calibration**")
    cal = paper.calibration()
    if cal.get("n_closed", 0) >= 10:
        st.metric("Predicted vs realised win rate",
                   f"{cal['win_rate']:.0%}",
                   delta=f"{cal['calibration_gap']:+.1%} vs predicted")
    st.caption(cal.get("verdict", ""))

st.caption(
    f"Execution mode: `{execution.get('mode', 'signal_only')}` — signals are "
    f"generated here and executed at `{execution.get('venue', 'external')}`. "
    f"Nothing on this page places an order.")
