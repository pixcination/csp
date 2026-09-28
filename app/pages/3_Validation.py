"""
Validation -- is the engine telling the truth?

Four questions, in the order they can be answered:

  1. Automation readiness. The explicit gates between here and unattended
     execution, and how many are met today.
  2. Calibration. Do the claimed probabilities match realised outcomes, and
     does the modelled fill match the real one?
  3. Regimes. Does each ticker's edge survive every environment, or did one
     bull run carry it?
  4. Walk-forward. Do the fitted parameters survive out of sample, or is the
     sweep selecting noise?

Everything here is evidence. Nothing on this page changes `config.yaml`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from analytics import calibration, iv_history  # noqa: E402
from core.paths import load_universe, output_dir  # noqa: E402

st.set_page_config(page_title="Validation", layout="wide")
st.title("Validation")
st.caption(
    "The strategy can be right and the model still wrong about it. These are the "
    "checks that separate the two — and none of them change a setting, because a "
    "parameter that affects every future trade should move on a decision, not a "
    "page load.")

report = calibration.report()

# --- 1. Automation readiness ----------------------------------------------

st.subheader("Automation readiness")
gate = report["ready_for_automation"]

progress = gate["passed"] / max(gate["total"], 1)
st.progress(progress, text=f"{gate['passed']} of {gate['total']} measurable gates passed")

for check in gate["checks"]:
    if check["pass"] is True:
        st.success(f"**{check['check']}** — {check['detail']}", icon=":material/check:")
    elif check["pass"] is None:
        st.info(f"**{check['check']}** — {check['detail']}",
                icon=":material/pending:")
    else:
        st.warning(f"**{check['check']}** — {check['detail']}",
                   icon=":material/priority_high:")

st.caption(gate["note"])
st.divider()

# --- 2. Calibration --------------------------------------------------------

st.subheader("Calibration")
performance = report["performance"]

if not performance.get("n_closed"):
    st.info(
        "No closed paper positions yet. Record outcomes on the Decisions page and "
        "this section becomes meaningful at roughly 25, trustworthy at 100. "
        "Nothing else in validation depends on it — the regime and walk-forward "
        "sections below can run today.",
        icon=":material/hourglass_empty:")
else:
    prob_col, fill_col = st.columns(2)

    with prob_col:
        st.markdown("**Probabilities**")
        probability = report["probability"]
        if probability:
            cols = st.columns(3)
            cols[0].metric("Predicted", f"{probability['mean_predicted']:.0%}")
            cols[1].metric("Realised", f"{probability['mean_actual']:.0%}",
                            delta=f"{probability['mean_actual'] - probability['mean_predicted']:+.1%}")
            cols[2].metric("Brier", f"{probability['brier']:.3f}",
                            help="Lower is better. Combines calibration error and "
                                 "discrimination into one number.")
            st.caption(probability["verdict"])

            buckets = report["reliability_buckets"]
            if buckets is not None and not buckets.empty:
                chart = buckets[["predicted", "actual"]].copy()
                chart["perfect"] = chart["predicted"]
                st.caption("Reliability curve — a calibrated model tracks the "
                           "diagonal. Points below it are over-confident.")
                st.line_chart(chart.set_index("predicted")[["actual", "perfect"]])
        else:
            st.caption("No closed positions carry a stored prediction yet.")

    with fill_col:
        st.markdown("**Fills**")
        fills = report["fills"]
        if fills:
            cols = st.columns(3)
            cols[0].metric("Assumed fraction", f"{fills['assumed_fraction']:.0%}")
            cols[1].metric("Implied fraction",
                            f"{fills['implied_fraction']:.0%}"
                            if fills["implied_fraction"] is not None else "--",
                            help="Recovered from the bid/ask each fill was taken "
                                 "against. This is a measurement, not an assumption.")
            cols[2].metric("Mean slippage", f"{fills['mean_slippage']:+.3f}")
            st.caption(fills["verdict"])
        else:
            st.caption("No recorded fills yet. Enter the price you actually got when "
                       "accepting a trade — leaving it at the modelled value tells "
                       "the calibration nothing.")

    by_strategy = report.get("by_strategy") or {}
    if len(by_strategy) > 1 or any(k != "csp" for k in by_strategy):
        st.markdown("**POP by strategy**")
        st.caption("A CSP's claim is P(expire OTM); a spread's is P(finish above "
                   "breakeven). Pooled, an error in one can hide in the other.")
        st.dataframe(pd.DataFrame([{"strategy": k.upper(), "n": v["n"],
                                    "predicted": v["mean_predicted"],
                                    "realised": v["mean_actual"], "brier": v["brier"],
                                    "verdict": v["verdict"]}
                                   for k, v in by_strategy.items()]),
                     hide_index=True, width="stretch",
                     column_config={"predicted": st.column_config.NumberColumn(format="percent"),
                                    "realised": st.column_config.NumberColumn(format="percent")})

    targets = report.get("targets") or {}
    st.markdown("**P(reach X% of max profit): predicted vs observed**")
    table = targets.get("table")
    if table is not None and not table.empty:
        st.dataframe(table, hide_index=True, width="stretch",
                     column_config={c: st.column_config.NumberColumn(format="percent")
                                    for c in ("predicted", "observed", "gap")})
    st.caption(targets.get("verdict", "") + " A target counts as a miss only when the "
               "position was held to expiry; closed early without reaching it is censored "
               "(the rest of the path was never seen).")

    if report["recommendations"]:
        st.markdown("**Suggested config changes**")
        st.caption("Evidence-backed, deliberately not applied automatically.")
        for rec in report["recommendations"]:
            with st.container(border=True):
                head = st.columns([3, 1, 1])
                head[0].markdown(f"`{rec['key']}`")
                head[1].metric("Current", f"{rec['current']}")
                head[2].metric("Suggested", f"{rec['suggested']}")
                st.caption(f"{rec['why']}  \n**Confidence:** {rec['confidence']}")

st.divider()

# --- 3. Regimes ------------------------------------------------------------

st.subheader("Regime consistency")
st.caption(
    "Two tickers can show the same fifteen-year return: one earned it steadily "
    "across nine environments, the other made everything in 2020–2021. A wheel "
    "holds through the bad periods rather than exiting them, so the difference "
    "matters more here than almost anywhere else.")

regime_path = output_dir() / "regime_consistency.csv"
if regime_path.exists():
    frame = pd.read_csv(regime_path)
    tier_cols = st.columns(4)
    for i, tier in enumerate(["core", "solid", "satellite", "avoid"]):
        count = int((frame["tier"] == tier).sum())
        tier_cols[i].metric(tier.title(), count)

    st.dataframe(
        frame[["ticker", "tier", "consistency_score", "regimes_positive",
               "regimes_covered", "median_annualised", "worst_regime_annualised",
               "worst_regime", "stress_median", "worst_cycle_days"]],
        hide_index=True, width="stretch")

    detail_path = output_dir() / "regime_detail.csv"
    if detail_path.exists():
        with st.expander("Per-regime detail"):
            detail = pd.read_csv(detail_path)
            chosen = st.selectbox("Ticker", sorted(detail["ticker"].unique()))
            st.dataframe(
                detail[detail["ticker"] == chosen][
                    ["label", "stress", "n_cycles", "annualised", "pct_profitable",
                     "assignment_rate", "worst_cycle_days", "drawdown"]],
                hide_index=True, width="stretch")
else:
    st.info("Not run yet. `python scripts/validate.py --regimes`",
            icon=":material/terminal:")

st.divider()

# --- 4. Walk-forward -------------------------------------------------------

st.subheader("Walk-forward")
st.caption(
    "A 36-cell sweep always produces a winner whether or not any real edge "
    "separates the cells. Walk-forward fits on a window and scores on the period "
    "after it, so the number reported is what the rule would actually have "
    "delivered. Degradation above roughly a third means the sweep is selecting noise.")

wf_path = output_dir() / "walkforward_summary.csv"
if wf_path.exists():
    frame = pd.read_csv(wf_path)
    cols = st.columns(4)
    cols[0].metric("Mean in-sample", f"{frame['mean_in_sample'].mean():.1%}")
    cols[1].metric("Mean out-of-sample", f"{frame['mean_out_of_sample'].mean():.1%}",
                    delta=f"{frame['mean_out_of_sample'].mean() - frame['mean_in_sample'].mean():+.1%}")
    cols[2].metric("Median degradation",
                    f"{frame['degradation_ratio'].median():.0%}")
    cols[3].metric("Parameter stability",
                    f"{frame['parameter_stability'].mean():.0%}",
                    help="How often the optimiser picked the same cell across folds. "
                         "Low stability means the grid is measuring noise even when "
                         "the strategy itself survives.")

    if frame["parameter_stability"].mean() < 0.35:
        st.warning(
            "Parameter selection is unstable across folds. The strategy may still "
            "work, but the specific optimum does not repeat — fix one defensible "
            "setting rather than re-tuning each time.", icon=":material/warning:")

    st.dataframe(
        frame[["ticker", "n_folds", "mean_in_sample", "mean_out_of_sample",
               "mean_baseline_oos", "degradation_ratio", "parameter_stability",
               "oos_positive_rate"]],
        hide_index=True, width="stretch")
else:
    st.info("Not run yet. `python scripts/validate.py --walk-forward`",
            icon=":material/terminal:")

st.divider()

# --- 5. IV rank coverage ---------------------------------------------------

st.subheader("IV rank coverage")
st.caption(
    f"IV rank needs {iv_history.MIN_OBSERVATIONS_FOR_RANK} regular-session captures "
    f"per ticker before it reports a number. Weekend and after-hours runs "
    f"deliberately do not count — they are last-trade echoes, and treating them as "
    f"samples is what would corrupt the metric.")

try:
    coverage = iv_history.coverage()
    if coverage.empty:
        st.caption("No universe loaded.")
    else:
        from data_sources import tasty_metrics
        tasty = tasty_metrics.latest()
        tasty_n = int(tasty["ivr"].notna().sum()) if not tasty.empty else 0
        active = int(coverage["active"].sum())
        cols = st.columns(4)
        cols[0].metric("Own IV rank active", f"{active}/{len(coverage)}")
        cols[1].metric("Median captures", f"{int(coverage['observations'].median())}")
        cols[2].metric("Median shortfall", f"{int(coverage['needed'].median())}")
        cols[3].metric("TastyTrade IVR available", f"{tasty_n}/{len(coverage)}")
        st.caption(
            "Since Phase 9 the engine carries TastyTrade's IV rank and percentile "
            "(`/market-metrics`, rank source 'tos', stored daily) on every candidate. "
            "Our own rank from captures stays as a cross-check and switches on per "
            "ticker once it has enough regular-session captures.")
        if not tasty.empty:
            coverage = coverage.merge(
                tasty[["symbol", "ivr", "ivr_tw", "ivp", "iv_index", "snapshot_date"]]
                .rename(columns={"symbol": "ticker", "ivr": "tasty_ivr",
                                 "ivr_tw": "tasty_ivr_tw", "ivp": "tasty_ivp",
                                 "snapshot_date": "tasty_as_of"}),
                on="ticker", how="left")
        st.dataframe(coverage, hide_index=True, width="stretch", column_config={
            "tasty_ivr": st.column_config.ProgressColumn(
                "Tasty IVR", min_value=0.0, max_value=1.0, format="%.2f"),
            "tasty_ivp": st.column_config.ProgressColumn(
                "Tasty IVP", min_value=0.0, max_value=1.0, format="%.2f"),
            "tasty_ivr_tw": st.column_config.NumberColumn("Tasty IVR (tw)", format="%.2f"),
        })
except Exception as exc:
    st.caption(f"Could not read IV history: {exc}")

# --- 6. Probability engine (Phase 13) ---------------------------------------

st.divider()
st.subheader("Probability engine: predicted vs observed")
st.caption("Walk-forward on real price history: at each entry date a synthetic 25-delta "
           "short put is priced at an IV proxy (20-day RV x the backtest VRP multiplier); "
           "models G / H / T predict using only data up to that date, and the actual path "
           "decides what happened. Run `python scripts/validate_prob_engine.py` "
           "(add `--dte 7` for weeklies) to refresh.")
try:
    import json

    from core.paths import validation_dir
    folder = validation_dir()
    found = sorted(folder.glob("prob_engine*summary*.json"))
    if not found:
        st.info("No validation run on disk yet.")
    for path in found:
        summary = json.loads(path.read_text(encoding="utf-8"))
        kind = ("put spread, long leg " + f"{summary.get('width_pct') or 0:.0%} below"
                if summary.get("strategy") == "pcs" else "short put")
        st.markdown(f"**{summary['dte']} DTE {kind}** · {', '.join(summary['tickers'])} · "
                    f"{summary['entries']:,} entries (~{summary['effective_entries_approx']:,} "
                    f"independent) · {summary['years']}y · run {summary['run_at']}")
        scores = pd.DataFrame(summary["scores"])
        st.dataframe(scores.pivot_table(index="target", columns="model", values="gap"),
                     width="stretch")
        st.caption("Mean predicted minus mean observed, by target (25 / 50 = reach that % of "
                   "max profit by expiry, 100 = expire worthless, touch = close at or below "
                   "the strike, max_loss = a spread finishing below its long strike). "
                   "Brier scores: " + ", ".join(
                       f"{r['model']}/{r['target']} {r['brier']:.3f}"
                       for r in summary["scores"] if r["target"] in ("50", "100")))
        cal_path = folder / path.name.replace("summary", "calibration").replace(".json", ".parquet")
        if cal_path.exists():
            import plotly.express as px
            cal = pd.read_parquet(cal_path)
            cal = cal[(cal["target"] == "100") & (cal["n"] >= 20)]
            fig = px.scatter(cal, x="predicted", y="observed", color="model", size="n",
                             labels={"predicted": "predicted P(expire worthless)",
                                     "observed": "observed frequency"})
            fig.add_shape(type="line", x0=0.6, y0=0.6, x1=1, y1=1, line=dict(dash="dot"))
            fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10))
            st.plotly_chart(fig, width="stretch")
        with st.expander("Caveats"):
            for caveat in summary.get("caveats", []):
                st.markdown(f"- {caveat}")
except Exception as exc:
    st.caption(f"Could not read the probability-engine validation: {exc}")

# --- 7. Ranking-weight calibration (Phase 14) ---------------------------------

st.divider()
st.subheader("Underlying ranking: which components predict outcomes?")
st.caption("From `python scripts/calibrate_rank_weights.py` (analytics/rank_calibration.py). "
           "On a shared calendar, each name gets a synthetic put 1 expected move out "
           "(IV = RV × 1.15). The table shows the mean cross-sectional rank correlation (IC) "
           "between a component's score on the entry date and the share of premium kept at "
           "expiry. Positive means a higher score went with keeping more.")
try:
    import json

    from core.paths import validation_dir
    folder = validation_dir()
    found = sorted(folder.glob("rank_calibration_ic_*d.csv"))
    if not found:
        st.caption("Not run yet.")
    for path in found:
        horizon = path.stem.rsplit("_", 1)[-1]
        ic = pd.read_csv(path)
        meta_path = folder / f"rank_calibration_{horizon}.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        st.markdown(f"**{horizon.removesuffix('d')} trading days** · {meta.get('symbols', '?')} names, "
                    f"{meta.get('dates', '?')} dates, {meta.get('entries', '?')} entries, "
                    f"breach rate {meta.get('breach_rate', float('nan')):.1%}")
        st.dataframe(ic, hide_index=True, width="stretch",
                     column_config={
                         "mean_ic": st.column_config.NumberColumn("Mean IC", format="%.3f"),
                         "t_stat": st.column_config.NumberColumn("t", format="%.1f"),
                         "hit_rate": st.column_config.NumberColumn("IC > 0", format="percent"),
                         "tercile_spread": st.column_config.NumberColumn(
                             "Top − bottom tercile (premium kept)", format="%.2f")})
        if meta.get("suggested_weights"):
            st.caption("Suggested weights (untestable components keep the default's share): "
                       + ", ".join(f"{k} {v:.2f}" for k, v in meta["suggested_weights"].items()))
    with st.expander("What this can and cannot show"):
        st.markdown(
            "- **iv_rank is a proxy here**: the percentile of 20-day realised vol within its "
            "trailing year, because no IV history exists before 2026.\n"
            "- **support is a proxy**: the nearest daily moving average below spot. The live "
            "component uses only *strong* levels, and strength comes from a full-history "
            "study that would leak the future.\n"
            "- **iv_rv and liquidity cannot be tested**: with IV proxied from RV the ratio is "
            "constant, and only today's liquidity rating exists.\n"
            "- A preset's IC here covers only its testable components.\n"
            "- The trade is priced at a fixed premium over RV, so this measures whether a name "
            "moved *less than its recent vol implied*. It cannot reward finding rich implied "
            "vol.")
except Exception as exc:
    st.caption(f"Could not read the ranking calibration: {exc}")


# --- 8. PCS rule backtest (Phase 15) ------------------------------------------

st.divider()
st.subheader("Put credit spreads: which management rules pay?")
st.caption("Every rule set (entry DTE x short delta x width x profit target x loss stop x "
           "breach close x time stop) simulated one spread at a time on real daily paths "
           "with synthetic Black-Scholes prices, then walked forward: the best set on 5 "
           "years is scored on the next 1, beside a fixed baseline. Run "
           "`python scripts/backtest_pcs.py` to refresh.")
try:
    import json

    from core.paths import validation_dir
    path = validation_dir() / "pcs_backtest_summary.json"
    if not path.exists():
        st.info("No PCS backtest on disk yet.")
    else:
        bt = json.loads(path.read_text(encoding="utf-8"))
        if "verdict" in bt:
            cols = st.columns(4)
            cols[0].metric("In-sample", f"{bt['mean_in_sample']:.1%}",
                           help="Annualised return on buying power of the best set, in "
                                "the window it was chosen on.")
            cols[1].metric("Out-of-sample", f"{bt['mean_out_of_sample']:.1%}",
                           delta=f"{bt['mean_out_of_sample'] - bt['mean_in_sample']:+.1%}")
            cols[2].metric("Fixed baseline OOS", f"{bt['mean_baseline_oos']:.1%}",
                           help=bt.get("baseline", ""))
            cols[3].metric("Folds", f"{bt['folds']}", delta=f"{len(bt.get('per_ticker', []))} "
                                                           f"tickers", delta_color="off")
            st.caption(f"{bt['verdict']} Baseline: {bt.get('baseline')}. Run {bt['run_at']}.")
        shape = bt.get("shape") or {}
        if shape:
            st.markdown("**The shape of the grid** (mean over every other setting and ticker)")
            axes = list(shape)
            columns = st.columns(min(len(axes), 4))
            for i, axis in enumerate(axes):
                with columns[i % len(columns)]:
                    frame = pd.DataFrame(shape[axis])
                    st.dataframe(frame, hide_index=True, width="stretch",
                                 column_config={
                                     "annualised_on_bpr": st.column_config.NumberColumn(
                                         "Annualised", format="percent"),
                                     "win_rate": st.column_config.NumberColumn(
                                         "Win", format="percent"),
                                     "pct_max_loss": st.column_config.NumberColumn(
                                         "Max loss", format="percent")})
        if bt.get("top_sets_in_sample"):
            with st.expander("Top rule sets over the full history (in-sample)"):
                st.dataframe(pd.DataFrame(bt["top_sets_in_sample"]), hide_index=True,
                             width="stretch")
        with st.expander("Caveats"):
            for caveat in bt.get("caveats", []):
                st.markdown(f"- {caveat}")
except Exception as exc:
    st.caption(f"Could not read the PCS backtest: {exc}")
