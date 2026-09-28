"""
Trade Detail -- the deep dive on one candidate (Phase 14, roadmap C.6).

Opened from the Screener with `?run=<run id>&trade=<trade id>`; it loads the
PERSISTED run, so the numbers are exactly what the ranking saw and the URL is
bookmarkable while the run folder exists. Without query parameters it shows
the Screener's last selection, else the top-ranked row of the latest run.

Tabs: Summary · Chart · Expected move · Payoff · Probabilities · Greeks ·
Chain & liquidity · Management plan · Context · Accept.

Everything is built from `analytics.trade_detail` (no Streamlit there) and the
chart builders in app/components/charts.py, which work on a `Position` of any
number of legs. Accept records CSPs and put credit spreads to the multi-leg
paper book (Phase 15). Nothing here places an order.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from analytics import trade_detail as td  # noqa: E402
from app.components import charts  # noqa: E402
from app.components.status import banner  # noqa: E402
from pipeline.results import latest_run, load_run  # noqa: E402

st.set_page_config(page_title="Trade Detail", layout="wide")


@st.cache_data(show_spinner=False, ttl=3600)
def _daily(ticker: str) -> pd.DataFrame:
    from data_sources.yfinance_sync import load_daily
    return load_daily(ticker, basis="price")


@st.cache_data(show_spinner=False, ttl=3600)
def _support(ticker: str) -> pd.DataFrame:
    from analytics import technical_study
    try:
        return technical_study.load_support(ticker)
    except Exception:
        return pd.DataFrame()


@st.cache_data(show_spinner="Sampling terminal distributions...", ttl=3600)
def _distributions(ticker: str, spot: float, iv: float | None, dte_trading: int,
                   dte_calendar: int) -> dict:
    return td.terminal_distributions(_daily(ticker), spot, iv, dte_trading, dte_calendar)


@st.cache_data(show_spinner=False, ttl=3600)
def _containment(ticker: str, horizon: int):
    from analytics import expected_move
    result = expected_move.containment(_daily(ticker), max(horizon, 1))
    return result.to_dict() if result else None


def _load(run_id: str | None):
    if run_id:
        results = load_run(run_id)
        if results is not None:
            return results
    return latest_run()


# --- Which trade -----------------------------------------------------------------

params = st.query_params
selection = st.session_state.get("screener_selection") or {}
run_id = params.get("run") or selection.get("run")
results = _load(run_id)
if results is None or results.candidates.empty:
    st.title("Trade Detail")
    st.info("No run with candidates on disk yet. Run a scan on the Screener page.")
    st.stop()
if run_id and results.run_id != run_id:
    st.warning(f"Run `{run_id}` was not found; showing the latest run `{results.run_id}`.")

trade_id = params.get("trade") or (selection.get("trade")
                                   if selection.get("run") == results.run_id else None)
if not trade_id or td.record(results, trade_id) is None:
    if trade_id:
        st.warning(f"Trade `{trade_id}` is not in run `{results.run_id}`; showing its top row.")
    trade_id = td.default_trade(results)

sheet = results.candidates
options = list(sheet["trade_id"])[:400]
if trade_id not in options:
    options = [trade_id] + options
labels = {r["trade_id"]: td.trade_label({**r, "expiration": str(pd.Timestamp(r["expiration"]).date())})
          for r in sheet[sheet["trade_id"].isin(options)].to_dict("records")}
top = st.columns([4, 1])
chosen = top[0].selectbox("Trade", options, index=options.index(trade_id),
                          format_func=lambda t: labels.get(t, t),
                          help="Every row of this run; the URL follows the choice.")
if chosen != trade_id:
    trade_id = chosen
st.query_params.update({"run": results.run_id, "trade": trade_id})
if top[1].button("← Screener", width="stretch"):
    try:
        st.switch_page("pages/8_Screener.py")
    except Exception:
        st.info("Open the Screener from the sidebar.")

row = td.record(results, trade_id)
position = td.position(row)
spot = float(row["spot"])
dte = td.days_to_expiry(row)
dte_trading = max(int(row.get("dte_trading") or 1), 1)
contracts = max(int(row.get("contracts") or 0), 1)
is_pcs = row.get("strategy") == "pcs"
strikes = [(f"Short ${row['strike']:g}", float(row["strike"]), "short")]
if is_pcs:
    strikes.append((f"Long ${row['long_strike']:g}", float(row["long_strike"]), "long"))

st.title(td.escape_md(td.trade_label(row)))
st.caption(f"Run `{results.run_id}` · {results.label} · trade id `{trade_id}` · "
           f"{'sized ' + str(int(row.get('contracts') or 0)) + ' contract(s)' if row.get('contracts') else 'not sized (rejected): figures per 1 contract'}")

head = st.columns(7)
head[0].metric("Credit", f"${row.get('modelled_fill') or 0:.2f}",
               delta=f"mid ${row.get('net_mid') or row.get('mid') or 0:.2f}", delta_color="off")
head[1].metric("Max loss", f"${(row.get('max_loss') or position.max_loss * 100 * contracts):,.0f}")
head[2].metric("BPR", f"${row.get('collateral') or 0:,.0f}")
head[3].metric("POP (blend)", f"{row['pop_blend']:.0%}" if row.get("pop_blend") is not None else "--")
head[4].metric("P(50%)", f"{row['p_hit_50_blend']:.0%}" if row.get("p_hit_50_blend") is not None else "--")
head[5].metric("Headline EV", f"${row['headline_ev']:,.0f}" if row.get("headline_ev") is not None else "--",
               delta=row.get("headline_policy"), delta_color="off")
head[6].metric("DTE", f"{dte}", delta=f"{row['expiration']}", delta_color="off")

tabs = st.tabs(["Summary", "Chart", "Expected move", "Payoff", "Probabilities", "Greeks",
                "Chain & liquidity", "Management plan", "Context", "Accept"])

# --- Summary ------------------------------------------------------------------------

with tabs[0]:
    thesis = td.thesis(row)
    banner(thesis["level"], thesis["verdict"], "Verdict")
    st.markdown(td.escape_md(thesis["text"]))
    left, right = st.columns(2)
    with left:
        st.markdown("**Why this strike**")
        for reason in thesis["why_strike"]:
            st.markdown(td.escape_md(f"- {reason}"))
        if row.get("rationale"):
            st.caption(td.escape_md(row["rationale"]))
    with right:
        st.markdown("**Risk flags**")
        if thesis["flags"]:
            for flag in thesis["flags"]:
                st.markdown(td.escape_md(f"- ⚠ {flag}"))
        else:
            st.caption("None raised.")
    for note in row.get("notes") or []:
        st.caption(td.escape_md(note))

# --- Chart ----------------------------------------------------------------------------

with tabs[1]:
    daily = _daily(row["ticker"])
    if daily.empty:
        st.info("No daily bars on disk for this ticker.")
    else:
        from analytics import bars, indicators
        c = st.columns([1, 2, 1])
        timeframe = c[0].radio("Bars", ["Daily", "Weekly"], horizontal=True)
        frame = (indicators.compute(daily) if timeframe == "Daily"
                 else indicators.compute(bars.weekly(daily), periods_per_year=52))
        prefix = "D" if timeframe == "Daily" else "W"
        ma_choices = [(f"{n}{prefix} {kind.upper()}", f"{kind}_{n}")
                      for kind in ("ema", "sma") for n in (21, 50, 100, 200)
                      if f"{kind}_{n}" in frame]
        picked = c[1].multiselect("Moving averages", [m[0] for m in ma_choices],
                                  default=[m[0] for m in ma_choices
                                           if m[1] in ("ema_21", "sma_50", "sma_200")][:3],
                                  max_selections=3)
        lookback = c[2].slider("Bars shown", 60, 520, 180 if timeframe == "Daily" else 104)
        levels = td.nearby_levels(_support(row["ticker"]), spot, [s[1] for s in strikes])
        last = pd.Timestamp(frame["date"].iloc[-1]).date()
        cone = td.em_cone(spot, last, dte, row.get("em_iv"), row.get("em_straddle"))
        start = pd.Timestamp(frame["date"].iloc[-min(lookback, len(frame))])
        events = td.trade_events(row["ticker"], start, pd.Timestamp(row["expiration"]),
                                 market_from=pd.Timestamp(last))
        fig = charts.trade_price_chart(frame, [m for m in ma_choices if m[0] in picked],
                                       strikes, levels, cone, events, lookback)
        st.plotly_chart(fig, width="stretch")
        if not levels.empty:
            st.markdown("**Respected levels near the trade** (Phase 10 level study)")
            st.dataframe(levels[[c_ for c_ in ["level_id", "level", "distance_pct", "n",
                                               "hold_rate", "edge_vs_placebo", "edge_ci_lo",
                                               "strong", "summary"] if c_ in levels]],
                         hide_index=True, width="stretch",
                         column_config={
                             "level": st.column_config.NumberColumn("Level", format="$%.2f"),
                             "distance_pct": st.column_config.NumberColumn("From spot", format="percent"),
                             "hold_rate": st.column_config.NumberColumn("Held", format="percent"),
                             "edge_vs_placebo": st.column_config.NumberColumn("Edge", format="percent"),
                             "edge_ci_lo": st.column_config.NumberColumn("Edge CI low", format="percent"),
                             "summary": st.column_config.TextColumn("Summary", width="large")})
        st.caption("EM cones scale each expected move with √time from the last bar to expiry. "
                   "Strong levels (green) have an edge-vs-placebo CI above zero; Phase 10 found "
                   "most MA support in this universe to be chance.")

# --- Expected move ------------------------------------------------------------------------

with tabs[2]:
    em = row.get("em")
    ems = pd.DataFrame([
        {"method": "tastytrade (0.6 straddle + 0.3/0.1 strangles)", "em": row.get("em_tastytrade")},
        {"method": "IV (spot × ATM IV × √(DTE/365))", "em": row.get("em_iv")},
        {"method": "straddle × 0.85", "em": row.get("em_straddle")}])
    ems["pct_of_spot"] = ems["em"] / spot
    c = st.columns([1, 2])
    with c[0]:
        st.markdown(f"**Expected move to {row['expiration']}** (method used: "
                    f"`{row.get('em_method') or 'n/a'}`)")
        st.dataframe(ems, hide_index=True, width="stretch",
                     column_config={"em": st.column_config.NumberColumn("EM", format="$%.2f"),
                                    "pct_of_spot": st.column_config.NumberColumn("% of spot", format="percent")})
        if row.get("short_distance_em") is not None:
            st.metric("Short strike distance", f"{row['short_distance_em']:+.2f} EM")
        contained = _containment(row["ticker"], dte_trading)
        if contained:
            st.caption(f"Historically, over {contained['horizon_days']} trading days, the "
                       f"move stayed within 1 EM {contained['within_1x']:.0%} and within 2 EM "
                       f"{contained['within_2x']:.0%} of the time ({contained['n_windows']:,} "
                       f"windows; {contained['source']}).")
    with c[1]:
        daily = _daily(row["ticker"])
        dists = _distributions(row["ticker"], spot, row.get("implied_vol"), dte_trading, dte)
        st.plotly_chart(charts.terminal_distribution_chart(dists, strikes, spot, em),
                        width="stretch")
    levels_p = {"short strike": float(row["strike"]),
                "breakeven": float(row.get("breakeven") or td.effective_basis(row)),
                "-1 EM": spot - em if em else None, "-2 EM": spot - 2 * em if em else None}
    if is_pcs:
        levels_p["long strike"] = float(row["long_strike"])
    table = td.distribution_table(dists, levels_p)
    if not table.empty:
        st.dataframe(table, hide_index=True, width="stretch",
                     column_config={
                         "price": st.column_config.NumberColumn("Price", format="$%.2f"),
                         "p_below_empirical": st.column_config.NumberColumn("P(below), empirical", format="percent"),
                         "p_below_lognormal": st.column_config.NumberColumn("P(below), lognormal", format="percent")})
    st.caption(" · ".join(f"{k}: {v}" for k, v in dists.get("label", {}).items())
               + ". The empirical view is model H's sampler (this ticker's own 5-day return "
                 "blocks); the lognormal is what the option's IV implies. Where they differ, "
                 "the market is charging for movement history has or hasn't delivered.")

# --- Payoff ---------------------------------------------------------------------------------

with tabs[3]:
    c = st.columns([1, 3])
    size = c[0].number_input("Contracts", 1, 1000, contracts)
    days = c[0].multiselect("T+n curves (days after entry)",
                            sorted({0, max(dte // 4, 1), max(dte // 2, 1), max(dte - 1, 0)}),
                            default=sorted({0, max(dte // 2, 1)} - {dte}))
    payoff = td.payoff_frame(position, spot, dte, size, row.get("em"), days=days)
    with c[1]:
        st.plotly_chart(charts.payoff_chart(payoff, spot, position.breakevens, strikes),
                        width="stretch")
    k = st.columns(4)
    k[0].metric("Max profit", f"${position.max_profit * 100 * size:,.0f}")
    k[1].metric("Max loss", f"${position.max_loss * 100 * size:,.0f}")
    k[2].metric("Breakeven(s)", ", ".join(f"${b:,.2f}" for b in position.breakevens) or "--")
    k[3].metric("Return on risk", f"{position.max_profit / position.max_loss:.1%}"
                if position.max_loss else "--")
    st.caption("Before fees. T+n curves reprice every leg by Black-Scholes at its own IV "
               "(sticky strike), the engine's assumption.")

# --- Probabilities -------------------------------------------------------------------------

with tabs[4]:
    metrics = results.prob_metrics
    metrics = metrics[metrics["trade_id"] == trade_id] if not metrics.empty else metrics
    if metrics.empty:
        st.info("This run has no probability-engine output (runs before Phase 13).")
    else:
        names = {"pop": "P(profit at expiry)", "p_touch_short": "P(touch short, closes)",
                 "p_short_itm": "P(short ITM at expiry)", "p_max_loss": "P(max loss)",
                 "p_assign": "P(assignment)", "p_roll_trigger": "P(roll trigger)"}
        for t_ in (25, 30, 50, 100):
            names[f"p_hit_{t_}"] = ("P(expire worthless)" if t_ == 100
                                    else f"P(reach {t_}% by expiry)")
            names[f"median_days_{t_}"] = f"median days to {t_}%"
        table = metrics.pivot_table(index="metric", columns="model", values="value",
                                    aggfunc="first")
        table = table[[m for m in ["G", "H", "T", "blend"] if m in table.columns]]
        table = table.loc[[m for m in names if m in table.index]]
        table.index = [names[m] for m in table.index]
        c = st.columns([2, 3])
        with c[0]:
            st.markdown("**Target × model**")
            st.dataframe(table.style.format(lambda v: f"{v:.1f}" if v > 1.0001 else f"{v:.0%}"),
                         width="stretch")
            st.caption(row.get("prob_labels") or "")
        with c[1]:
            curves = results.prob_curves
            curves = curves[curves["trade_id"] == trade_id] if not curves.empty else curves
            if not curves.empty:
                st.plotly_chart(charts.prob_curves_chart(curves), width="stretch")
        pol = results.prob_policies
        pol = pol[pol["trade_id"] == trade_id] if not pol.empty else pol
        if not pol.empty:
            model = st.radio("Policy comparison under model", ["blend", "G", "H", "T"],
                             horizontal=True)
            view = pol[pol["model"] == model].sort_values("ev_per_day_bpr", ascending=False)
            st.dataframe(view[["policy", "ev", "p_profit", "days", "annualised",
                               "ev_per_day_bpr", "net_gain_when_hit", "below_min_gain"]],
                         hide_index=True, width="stretch",
                         column_config={
                             "ev": st.column_config.NumberColumn("EV $", format="$%.0f"),
                             "p_profit": st.column_config.NumberColumn("P(profit)", format="percent"),
                             "days": st.column_config.NumberColumn("Days held", format="%.1f"),
                             "annualised": st.column_config.NumberColumn("Annualised", format="percent"),
                             "ev_per_day_bpr": st.column_config.NumberColumn("EV/day/BPR", format="%.5f"),
                             "net_gain_when_hit": st.column_config.NumberColumn("Net $ if hit", format="$%.0f"),
                             "below_min_gain": st.column_config.CheckboxColumn("Below min gain")})
            st.caption(f"Headline policy: `{row.get('headline_policy')}`. G is the zero-edge "
                       f"baseline (risk-neutral at the short IV); H and T measure the premium "
                       f"the trade harvests. Touch is on daily closes and understates intraday "
                       f"touches.")

# --- Greeks ------------------------------------------------------------------------------------

with tabs[5]:
    now = td.leg_greeks(position, spot, dte)
    g = st.columns(4)
    g[0].metric("Delta (shares)", f"{now['delta'] * contracts:+,.1f}")
    g[1].metric("Theta ($/day)", f"{now['theta'] * contracts:+,.2f}")
    g[2].metric("Gamma", f"{now['gamma'] * contracts:+,.3f}")
    g[3].metric("Vega ($/vol pt)", f"{now['vega'] * contracts:+,.2f}")
    st.plotly_chart(charts.greeks_time_chart(td.greeks_frame(position, spot, dte, contracts)),
                    width="stretch")
    day = (st.slider("Scenario day (days after entry)", 0, dte - 1, 0) if dte > 1 else 0)
    grid = td.scenario_grid(position, spot, dte, day, contracts)
    st.plotly_chart(charts.scenario_heatmap(grid), width="stretch")
    captured = [f"{name} {row[key]:+.3f}" for name, key in
                (("short delta", "delta"), ("net theta", "net_theta"), ("net vega", "net_vega"))
                if row.get(key) is not None]
    st.caption(f"{contracts} contract(s), before fees, Black-Scholes at each leg's IV plus "
               f"the shift." + (f" Chain Greeks at capture: {', '.join(captured)}."
                                if captured else ""))

# --- Chain & liquidity ------------------------------------------------------------------------

with tabs[6]:
    block = ((results.manifest.stages or {}).get("chains", {}) or {}).get("block")
    chain = td.load_chain_for(row, block)
    window = td.chain_window(chain, row["expiration"], [s[1] for s in strikes])
    if window.empty:
        st.info("The chain snapshot for this expiration is no longer on disk.")
    else:
        l = st.columns(4)
        l[0].metric("Fillability", f"{row['fillability']:.2f}" if row.get("fillability") is not None else "--",
                    help="0-1 from spread %, $ width, OI and volume; weakest leg for a spread.")
        l[1].metric("Weakest leg", row.get("weakest_leg") or "short")
        l[2].metric("Modelled fill", f"${row.get('modelled_fill') or 0:.2f}",
                    delta=f"natural ${row.get('natural') or row.get('bid') or 0:.2f}",
                    delta_color="off")
        l[3].metric("OI wall", f"${row['oi_wall_strike']:g}" if row.get("oi_wall_strike") else "none")
        st.plotly_chart(charts.chain_liquidity_chart(window), width="stretch")
        st.dataframe(window, hide_index=True, width="stretch",
                     column_config={
                         "bid": st.column_config.NumberColumn(format="$%.2f"),
                         "ask": st.column_config.NumberColumn(format="$%.2f"),
                         "mid": st.column_config.NumberColumn(format="$%.2f"),
                         "width": st.column_config.NumberColumn("Bid/ask $", format="$%.2f"),
                         "width_pct": st.column_config.NumberColumn("Bid/ask %", format="percent"),
                         "iv": st.column_config.NumberColumn("IV", format="percent"),
                         "delta": st.column_config.NumberColumn(format="%.3f")})
        st.caption(f"Snapshot `{window.attrs.get('block') or block or 'latest'}`. Size: "
                   f"{int(row.get('contracts') or 0)} contract(s), bound by "
                   f"`{row.get('binding_constraint') or 'n/a'}`.")

# --- Management plan -----------------------------------------------------------------------------

with tabs[7]:
    plan = td.management_plan(row)
    st.dataframe(pd.DataFrame(plan), hide_index=True, width="stretch",
                 column_config={"detail": st.column_config.TextColumn("Detail", width="large")})
    if is_pcs:
        st.caption("Once recorded, the pipeline applies these rules to the spread on every "
                   "run (`exit_rules.evaluate_put_spread`) and lists rolls for a net credit "
                   "on the Command Center when the roll trigger fires.")
    else:
        with st.expander("Roll preview (if the trigger fired on today's chain)"):
            try:
                from analytics import roll_engine
                rolls = roll_engine.rank_rolls(row["ticker"], float(row["strike"]),
                                               pd.Timestamp(row["expiration"]).date(), contracts)
                if rolls:
                    st.dataframe(pd.DataFrame([r.to_dict() for r in rolls]),
                                 hide_index=True, width="stretch")
                else:
                    st.caption("No roll candidates in the stored chain.")
            except Exception as exc:
                st.caption(f"Roll preview unavailable: {exc}")
        with st.expander("If assigned: covered-call preview"):
            try:
                from analytics import covered_call
                calls = covered_call.candidates_for_lot(
                    row["ticker"], 100 * contracts, td.effective_basis(row))
                if calls:
                    st.dataframe(pd.DataFrame([c_.to_dict() for c_ in calls]),
                                 hide_index=True, width="stretch")
                else:
                    st.caption("No covered call clears the rules on the stored chain "
                               "(calls at or above the basis, delta and credit limits).")
            except Exception as exc:
                st.caption(f"Covered-call preview unavailable: {exc}")

# --- Context -----------------------------------------------------------------------------------

with tabs[8]:
    c = st.columns(3)
    with c[0]:
        st.markdown("**Volatility and regime**")
        st.markdown(f"- IV rank {row['ivr']:.0%}, IV percentile {row.get('ivp') or 0:.0%}"
                    if row.get("ivr") is not None else "- IV rank: n/a")
        st.markdown(f"- IV / RV {row['iv_rv_ratio']:.2f}" if row.get("iv_rv_ratio") else "- IV / RV: n/a")
        if row.get("skew_class"):
            st.markdown(f"- Skew: {row['skew_class']}")
        try:
            from analytics import regime
            reading = regime.current()
            st.markdown(f"- Market regime: **{reading.state}** — {reading.headline}")
        except Exception:
            pass
    with c[1]:
        st.markdown("**Earnings reactions**")
        try:
            from analytics import earnings_history
            summary = earnings_history.summary(row["ticker"])
        except Exception:
            summary = {"n": 0}
        if summary.get("n"):
            st.markdown(f"- Last {summary['n']} reports: median move "
                        f"{summary['median_abs_move']:.1%}, max {summary['max_abs_move']:.1%}, "
                        f"worst down {summary['worst_down']:.1%}")
            if summary.get("beat_rate") is not None:
                st.markdown(f"- Beat the implied move {summary['beat_rate']:.0%} of "
                            f"{summary['implied_known']} known")
        else:
            st.caption("No earnings history (ETF/index, or none stored).")
        st.markdown("**Gap risk**")
        try:
            from analytics import gaps
            gap = gaps.trade_gap_risk(row["ticker"], spot, float(row["strike"]), dte_trading)
        except Exception:
            gap = None
        if gap:
            st.markdown(f"- P(one overnight gap clears the short strike over the trade) "
                        f"≤ {gap.prob_any_gap_through:.1%}")
            st.caption(gap.verdict)
        else:
            st.caption("No 1-minute history to measure overnight gaps.")
    with c[2]:
        st.markdown("**Fit with the current book**")
        try:
            from analytics import paper, portfolio
            book = paper.list_positions("open")
            held = sorted(set(book["ticker"])) if not book.empty else []
            if held:
                risk = portfolio.marginal_risk(row["ticker"], held)
                corr = portfolio.correlation_matrix(held + [row["ticker"]])
                if row["ticker"] in corr:
                    st.dataframe(corr[[row["ticker"]]].drop(row["ticker"], errors="ignore")
                                 .rename(columns={row["ticker"]: "correlation"}),
                                 width="stretch")
                if risk:
                    st.caption(" · ".join(f"{k}: {v}" for k, v in risk.to_dict().items()
                                          if isinstance(v, str)))
            else:
                st.caption("The paper book is empty, so there is nothing to correlate with.")
        except Exception as exc:
            st.caption(f"Book correlation unavailable: {exc}")

# --- Accept -------------------------------------------------------------------------------------

with tabs[9]:
    if st.button("Send to Decisions"):
        st.session_state["screener_selection"] = {"run": results.run_id, "trade": trade_id}
        try:
            st.switch_page("pages/1_Decisions.py")
        except Exception:
            st.info("Selection saved; open Decisions from the sidebar.")
    if not row.get("accepted"):
        st.warning("This trade failed an entry gate; recording it is still allowed, but the "
                   "reasons are on the Summary tab.")
    from analytics import paper
    from core.paths import load_config

    def _price(value) -> float:
        value = pd.to_numeric(value, errors="coerce")
        return float(value) if pd.notna(value) else 0.0

    allow = (load_config().get("execution", {}) or {}).get("allow_manual_fill_override", True)
    with st.form("accept_detail"):
        f = st.columns(4)
        size = f[0].number_input("Contracts", 1, 1000, contracts)
        fill = f[1].number_input("Actual net credit" if is_pcs else "Actual fill", 0.0,
                                 value=float(row["modelled_fill"]), step=0.01, format="%.2f",
                                 disabled=not allow,
                                 help="What you really got. Calibrates the slippage model.")
        entry = f[2].date_input("Entry date", value=dt.date.today())
        note = f[3].text_input("Note", "")
        leg_fills = None
        if is_pcs:
            g = st.columns(4)
            use_legs = g[0].checkbox("Enter leg fills instead", value=False,
                                     help="If the broker filled the legs separately: the net "
                                          "credit is short minus long.")
            short_fill = g[1].number_input(f"Short \\${row['strike']:g} sold at", 0.0,
                                           value=_price(row.get("bid")), step=0.01,
                                           format="%.2f", disabled=not allow)
            long_fill = g[2].number_input(f"Long \\${row['long_strike']:g} bought at", 0.0,
                                          value=_price(row.get("long_ask")), step=0.01,
                                          format="%.2f", disabled=not allow)
            if use_legs:
                leg_fills = [float(short_fill), float(long_fill)]
        submitted = st.form_submit_button("Accept and record", type="primary")
    if submitted:
        try:
            used_real = abs(fill - float(row["modelled_fill"])) > 1e-9
            result = paper.accept(row, contracts=int(size),
                                  actual_fill=None if leg_fills else
                                  (float(fill) if used_real else None),
                                  leg_fills=leg_fills, entry_date=entry,
                                  run_id=results.run_id, notes=note)
            st.success(result.message)
        except Exception as exc:
            st.error(f"{type(exc).__name__}: {exc}")
    st.caption("Research tool only: nothing here places an order.")
