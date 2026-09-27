"""Trade log page -- manually logged positions, outcome tracking (rolled /
assigned / expired), realized win-rate stats vs. the backtest's theoretical
numbers. See docs/PROJECT_SPEC.md Page 3.

This is a research log, not a broker-synced portfolio -- no order placement
or broker integration, by design (see docs/PROJECT_SPEC.md "Explicitly out of
scope")."""
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import streamlit as st

from analytics.strategies import STRATEGIES
from analytics.trade_log import add_position, update_position, delete_position, list_positions, summary_stats, STATUSES
from analytics.backtest import run_backtest
from app.components.formatting import fmt_currency, fmt_pct

st.title("Trade Log")
st.caption("Manual position log -- not broker-synced, no order placement.")

with st.expander("Log a new position", expanded=False):
    with st.form("new_position", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            ticker = st.text_input("Ticker").strip().upper()
            strategy = st.selectbox("Strategy", list(STRATEGIES.keys()),
                                     format_func=lambda k: STRATEGIES[k].label)
        with c2:
            strike = st.number_input("Strike", min_value=0.0, step=0.5)
            contracts = st.number_input("Contracts", min_value=1, value=1, step=1)
        with c3:
            premium = st.number_input("Premium collected (per share, $)", min_value=0.0, step=0.01)
            commission = st.number_input("Commission (total, $)", min_value=0.0, value=1.30, step=0.05)

        c4, c5 = st.columns(2)
        with c4:
            entry_date = st.date_input("Entry date", value=date.today())
        with c5:
            expiration = st.date_input("Expiration")
        notes = st.text_area("Notes", height=68)

        if st.form_submit_button("Log position"):
            if not ticker or strike <= 0 or premium <= 0:
                st.error("Ticker, strike, and premium are required.")
            else:
                add_position(ticker, strategy, strike, expiration, int(contracts),
                             premium, commission, entry_date, notes)
                st.success(f"Logged {ticker} {strike} {STRATEGIES[strategy].label}.")
                st.rerun()

positions = list_positions()

st.divider()
st.subheader("Open positions")
open_pos = positions[positions["status"] == "open"]
if open_pos.empty:
    st.caption("No open positions logged.")
else:
    for _, p in open_pos.iterrows():
        with st.expander(f"{p['ticker']} ${p['strike']:g} {STRATEGIES.get(p['strategy'], STRATEGIES['csp']).label} "
                          f"-- exp {p['expiration']} -- {p['contracts']}x @ ${p['premium_collected']:.2f}"):
            oc1, oc2, oc3, oc4 = st.columns(4)
            with oc1:
                new_status = st.selectbox("Mark outcome", [s for s in STATUSES if s != "open"],
                                           key=f"status_{p['id']}")
            with oc2:
                exit_date = st.date_input("Exit date", value=date.today(), key=f"exitdate_{p['id']}")
            with oc3:
                exit_price = st.number_input("Premium paid to close ($, 0 if expired/assigned)",
                                              min_value=0.0, step=0.01, key=f"exitprice_{p['id']}")
            with oc4:
                st.write("")
                st.write("")
                if st.button("Save outcome", key=f"save_{p['id']}"):
                    update_position(int(p["id"]), new_status, exit_date, exit_price)
                    st.rerun()
            if st.button("Delete (logged in error)", key=f"del_{p['id']}"):
                delete_position(int(p["id"]))
                st.rerun()

st.divider()
st.subheader("Closed positions")
closed_pos = positions[positions["status"] != "open"]
if closed_pos.empty:
    st.caption("No closed positions yet.")
else:
    display = closed_pos[["ticker", "strategy", "strike", "expiration", "contracts",
                           "premium_collected", "status", "exit_date", "exit_price", "notes"]].copy()
    display["strategy"] = display["strategy"].map(lambda s: STRATEGIES.get(s, STRATEGIES["csp"]).label)
    st.dataframe(display, width="stretch", hide_index=True)

st.divider()
st.subheader("Realized performance vs. backtest")
stats = summary_stats(positions)
if stats["overall"] is None:
    st.info("Log and close some positions to see realized win-rate stats here.")
else:
    o = stats["overall"]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Closed positions", o["n_closed"])
    m2.metric("Win rate (expired OTM)", fmt_pct(o["win_rate"]))
    m3.metric("Assignment frequency", fmt_pct(o["assignment_frequency"]))
    m4.metric("Total realized P&L", fmt_currency(o["total_realized_pnl"]))

    st.markdown("**By ticker** (realized vs. backtest-theoretical, where available)")
    by_ticker = stats["by_ticker"].copy()
    rows = []
    for _, r in by_ticker.iterrows():
        bt_win_rate = None
        try:
            bt = run_backtest(r["ticker"])["summary"]
            bt_win_rate = bt.get("win_rate_expired_otm")
        except Exception:
            pass
        rows.append({
            "Ticker": r["ticker"], "Closed": r["n_closed"],
            "Realized win rate": fmt_pct(r["win_rate"]),
            "Backtest win rate": fmt_pct(bt_win_rate) if bt_win_rate is not None else "n/a",
            "Assignment freq.": fmt_pct(r["assignment_frequency"]),
            "Avg return (ann.)": fmt_pct(r["avg_realized_return_annualized"]),
            "Total P&L": fmt_currency(r["total_realized_pnl"]),
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
