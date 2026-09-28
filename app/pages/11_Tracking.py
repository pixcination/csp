"""
Tracking (Phase 18) -- the book of logged recommendations and real trades.

    tracked   forward tests logged from the Screener / Strategies pages (or
              "Log all": the top K + control sample of a run); never counted
              against the account
    taken     real trades at your fill ("Promote" records one from a tracked
              row)

Update now pulls chains for the open positions' tickers only and writes a
mark per position: P&L, share of max profit, probabilities recomputed from
now, the management verdict, and the P&L since the last mark split into
delta / gamma / theta / vega / residual. Expire due settles positions whose
expiry has closed and records the hold-to-expiry and managed outcomes.
Everything is `analytics/tracking.py`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from analytics import paper, tracking  # noqa: E402
from app.components.status import session_banner  # noqa: E402

st.title("Tracking")
session_banner()

top = st.columns([1.3, 1, 1, 1, 2])
book_pick = top[0].segmented_control("Book", ["tracked", "taken", "both"], default="both",
                                     key="track_book")
book = None if book_pick in (None, "both") else book_pick

if top[1].button("Update now", type="primary", help="Pull chains for the open positions' "
                 "tickers and record a mark for each (a minute or two)."):
    from core.progress import StreamlitReporter
    box = st.container()
    reporter = StreamlitReporter(box, [("chains", "Chains for open positions"),
                                       ("marks", "Marks and probabilities")])
    with st.spinner("Updating open positions..."):
        new = tracking.update(book=book, reporter=reporter)
    st.success(f"Marked {len(new)} position(s).")
if top[2].button("Expire due", help="Settle positions whose expiry has a completed "
                 "session, from that day's close."):
    done = tracking.expire_due(book=book)
    for item in done:
        st.write(item["message"])
    if not done:
        st.info("Nothing has expired.")
if top[3].button("Archive chains", help="Today's full-universe chain snapshot into "
                 "data/chain_archive (about 10 minutes; meant for 15:45 ET)."):
    from core.progress import StreamlitReporter
    from data_sources import chain_archive
    reporter = StreamlitReporter(st.container(), [("archive", "Chain archive")])
    with st.spinner("Archiving the universe's chains..."):
        manifest = chain_archive.archive(reporter=reporter)
    st.success(f"Archived {len(manifest['tickers'])} tickers, {manifest['rows']:,} rows, "
               f"{manifest['bytes'] / 1e6:.1f} MB.")

positions = paper.list_positions(book=book)
if positions.empty:
    st.info("Nothing logged yet. Log rows from the Screener (log mode) or the Strategies "
            "page, or use Log all on the Screener.")
    st.stop()

open_rows = positions[positions["status"] == "open"].copy()
all_marks = tracking.marks()
latest = (all_marks.sort_values("marked_at").groupby("position_id").tail(1).set_index("position_id")
          if not all_marks.empty else pd.DataFrame())


def _entry(row, key):
    try:
        return json.loads(row["entry_context"]).get(key) if row.get("entry_context") else None
    except ValueError:
        return None


# --- Open positions ------------------------------------------------------------------------

st.subheader(f"Open ({len(open_rows)})")
if not open_rows.empty:
    table = []
    for _, row in open_rows.iterrows():
        mark = latest.loc[row["id"]] if not latest.empty and row["id"] in latest.index else None
        get = (lambda k: None) if mark is None else (lambda k: mark.get(k))
        table.append({
            "id": int(row["id"]), "book": row.get("book"), "sample": row.get("sample"),
            "ticker": row["ticker"], "strategy": row.get("strategy"),
            "legs": paper.leg_text(row), "expiry": row["expiration"],
            "DTE": get("dte_left"), "contracts": int(row["contracts"]),
            "credit": row["actual_fill"] if pd.notna(row.get("actual_fill"))
            else row["modelled_fill"],
            "mark": get("mark"), "P&L": get("pnl"), "% max": get("profit_pct"),
            "best": get("best_pct"), "worst": get("worst_pct"),
            "P(target) entry": _entry(row, "p_hit_50"), "P(target) now": get("p_target_now"),
            "POP now": get("pop_now"), "verdict": get("verdict"),
            "why": get("verdict_reason"),
            "marked": get("marked_at")})
    pct = "percent"
    st.dataframe(pd.DataFrame(table), hide_index=True, width="stretch", column_config={
        "credit": st.column_config.NumberColumn(format="$%.2f"),
        "mark": st.column_config.NumberColumn(format="$%.2f"),
        "P&L": st.column_config.NumberColumn(format="dollar"),
        "% max": st.column_config.NumberColumn(format=pct),
        "best": st.column_config.NumberColumn(format=pct),
        "worst": st.column_config.NumberColumn(format=pct),
        "P(target) entry": st.column_config.NumberColumn(format=pct),
        "P(target) now": st.column_config.NumberColumn(format=pct),
        "POP now": st.column_config.NumberColumn(format=pct),
        "marked": st.column_config.DatetimeColumn(format="MMM D HH:mm"),
        "why": st.column_config.TextColumn(width="large")})
    tracked_n = int((open_rows["book"] == "tracked").sum())
    st.caption(f"{tracked_n} tracked forward test(s) never count against the account; "
               f"{len(open_rows) - tracked_n} taken trade(s) do (Portfolio page).")

# --- One position --------------------------------------------------------------------------

st.subheader("Position")
labels = {int(r["id"]): f"#{int(r['id'])} {r['ticker']} {paper.leg_text(r)} "
          f"({r.get('book')}, {r['status']})" for _, r in positions.iterrows()}
pick = st.selectbox("Position", list(labels), format_func=labels.get, key="track_pick")
position = positions[positions["id"] == pick].iloc[0].to_dict()
history = tracking.marks(pick)
last = history.iloc[-1].to_dict() if not history.empty else None

tabs = st.tabs(["Entry vs now", "Marks & probabilities", "P&L attribution", "Observations",
                "Promote"])
with tabs[0]:
    frame = tracking.entry_vs_now(position, last)

    def _fmt(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        return f"{v:,.3f}" if isinstance(v, (int, float)) else str(v)

    st.dataframe(frame.map(_fmt), hide_index=True, width="stretch")
    if last:
        st.markdown(f"**Verdict:** `{last.get('verdict')}` -- {last.get('verdict_reason') or ''}")
    else:
        st.caption("No mark yet: press Update now.")
with tabs[1]:
    if history.empty:
        st.caption("No marks yet.")
    else:
        chart = history.set_index("marked_at")[["profit_pct", "p_target_now", "pop_now"]]
        chart.columns = ["share of max profit", "P(reach target) from now", "POP from now"]
        st.line_chart(chart)
        st.dataframe(history[["marked_at", "session_block", "spot", "mark", "pnl", "profit_pct",
                              "best_pct", "worst_pct", "dte_left", "p_target_now",
                              "p_max_loss_now", "pop_now", "verdict", "verdict_reason"]],
                     hide_index=True, width="stretch")
with tabs[2]:
    attr = history.dropna(subset=["pnl_change"]) if not history.empty else history
    if attr.empty:
        st.caption("Attribution needs two marks.")
    else:
        parts = attr.set_index("marked_at")[["attr_delta", "attr_gamma", "attr_theta",
                                             "attr_vega", "attr_residual"]]
        parts.columns = ["delta", "gamma", "theta", "vega", "residual"]
        st.bar_chart(parts)
        totals = parts.sum()
        st.caption("Since the first mark: " + ", ".join(f"{k} ${v:,.0f}" for k, v in totals.items())
                   + f" = ${attr['pnl_change'].sum():,.0f}. Each interval uses the previous "
                     "mark's Greeks; IV changes are implied from each leg's mid.")
        st.dataframe(attr[["marked_at", "spot", "pnl_change", "attr_delta", "attr_gamma",
                           "attr_theta", "attr_vega", "attr_residual"]],
                     hide_index=True, width="stretch")
with tabs[3]:
    obs = tracking.observations(pick)
    if obs.empty:
        st.caption("No observations.")
    else:
        st.dataframe(obs, hide_index=True, width="stretch")
with tabs[4]:
    if position.get("book") != "tracked" or position["status"] != "open":
        st.caption("Only an open tracked position can be promoted to a real trade.")
    else:
        with st.form("promote"):
            c = st.columns(2)
            fill = c[0].number_input("Your net fill ($/share)", min_value=0.0, step=0.01,
                                     value=float(position["modelled_fill"]))
            qty = c[1].number_input("Contracts", min_value=1, step=1,
                                    value=int(position["contracts"]))
            if st.form_submit_button("Record as taken"):
                try:
                    result = tracking.promote(pick, actual_fill=fill, contracts=int(qty))
                    st.success(result.message + f" -- taken #{result.position_id}")
                except ValueError as exc:
                    st.error(str(exc))

# --- Closed ------------------------------------------------------------------------------

st.subheader("Closed: hold-to-expiry vs managed")
closed = tracking.closed_outcomes(book)
if closed.empty:
    st.caption("Nothing closed yet.")
else:
    st.dataframe(closed, hide_index=True, width="stretch", column_config={
        "hold_pnl": st.column_config.NumberColumn("hold P&L", format="dollar"),
        "managed_pnl": st.column_config.NumberColumn("managed P&L", format="dollar"),
        "rec_pop": st.column_config.NumberColumn("POP at entry", format="percent")})

with st.expander("Chain archive"):
    from data_sources import chain_archive
    archives = chain_archive.list_archives()
    if archives:
        st.dataframe(pd.DataFrame([{k: a.get(k) for k in ("date", "block", "rows", "bytes",
                                                          "seconds")}
                                   | {"tickers": len(a.get("tickers", [])),
                                      "failed": len(a.get("failed", {}))}
                                   for a in archives]), hide_index=True, width="stretch")
    else:
        st.caption("No archived days yet.")
