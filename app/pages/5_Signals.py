"""
Signals -- moving-average levels (Phase 10), overnight gap risk, put skew,
and single-name term structure.

Three reads that were computable from data already on disk and had never been
taken. Gap risk comes from the 4.6 GB 1-minute archive; skew and term structure
come from the chain snapshots. None of them need accumulated history.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from analytics import gaps, skew  # noqa: E402
from core.paths import load_config, load_universe, output_dir  # noqa: E402

st.set_page_config(page_title="Signals", layout="wide")
st.title("Signals")

cfg = load_config().get("signals", {})
universe = load_universe()

tab_levels, tab_gaps, tab_skew = st.tabs(
    ["Levels", "Overnight gap risk", "Skew and term structure"])

# --- Levels (Phase 10) -------------------------------------------------------

with tab_levels:
    from analytics import indicators, level_respect, oscillator_study, technical_study
    from app.components.charts import levels_chart

    stats_all = level_respect.load_stats()
    params = level_respect.Params.from_config()
    if stats_all.empty:
        st.info("No level study cached yet. Run  python pipeline/run.py --data-only  "
                "(about 2.5 minutes for the whole registry).")
    else:
        headline = stats_all[(stats_all["horizon"] == params.headline_horizon)
                             & (stats_all["slope_regime"] == "all")]
        ok = headline[headline["status"] == "ok"]
        strong_n = int((ok["edge_ci_lo"] > params.strong_min_edge_ci_lo).sum())
        expected = 0.025 * len(ok)
        st.caption(
            "Each moving average is treated as an event study: every time price came "
            "down to it from above, did it hold, how deep did it pierce first, and did "
            "it hold more often than the same test at randomly shifted placebo levels "
            "over the same bars? Levels are ranked by the lower end of that edge's 95% "
            "confidence interval, not by the raw hold rate.")
        st.warning(
            f"Universe check: {strong_n} of {len(ok):,} testable symbol/level pairs clear "
            f"the 'strong' bar at the {params.headline_horizon}-session horizon. About "
            f"{expected:.0f} would clear it by chance alone (one-sided 2.5% per test), and "
            f"the median edge over placebo is {ok['edge_vs_placebo'].median() * 100:+.1f} pts. "
            f"Treat any single 'strong' level as a hypothesis, not a floor.",
            icon=":material/science:")

        symbols = load_universe(scope="all")
        default = symbols.index("SPY") if "SPY" in symbols else 0
        top = st.columns([2, 1, 1])
        symbol = top[0].selectbox("Symbol", symbols, index=default, key="levels_symbol")
        horizon = top[1].selectbox("Horizon (sessions)", list(params.horizons),
                                   index=list(params.horizons).index(params.headline_horizon))
        regime = top[2].selectbox("Level slope at test", ["all", "rising", "falling"])

        frame = indicators.for_symbol(symbol)
        latest = technical_study.load_latest(symbol)
        if frame.empty:
            st.caption("No daily bars for this symbol.")
        else:
            state = latest["trend_state"].iloc[0] if not latest.empty else None
            m = st.columns(5)
            m[0].metric("Close", f"{frame['close'].iloc[-1]:,.2f}")
            m[1].metric("Trend state", state or "--")
            weekly_rsi = frame["w_rsi"].iloc[-1] if "w_rsi" in frame else float("nan")
            m[2].metric("RSI daily / weekly", f"{frame['rsi'].iloc[-1]:.0f} / {weekly_rsi:.0f}")
            m[3].metric("ADX", f"{frame['adx'].iloc[-1]:.0f}")
            m[4].metric("ATR", f"{frame['atr'].iloc[-1]:,.2f}",
                        delta=f"{frame['atr_pct'].iloc[-1]:.1%} of price", delta_color="off")

            st.markdown("**Support map** -- studied levels below the last close, nearest first")
            smap = level_respect.support_map(symbol, frame=frame,
                                             stats=stats_all[stats_all["symbol"] == symbol],
                                             p=params)
            if smap.empty:
                st.caption("No studied level sits below the last close.")
            else:
                cols = [c for c in ["level_id", "level", "distance_pct", "distance_atr",
                                    "distance_em", "slope_now", "slope_regime", "n",
                                    "hold_rate", "ci_lo", "ci_hi", "placebo_rate",
                                    "edge_vs_placebo", "edge_ci_lo", "median_pierce_atr",
                                    "strong", "status"] if c in smap.columns]
                em_days = load_config().get("levels", {}).get("em_dte_days", 30)
                st.dataframe(smap[cols], hide_index=True, width="stretch", column_config={
                    "distance_pct": st.column_config.NumberColumn("Below close", format="percent"),
                    "distance_atr": st.column_config.NumberColumn("ATRs", format="%.1f"),
                    "distance_em": st.column_config.NumberColumn(
                        "EMs", format="%.2f",
                        help=f"expected moves over {em_days} days at the TastyTrade IV index"),
                    "hold_rate": st.column_config.NumberColumn("Held", format="percent"),
                    "ci_lo": st.column_config.NumberColumn("CI lo", format="percent"),
                    "ci_hi": st.column_config.NumberColumn("CI hi", format="percent"),
                    "placebo_rate": st.column_config.NumberColumn("Placebo", format="percent"),
                    "edge_vs_placebo": st.column_config.NumberColumn("Edge", format="percent"),
                    "edge_ci_lo": st.column_config.NumberColumn("Edge CI lo", format="percent"),
                    "median_pierce_atr": st.column_config.NumberColumn(
                        "Median pierce (ATR)", format="%.2f"),
                })

            table = stats_all[(stats_all["symbol"] == symbol) & (stats_all["horizon"] == horizon)
                              & (stats_all["slope_regime"] == regime)].copy()
            table = table.sort_values("edge_ci_lo", ascending=False)
            if not table.empty:
                table["summary"] = table.apply(level_respect.describe, axis=1)
                st.markdown(f"**Every level, {horizon}-session horizon, slope: {regime}**")
                st.dataframe(table[["summary", "n", "hold_rate", "bounce_rate", "placebo_n",
                                    "edge_ci_lo", "median_pierce_atr", "p80_pierce_atr",
                                    "recency_hold_rate", "last_test_date", "level_now"]],
                             hide_index=True, width="stretch", column_config={
                                 "summary": st.column_config.TextColumn("Level", width="large"),
                                 "hold_rate": st.column_config.NumberColumn("Held", format="percent"),
                                 "bounce_rate": st.column_config.NumberColumn("Bounced", format="percent"),
                                 "edge_ci_lo": st.column_config.NumberColumn("Edge CI lo", format="percent"),
                                 "recency_hold_rate": st.column_config.NumberColumn(
                                     "Held (recency-wtd)", format="percent"),
                             })

                columns_by_id = {l: c for l, _, c in level_respect.level_columns(params)}
                chosen = st.selectbox("Chart the tests of", table["level_id"].tolist())
                if chosen:
                    column = columns_by_id[chosen]
                    tests = level_respect.test_events(frame, column, horizon, params)
                    others = [(l, columns_by_id[l]) for l in ("200D SMA", "200W EMA")
                              if l != chosen and l in columns_by_id]
                    st.plotly_chart(levels_chart(frame, [(chosen, column)] + others, tests,
                                                 chosen, years=3), width="stretch")
                    st.caption("Markers sit at the level on each test day: circle = held for "
                               "the horizon, x = broke (a close below the level minus 1 ATR). "
                               "Hover for the pierce depth in ATR units.")

            osc = oscillator_study.load_stats(symbol)
            if not osc.empty:
                st.markdown("**RSI extremes** -- forward returns from the first day of each "
                            "episode vs any day")
                view = osc[osc["horizon"] == horizon][[
                    "timeframe", "condition", "n", "median", "win_rate", "p10", "p90",
                    "base_median", "base_win_rate", "median_minus_base", "last_episode",
                    "in_zone_now"]]
                st.dataframe(view, hide_index=True, width="stretch", column_config={
                    c: st.column_config.NumberColumn(c.replace("_", " "), format="percent")
                    for c in ("median", "win_rate", "p10", "p90", "base_median",
                              "base_win_rate", "median_minus_base")})

# --- Gaps ------------------------------------------------------------------

with tab_gaps:
    st.caption(
        "A short put is rarely destroyed by drift — it is destroyed by a gap. The "
        "stock closes at 52, an announcement lands at 06:40, it opens at 44, and "
        "there was no moment in between at which the position could be defended. "
        "Daily bars record that as one bad day and imply, wrongly, that it was a "
        "path you could react to. Splitting each session into its overnight and "
        "intraday halves is what the 1-minute archive is uniquely for.")

    path = output_dir() / "gap_profiles.csv"
    controls = st.columns([1, 1, 3])
    if controls[0].button("Profile universe", type="primary"):
        from core.progress import StreamlitReporter
        box = st.container()
        reporter = StreamlitReporter(box, [("gaps", "Overnight gap profiles")])
        with st.spinner("Reading 1-minute history..."):
            frame = gaps.universe_profiles(universe, reporter=reporter)
        if frame.empty:
            st.error("No usable 1-minute data. Check data/raw_1m/ and the cache.")
        else:
            frame.to_csv(path, index=False)
            st.success(f"Profiled {len(frame)} tickers.")

    single = controls[1].selectbox("Or one ticker", ["--"] + universe)
    if single != "--":
        with st.spinner(f"Reading {single}..."):
            result = gaps.profile(single)
        if result is None:
            st.warning(f"Not enough 1-minute history for {single}.")
        else:
            cols = st.columns(5)
            cols[0].metric("Overnight vol", f"{result.overnight_vol:.1%}")
            cols[1].metric("Intraday vol", f"{result.intraday_vol:.1%}")
            cols[2].metric("Overnight share of variance",
                            f"{result.overnight_variance_share:.0%}")
            cols[3].metric("Tail ratio", f"{result.tail_ratio:.1f}x",
                            help="Overnight 1st-percentile move divided by the "
                                 "intraday one. Above 1 means the undefendable "
                                 "tail is the bigger one.")
            cols[4].metric("Worst gap", f"{result.worst_gap:.1%}",
                            delta=result.worst_gap_date, delta_color="off")
            st.info(result.note, icon=":material/nightlight:")
            probs = st.columns(3)
            probs[0].metric("P(gap ≤ −2%)", f"{result.prob_gap_below_2pct:.2%}")
            probs[1].metric("P(gap ≤ −5%)", f"{result.prob_gap_below_5pct:.2%}")
            probs[2].metric("P(gap ≤ −10%)", f"{result.prob_gap_below_10pct:.3%}")

    if path.exists():
        frame = pd.read_csv(path)
        threshold = cfg.get("gap_variance_share_warn", 0.60)
        risky = frame[frame["overnight_variance_share"] >= threshold]
        if not risky.empty:
            st.warning(
                f"{len(risky)} name(s) carry {threshold:.0%}+ of their daily variance "
                f"overnight. On these, rolling and stop discipline can only act on "
                f"the smaller half of the risk — size them as if the roll engine "
                f"did not exist.", icon=":material/warning:")
        st.dataframe(
            frame[["ticker", "overnight_vol", "intraday_vol",
                   "overnight_variance_share", "tail_ratio", "gap_kurtosis",
                   "prob_gap_below_5pct", "worst_gap", "worst_gap_date",
                   "undefendable_share"]],
            hide_index=True, width="stretch")

# --- Skew ------------------------------------------------------------------

with tab_skew:
    st.caption(
        "The 25-delta put IV minus the 25-delta call IV — what the market charges "
        "for downside relative to upside. Rich skew means you are well paid for the "
        "same delta; inverted means you are selling the cheap tail. Unlike IV rank, "
        "this is a cross-sectional relationship within one expiration, so it needs "
        "no accumulated history and works from the first snapshot.")

    if st.button("Measure universe", type="primary", key="skew_run"):
        from core.progress import StreamlitReporter
        box = st.container()
        reporter = StreamlitReporter(box, [("skew", "Skew and term structure")])
        with st.spinner("Reading chain snapshots..."):
            frame = skew.universe_skew(universe, reporter=reporter)
        if frame.empty:
            st.error("No snapshots with IV. Run the pipeline during market hours.")
        else:
            frame.to_csv(output_dir() / "skew.csv", index=False)
            st.session_state["skew_frame"] = frame

    frame = st.session_state.get("skew_frame")
    if frame is None and (output_dir() / "skew.csv").exists():
        frame = pd.read_csv(output_dir() / "skew.csv")

    if frame is not None and not frame.empty:
        if "classification" in frame.columns:
            counts = frame["classification"].value_counts()
            cols = st.columns(4)
            for i, label in enumerate(["rich", "normal", "flat", "inverted"]):
                cols[i].metric(label.title(), int(counts.get(label, 0)))

            inverted = frame[frame["classification"] == "inverted"]
            if not inverted.empty:
                st.error(
                    f"{len(inverted)} name(s) show inverted skew — calls priced above "
                    f"puts. These are blocked at the entry gate: "
                    f"{', '.join(inverted['ticker'].head(8))}",
                    icon=":material/swap_vert:")

        if "ts_event_suspected" in frame.columns:
            events = frame[frame["ts_event_suspected"] == True]  # noqa: E712
            if not events.empty:
                st.warning(
                    f"{len(events)} name(s) show single-name backwardation — near-dated "
                    f"IV well above further-dated. Something dateable is expected. If "
                    f"the earnings calendar shows nothing for these, it is the only "
                    f"warning you get: {', '.join(events['ticker'].head(8))}",
                    icon=":material/event_busy:")

        # Lead with how many names the quotes could actually resolve. Without
        # this the table reads as 61 measurements when most of it is spread.
        if "classification" in frame.columns:
            unmeasurable = int((frame["classification"] == "unmeasurable").sum())
            if unmeasurable:
                st.info(
                    f"{len(frame) - unmeasurable} of {len(frame)} names have quotes "
                    f"tight enough to fix the sign of skew. The other {unmeasurable} "
                    f"carry a bid/ask band wide enough to span more than one "
                    f"classification — they are warned on, never blocked. Re-run "
                    f"during regular trading hours for a tighter read.",
                    icon=":material/straighten:")

        show = [c for c in ["ticker", "spot", "forward", "put_iv", "call_iv", "atm_iv",
                             "normalised_skew", "skew_low", "skew_high",
                             "classification", "measurable", "skew_score",
                             "ts_ratio", "ts_state", "ts_event_suspected"]
                if c in frame.columns]
        st.dataframe(frame[show], hide_index=True, width="stretch")
