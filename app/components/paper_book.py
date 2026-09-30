"""
The paper book's own sections (moved from Decisions to Tracking, Tom
2026-09-28/29): book performance and the full ledger, the outcome / roll /
mark forms, share lots from assignment, and "is the model telling the
truth?" (fill quality and probability calibration). Tracking renders them;
Decisions keeps the proposals and the accept/override flow.
"""
from __future__ import annotations

import datetime as dt

import streamlit as st

from analytics import paper


def book_section() -> None:
    """Performance, the ledger, and the forms that change it."""
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
            if stats.get("n_scored"):
                metrics[2].metric("Win rate", f"{stats['win_rate']:.0%}",
                                  help="Symbol Lookup trades are left out of the win rate.")
            excluded = stats.get("n_dollar_excluded", 0)
            note = (f"Leaves out {excluded} row(s) sized against the research default or a "
                    f"placeholder profile (their rates still count)." if excluded else None)
            metrics[3].metric("Realised", f"${stats['total_realized']:,.0f}", help=note)
            annualised = stats["mean_annualised"]
            metrics[4].metric("Mean annualised",
                              f"{annualised:.1%}" if annualised == annualised else "n/a", help=note)

        if stats.get("by_strategy"):
            st.caption(" · ".join(
                f"{name.upper()}: {v['n_closed']} closed, {v['profit_rate']:.0%} profitable, "
                f"${v['total_realized']:,.0f}" for name, v in stats["by_strategy"].items()))
        lookup = stats.get("tracked_lookup") or {}
        if lookup.get("n_closed"):
            annualised = lookup["mean_annualised"]
            st.caption(f"Symbol Lookup, tracked: {lookup['n_closed']} closed, "
                       f"${lookup['total_realized']:,.0f}"
                       + (f", {annualised:.1%} mean annualised" if annualised == annualised
                          else "")
                       + ". Not in the totals above; taken lookup trades are.")

        shown = positions.copy()
        shown["legs"] = [paper.leg_text(r) for _, r in shown.iterrows()]
        show = ["id", "ticker", "strategy", "legs", "expiration", "contracts", "modelled_fill",
                "actual_fill", "slippage", "status", "collateral", "rec_prob_otm",
                "max_profit_pct_seen", "rolls_used"]
        st.dataframe(shown[[c for c in show if c in shown.columns]], hide_index=True,
                     width="stretch",
                     column_config={"collateral": st.column_config.NumberColumn("BPR",
                                                                                format="$%.0f"),
                                    "max_profit_pct_seen": st.column_config.NumberColumn(
                                        "Best % of max seen", format="percent")})

        def _label(i: int) -> str:
            r = open_rows.loc[open_rows["id"] == i].iloc[0]
            return f"#{i} {r['ticker']} {r['expiration']} {paper.leg_text(r)}"

        if not open_rows.empty:
            with st.expander("Mark an outcome"):
                with st.form("close_position"):
                    cols = st.columns([2, 1, 1, 1, 1])
                    pid = cols[0].selectbox("Position", open_rows["id"].tolist(),
                                            format_func=_label)
                    status = cols[1].selectbox(
                        "Outcome", ["expired_otm", "closed_early", "assigned", "settled"],
                        help="A CSP that finishes in the money is 'assigned'; a spread is "
                             "'settled' at the underlying's settlement price.")
                    price = cols[2].number_input("Paid to close (net)", min_value=0.0,
                                                 value=0.0, step=0.01, format="%.2f",
                                                 help="0 for expiry or assignment.")
                    settle = cols[3].number_input("Settlement price", min_value=0.0, value=0.0,
                                                  step=0.01, format="%.2f",
                                                  help="Spreads settled in the money only.")
                    when = cols[4].date_input("Date", value=dt.date.today())
                    if st.form_submit_button("Record outcome"):
                        try:
                            paper.close_position(int(pid), status, when, price,
                                                 settlement_price=settle or None)
                            st.success(f"Position #{pid} marked {status}.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"{type(exc).__name__}: {exc}")

            with st.expander("Roll a position"):
                st.caption("Recorded as two positions: the old one `rolled` at the debit you "
                           "paid, the new one linked to it. Rolls are meant to be for a net "
                           "credit; a debit roll is recorded and flagged.")
                with st.form("roll_position"):
                    cols = st.columns([2, 1, 1, 1, 1, 1])
                    rid = cols[0].selectbox("Position", open_rows["id"].tolist(),
                                            format_func=_label, key="roll_pid")
                    debit = cols[1].number_input("Paid to close", 0.0, step=0.01, format="%.2f")
                    new_exp = cols[2].date_input("New expiration",
                                                 value=dt.date.today() + dt.timedelta(days=28))
                    new_short = cols[3].number_input("New short strike", 0.0, step=0.5)
                    new_long = cols[4].number_input("New long strike (spread)", 0.0, step=0.5,
                                                    help="Blank (0) keeps the width.")
                    new_credit = cols[5].number_input("New credit", 0.0, step=0.01, format="%.2f")
                    if st.form_submit_button("Record roll"):
                        try:
                            result = paper.roll_position(int(rid), debit, new_exp, new_short,
                                                         new_credit, new_long or None,
                                                         roll_date=dt.date.today())
                            (st.success if result.net_per_share > 0 else st.warning)(
                                result.message)
                        except Exception as exc:
                            st.error(f"{type(exc).__name__}: {exc}")

            with st.expander("Record a mark"):
                st.caption("The pipeline records a mark for every open position on each run. "
                           "Add your own when you see the position trade -- the best mark seen "
                           "decides whether a P(reach X%) prediction came true.")
                with st.form("record_mark"):
                    cols = st.columns([2, 1, 1, 1])
                    mid = cols[0].selectbox("Position", open_rows["id"].tolist(),
                                            format_func=_label, key="mark_pid")
                    mark = cols[1].number_input("Mark (net debit to close)", 0.0, step=0.01,
                                                format="%.2f")
                    spot_in = cols[2].number_input("Spot", 0.0, step=0.01, format="%.2f")
                    day = cols[3].date_input("Date", value=dt.date.today(), key="mark_day")
                    if st.form_submit_button("Record mark"):
                        try:
                            pct = paper.record_mark(int(mid), mark, spot_in or None, day)
                            st.success(f"Mark recorded: {pct:.0%} of max profit.")
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


def truth_section() -> None:
    """Fill quality and probability calibration."""
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
        if cal.get("n_scored", 0) >= 10:
            st.metric("Predicted vs realised win rate",
                       f"{cal['win_rate']:.0%}",
                       delta=f"{cal['calibration_gap']:+.1%} vs predicted")
        st.caption(cal.get("verdict", ""))
