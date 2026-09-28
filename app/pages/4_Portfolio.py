"""
Portfolio -- the book as one thing rather than a list of trades.

Phase 5 measured the risk that matters here: AAPL's worst assigned cycle in the
2008 regime ran 462 trading days. One position immobilised for two years is a
nuisance. Twenty-five correlated positions immobilised together is the wheel
stopping — there is no cash left to sell puts with, so the strategy does not
recover, it just waits.

Views, in the order they change a decision:

  0. Open book (Phase 15) — every open position, puts and spreads alike:
     mark, P&L, beta-weighted delta, theta per day, vega, buying power
     against the account, and the events that fall inside each trade
  1. Exposure — where the collateral actually is, by correlation cluster
  2. Stress — how many positions would have assigned in the same week
  3. Correlation — what moves with what
  4. Recycling — when capital actually comes back
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from analytics import book as open_book  # noqa: E402
from analytics import paper, portfolio  # noqa: E402
from core.paths import load_config, load_universe  # noqa: E402

st.set_page_config(page_title="Portfolio", layout="wide")
st.title("Portfolio")

cfg = load_config()
nlv = float(cfg.get("account", {}).get("net_liquidating_value", 0.0))
limits = cfg.get("portfolio", {})

from app.components.run_state import active_run, analyse_stage, run_caption  # noqa: E402

manifest, _, source = active_run()
analyse = analyse_stage(manifest)
run_caption(manifest, source)
candidates = analyse.get("candidates", [])
held_back = analyse.get("portfolio_rejected", [])


def current_book() -> list[dict]:
    """Open positions plus anything the latest run proposed."""
    book = []
    positions = paper.list_positions(status="open")
    for _, row in positions.iterrows():
        book.append({"ticker": str(row["ticker"]).upper(),
                      "strike": float(row["strike"]),
                      "spot": None,
                      "collateral": float(row.get("collateral") or 0.0),
                      "source": "open"})
    for rec in candidates:
        book.append({"ticker": str(rec["ticker"]).upper(),
                      "strike": float(rec["strike"]), "spot": rec.get("spot"),
                      "collateral": float(rec.get("collateral") or 0.0),
                      "source": "proposed"})
    return book


book = current_book()

# --- 0. Open book ------------------------------------------------------------

st.subheader("Open book")


@st.cache_data(ttl=300, show_spinner="Marking the open book...")
def _open_book():
    frame = open_book.open_book()
    return frame, open_book.event_calendar(frame)


held, calendar = _open_book()
if held.empty:
    st.caption("No open positions in the paper book. Accept a trade on Trade Detail or "
               "Decisions and it appears here with its marks and Greeks.")
else:
    totals = open_book.summary(held, nlv or None)
    cols = st.columns(6)
    cols[0].metric("Positions", totals["positions"], delta=f"{totals['names']} names",
                   delta_color="off")
    cols[1].metric("Buying power held", f"${totals['bpr']:,.0f}",
                   delta=f"{totals['utilisation']:.1%} of net liq"
                   if totals.get("utilisation") is not None else None, delta_color="off")
    cols[2].metric("Beta-weighted delta",
                   f"{totals['bw_delta']:+,.0f} SPY sh" if totals.get("bw_delta") is not None
                   else "--",
                   help="Position deltas in SPY-share equivalents: delta x beta x spot / "
                        "SPY. Positive = the book gains when the market rises.")
    cols[3].metric("Theta / day", f"${totals['theta_day']:+,.0f}"
                   if totals.get("theta_day") is not None else "--",
                   help="Dollars of time decay per calendar day at today's prices.")
    cols[4].metric("Vega", f"${totals['vega']:+,.0f}" if totals.get("vega") is not None
                   else "--", help="Dollars per 1 vol point. Negative = short volatility.")
    cols[5].metric("Unrealised", f"${totals['unrealized']:+,.0f}"
                   if totals.get("unrealized") is not None else "--",
                   help="Credit minus the current mark, before exit fees.")
    if totals.get("utilisation") is not None:
        st.progress(min(totals["utilisation"], 1.0),
                    text=f"Buying power in use: {totals['utilisation']:.1%} of "
                         f"${totals['nlv']:,.0f}"
                         + "".join(f" · {k.upper()} ${v:,.0f}"
                                   for k, v in totals["by_strategy"].items()))
    if totals.get("unpriced"):
        st.warning(f"{totals['unpriced']} position(s) have no mark in the stored chains; "
                   f"their P&L is blank and their Greeks are modelled. Run the pipeline to "
                   f"refresh chains.", icon=":material/warning:")
    show = ["id", "ticker", "legs", "expiration", "dte", "contracts", "credit", "mark",
            "profit_pct", "unrealized", "spot", "bpr", "max_loss", "beta", "delta_shares",
            "bw_delta", "theta_day", "vega", "greeks_source"]
    st.dataframe(
        held[[c for c in show if c in held.columns]], hide_index=True, width="stretch",
        column_config={
            "credit": st.column_config.NumberColumn("Credit", format="$%.2f"),
            "mark": st.column_config.NumberColumn("Mark", format="$%.2f"),
            "profit_pct": st.column_config.ProgressColumn("Of max profit", min_value=-1.0,
                                                          max_value=1.0, format="percent"),
            "unrealized": st.column_config.NumberColumn("Unrealised", format="$%.0f"),
            "bpr": st.column_config.NumberColumn("BPR", format="$%.0f"),
            "max_loss": st.column_config.NumberColumn("Max loss", format="$%.0f"),
            "beta": st.column_config.NumberColumn("Beta", format="%.2f"),
            "delta_shares": st.column_config.NumberColumn("Delta (sh)", format="%.0f"),
            "bw_delta": st.column_config.NumberColumn("BW delta (SPY sh)", format="%.0f"),
            "theta_day": st.column_config.NumberColumn("Theta/day", format="$%.2f"),
            "vega": st.column_config.NumberColumn("Vega", format="$%.0f"),
        })
    st.caption("Greeks from the latest chain snapshot where the leg is quoted, else "
               "Black-Scholes at the leg's entry IV. Beta: 1-year daily regression on SPY.")

    st.markdown("**Event calendar**")
    if calendar.empty:
        st.caption("No stored events fall inside an open position.")
    else:
        st.dataframe(calendar, hide_index=True, width="stretch",
                     column_config={"bpr_exposed": st.column_config.NumberColumn(
                         "BPR exposed", format="$%.0f")})
        st.caption("Each event with the positions whose life it falls inside. Market-wide "
                   "events (FOMC, CPI, OPEX...) hit every position still open on the day.")

st.divider()

# --- 1. Exposure -----------------------------------------------------------

st.subheader("Exposure")

if not book:
    st.info("No open positions and no proposals yet. Run the pipeline from the "
            "Command Center — concentration limits are applied during selection, "
            "not audited afterwards, so this page fills in once there is a book.",
            icon=":material/info:")
else:
    committed = sum(b["collateral"] for b in book)
    cols = st.columns(4)
    cols[0].metric("Positions", len(book))
    cols[1].metric("Collateral committed", f"${committed:,.0f}")
    cols[2].metric("Utilisation", f"{committed / nlv:.0%}" if nlv else "--")
    cols[3].metric("Names", len({b["ticker"] for b in book}))

    with st.spinner("Clustering by correlation..."):
        exposure = portfolio.exposure_summary(book)

    if not exposure.empty:
        st.caption(
            f"Grouped by correlation cluster rather than sector label — 27 of 61 "
            f"universe names carry no sector tag, and correlation groups what "
            f"actually moves together. Limit: "
            f"{limits.get('max_positions_per_cluster', 2)} positions and "
            f"{limits.get('max_cluster_collateral_pct', 0.25):.0%} of the account "
            f"per cluster.")
        show = exposure.copy()
        if "pct_of_account" in show.columns:
            over = show[show["pct_of_account"] >
                        limits.get("max_cluster_collateral_pct", 0.25)]
            if not over.empty:
                for _, row in over.iterrows():
                    st.warning(
                        f"Cluster {row['cluster']} ({row['tickers']}) holds "
                        f"{row['pct_of_account']:.0%} of the account — over the "
                        f"{limits.get('max_cluster_collateral_pct', 0.25):.0%} limit.",
                        icon=":material/warning:")
        st.dataframe(show, hide_index=True, width="stretch")

st.divider()

# --- 2. Stress -------------------------------------------------------------

st.subheader("Simultaneous assignment")
st.caption(
    "Replays every historical window and counts how many of these positions would "
    "have finished in the money at the same time. This is the scenario that stops a "
    "cash-secured wheel: not one bad trade, but the whole book converting to stock "
    "in one week.")

stress_stored = analyse.get("stress")
priced = [b for b in book if b.get("spot")]

if stress_stored:
    stress = stress_stored
elif priced and st.button("Run stress test", type="primary"):
    with st.spinner("Replaying history..."):
        result = portfolio.simultaneous_assignment(
            priced, horizon=int(limits.get("stress_horizon_days", 7)),
            years=int(limits.get("stress_years", 20)))
    stress = result.to_dict() if result else None
    st.session_state["stress"] = stress
else:
    stress = st.session_state.get("stress")

if stress:
    cols = st.columns(5)
    cols[0].metric("Positions", stress["positions"])
    cols[1].metric("Typical", f"{stress['p50_assigned']}")
    cols[2].metric("Worst 10%", f"{stress['p90_assigned']}")
    cols[3].metric("Worst 1%", f"{stress['p99_assigned']}")
    cols[4].metric("Worst ever", f"{stress['worst_assigned']}",
                    delta=stress["worst_date"], delta_color="off")

    converted = stress["worst_pct_converted"]
    if stress["all_assigned_ever"] or converted > 0.5:
        st.error(stress["verdict"], icon=":material/error:")
    elif converted > 0.3:
        st.warning(stress["verdict"], icon=":material/warning:")
    else:
        st.success(stress["verdict"], icon=":material/check_circle:")

    st.caption(
        f"Worst window converts ${stress['worst_capital_converted']:,.0f} of "
        f"${stress['total_collateral']:,.0f} collateral, across "
        f"{stress['windows_tested']:,} historical {stress['horizon_days']}-day "
        f"windows. Keep a cash buffer sized to the worst-1% figure, not the median.")
elif not priced:
    st.caption("Needs positions with a current spot price — run the pipeline first.")

st.divider()

# --- 3. Held back ----------------------------------------------------------

if held_back:
    st.subheader("Held back on concentration")
    st.caption(
        "These passed every entry gate and were blocked by portfolio limits rather "
        "than by anything wrong with the trade. Shown rather than hidden — you may "
        "still want one, and the tool should not pretend it was never a candidate.")
    for record in held_back[:8]:
        with st.container(border=True):
            head = st.columns([2, 1, 1, 1])
            head[0].markdown(f"**{record.get('ticker')}** "
                              f"{record.get('expiration', '')} "
                              f"${record.get('strike', 0):g} put")
            if record.get("ev_annualised") is not None:
                head[1].metric("EV", f"{record['ev_annualised']:.1%}")
            if record.get("diversification_benefit") is not None:
                head[2].metric("Diversification",
                                f"{record['diversification_benefit']:.0%}",
                                help="How much less than its standalone risk this "
                                     "position would add to the book. Low means it "
                                     "duplicates something you already hold.")
            if record.get("max_correlation") is not None:
                head[3].metric("Max corr", f"{record['max_correlation']:.2f}",
                                delta=record.get("most_correlated_with"),
                                delta_color="off")
            for reason in record.get("rejection_reasons", []):
                st.caption(f"· {reason}")
    st.divider()

# --- 4. Correlation --------------------------------------------------------

st.subheader("Correlation")

scope = st.radio("Scope", ["Current book", "Whole universe"], horizontal=True,
                  label_visibility="collapsed")
tickers = (sorted({b["ticker"] for b in book}) if scope == "Current book"
           else load_universe())

if len(tickers) < 2:
    st.caption("Needs at least two names.")
elif st.button(f"Compute correlation for {len(tickers)} names"):
    with st.spinner("Loading returns..."):
        corr = portfolio.correlation_matrix(
            tickers, years=int(limits.get("correlation_years", 3)))
    if corr.empty:
        st.warning("No usable return history. Run the pipeline to populate daily bars.")
    else:
        st.session_state["corr"] = corr

corr = st.session_state.get("corr")
if corr is not None and not corr.empty:
    threshold = limits.get("max_pairwise_correlation", 0.80)
    pairs = []
    names = list(corr.columns)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            value = corr.loc[a, b]
            if pd.notna(value) and value >= threshold:
                pairs.append({"a": a, "b": b, "correlation": float(value)})
    if pairs:
        st.warning(
            f"{len(pairs)} pair(s) above the {threshold:.2f} limit — holding both "
            f"sides of any of these is one position wearing two names.",
            icon=":material/link:")
        st.dataframe(pd.DataFrame(pairs).sort_values("correlation", ascending=False),
                      hide_index=True, width="stretch")
    st.dataframe(corr.round(2), width="stretch")

st.divider()

# --- 5. Recycling ----------------------------------------------------------

st.subheader("Capital recycling")
st.caption(
    "Short puts release their collateral at expiry. Assigned shares release nothing "
    "on a schedule — Phase 5 measured recovery at a median of 6 sessions but a p90 "
    "of 43 and a worst case in the hundreds. Sizing new trades against capital that "
    "is parked in stock is how a book runs out of cash while looking fully funded.")

schedule = portfolio.recycling_schedule()
if schedule.empty:
    st.caption("Nothing committed yet.")
else:
    certain = schedule[schedule["certain"]]
    locked = schedule[~schedule["certain"]]
    cols = st.columns(3)
    cols[0].metric("Scheduled to free", f"${certain['capital'].sum():,.0f}")
    cols[1].metric("Locked in shares", f"${locked['capital'].sum():,.0f}")
    cols[2].metric("Next release",
                    str(certain["frees_on"].min()) if not certain.empty else "--")
    if not locked.empty:
        st.warning(
            f"${locked['capital'].sum():,.0f} across {len(locked)} share lot(s) has no "
            f"scheduled release date. Treat it as unavailable when sizing new "
            f"positions, not as idle cash.", icon=":material/lock:")
    st.dataframe(schedule, hide_index=True, width="stretch")
