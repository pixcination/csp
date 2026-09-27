"""
Wheel -- the second half: shares you hold, calls to write, puts to defend.

Three things live here, in the order they demand attention:

  1. Rolls, for puts that have gone against you. This is where assignment is
     actually avoided.
  2. Covered calls against assigned shares, with the never-below-basis rule
     visible rather than buried.
  3. The cycle backtest and parameter sweep, which is what turns the reasoned
     management rules into fitted ones.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from analytics import covered_call, paper, roll_engine  # noqa: E402
from core.paths import load_config, load_universe  # noqa: E402

st.set_page_config(page_title="Wheel", layout="wide")
st.title("Wheel")

manifest = st.session_state.get("last_manifest")
analyse = manifest.stages.get("analyse", {}) if manifest else {}

# --- 1. Defence ------------------------------------------------------------

st.subheader("Positions needing defence")
rolls = analyse.get("rolls", [])

if not rolls:
    st.caption("No short puts are near or through their strike. Rolls are ranked "
               "here automatically when one is — a put comfortably out of the money "
               "does not need a roll analysis.")
else:
    for entry in rolls:
        candidate = entry.get("candidate")
        with st.container(border=True):
            head = st.columns([3, 1])
            head[0].markdown(f"### Position #{entry.get('position_id')}")
            head[1].markdown(f"`{entry.get('action', '').upper()}`")
            st.write(entry.get("message", ""))

            if candidate:
                cols = st.columns(5)
                cols[0].metric("Net credit", f"${candidate['net_credit_dollars']:,.0f}")
                cols[1].metric("New strike", f"${candidate['to_strike']:g}",
                                delta=f"{candidate['strike_change']:+.2f}")
                cols[2].metric("Days added", f"{candidate['days_added']}")
                cols[3].metric("Annualised on added days",
                                f"{candidate['annualised_on_added_days']:.1%}")
                if candidate.get("risk_reduction") is not None:
                    cols[4].metric("Assignment odds",
                                    f"{candidate['new_prob_assign']:.0%}",
                                    delta=f"{-candidate['risk_reduction']:+.0%}",
                                    delta_color="inverse")
                st.caption(f"New breakeven ${candidate['new_breakeven']:.2f}. "
                           f"Cost to close the current leg ${candidate['cost_to_close']:.2f}, "
                           f"new leg pays ${candidate['new_credit']:.2f}.")

                alts = entry.get("alternatives", [])
                if alts:
                    with st.expander(f"{len(alts)} other credit roll(s)"):
                        st.dataframe(pd.DataFrame(alts)[[
                            "to_expiration", "to_strike", "net_credit_dollars",
                            "days_added", "annualised_on_added_days",
                            "new_prob_assign"]],
                            hide_index=True, width="stretch")
            else:
                st.info("Taking assignment is the recommendation here. That is the "
                        "strategy working, not failing — you own the stock at a "
                        "basis you already agreed to, and the covered-call side "
                        "takes over.", icon=":material/check_circle:")

st.divider()

# --- 2. Covered calls ------------------------------------------------------

st.subheader("Covered calls")

lots = paper.list_share_lots(open_only=True)
if lots.empty:
    st.caption("No assigned shares yet. When a put is assigned, the share lot appears "
               "here with its adjusted basis — the strike less every premium collected "
               "against that cycle — and calls are ranked against it.")
else:
    allow_below = st.checkbox(
        "Show strikes below basis", value=False,
        help="Selling a call under your basis converts an unrealised loss into a "
             "realised one. Those strikes pay more, so a yield-sorted list puts them "
             "first — which is exactly why they are hidden by default. Enable this "
             "only when you have decided to exit at a loss deliberately.")

    sheet = analyse.get("covered_calls") or []
    if not sheet:
        with st.spinner("Ranking calls against held lots..."):
            frame = covered_call.sheet_for_all_lots(allow_below_basis=allow_below)
        sheet = frame.to_dict("records") if not frame.empty else []

    for record in sheet:
        with st.container(border=True):
            head = st.columns([3, 1])
            head[0].markdown(
                f"### {record['ticker']} &nbsp;"
                f"<span style='font-size:.6em;opacity:.7'>"
                f"{record.get('shares', 0):,} shares @ "
                f"${record.get('basis', 0):.2f} basis</span>",
                unsafe_allow_html=True)

            if not record.get("accepted"):
                st.warning(record.get("rationale", "No call available above basis."))
                for reason in record.get("rejections", []) or []:
                    st.caption(f"· {reason}")
                continue

            head[1].metric("If called away",
                            f"{record['annualised_if_called']:.1%}")
            cols = st.columns(6)
            cols[0].metric("Strike", f"${record['strike']:g}",
                            delta=f"{record['strike'] - record['basis']:+.2f} vs basis")
            cols[1].metric("Contracts", f"{record['contracts']}")
            cols[2].metric("Credit", f"${record['net_credit']:,.0f}")
            cols[3].metric("P(called away)",
                            f"{record['prob_called_away']:.0%}"
                            if record.get("prob_called_away") is not None else "--")
            cols[4].metric("Total if called", f"${record['gain_if_called']:,.0f}")
            cols[5].metric("If it expires",
                            f"{record['annualised_if_expires']:.1%}")

            st.caption(record.get("rationale", ""))
            if record.get("ex_dividend_risk"):
                st.warning(record["ex_dividend_risk"])
            for warning in record.get("warnings", []) or []:
                st.caption(f"⚠ {warning}")

st.divider()

# --- 3. Backtest -----------------------------------------------------------

st.subheader("Cycle backtest")
st.caption(
    "Simulates full wheel cycles — put sold, assigned or expired, calls written "
    "above basis until called away — and reports return per capital-day. The old "
    "harness modelled a naked put and booked assignment as a realised loss, which "
    "measured a strategy you do not trade.")

universe = load_universe()
controls = st.columns([2, 1, 1, 1, 1])
ticker = controls[0].selectbox("Ticker", universe or ["SPY"])
put_delta = controls[1].number_input("Put delta", -0.50, -0.05, -0.20, 0.05)
put_dte = controls[2].number_input("Put DTE", 3, 30, 7, 1)
call_delta = controls[3].number_input("Call delta", 0.05, 0.60, 0.25, 0.05)
years = controls[4].number_input("Years", 3, 30, 15, 1)

run_cols = st.columns([1, 1, 4])
run_one = run_cols[0].button("Run backtest", type="primary")
run_sweep = run_cols[1].button("Parameter sweep")

if run_one or run_sweep:
    from data_sources.yfinance_sync import load_daily_total_return
    from analytics.wheel_backtest import (WheelParams, compare_to_buy_and_hold,
                                            run_wheel, sweep)

    daily = load_daily_total_return(ticker)
    if daily.empty:
        st.error(f"No daily history for {ticker}. Run the pipeline first.")
    else:
        cutoff = daily["date"].max() - pd.DateOffset(years=int(years))
        daily = daily[daily["date"] >= cutoff]

        if run_one:
            params = WheelParams(put_delta=float(put_delta), put_dte=int(put_dte),
                                  call_delta=float(call_delta))
            with st.spinner("Simulating cycles..."):
                result = run_wheel(daily, ticker, params)
            if result.summary.get("error"):
                st.error(result.summary["error"])
            else:
                summary = result.summary
                row1 = st.columns(5)
                row1[0].metric("Cycles", f"{summary['n_cycles']}")
                row1[1].metric("Expired clean", f"{summary['pct_expired_clean']:.0%}")
                row1[2].metric("Assigned", f"{summary['assignment_rate']:.0%}")
                row1[3].metric("Profitable cycles",
                                f"{summary['pct_profitable_cycles']:.0%}")
                row1[4].metric("Annualised on capital",
                                f"{summary['annualised_on_capital_deployed']:.1%}")

                row2 = st.columns(5)
                row2[0].metric("Mean cycle", f"{summary['mean_cycle_days']:.0f}d")
                row2[1].metric("Mean assigned cycle",
                                f"{summary['mean_assigned_cycle_days']:.0f}d",
                                help="The capital-days cost of assignment — the "
                                     "number the old naked-put harness could not see.")
                row2[2].metric("Worst cycle", f"{summary['worst_cycle_days']}d")
                row2[3].metric("Calls per assignment",
                                f"{summary['mean_calls_per_assignment']:.1f}")
                row2[4].metric("Total fees", f"${summary['total_fees']:,.0f}")

                comparison = compare_to_buy_and_hold(daily, result)
                if comparison:
                    st.info(
                        f"Over {comparison['years']:.1f} years the wheel returned "
                        f"**{comparison['wheel_annualised_on_capital']:.1%}** "
                        f"annualised on capital deployed; buy-and-hold returned "
                        f"**{comparison['buy_and_hold_annualised']:.1%}**. "
                        f"{comparison['note']}",
                        icon=":material/compare_arrows:")

                with st.expander("Every cycle"):
                    st.dataframe(result.cycles, hide_index=True, width="stretch")

        if run_sweep:
            with st.spinner("Sweeping parameters — this takes a few seconds..."):
                grid = sweep(daily, ticker)
            if grid.empty:
                st.error("Sweep produced no results.")
            else:
                st.markdown("**Ranked by annualised return on capital deployed**")
                st.dataframe(
                    grid[["put_delta", "put_dte", "call_delta", "n_cycles",
                          "assignment_rate", "mean_cycle_days",
                          "annualised_on_capital_deployed", "worst_drawdown"]].head(12),
                    hide_index=True, width="stretch")
                st.caption(
                    "Read this for shape, not for the winning cell. A grid this size "
                    "always produces a winner, and the gap between the top few "
                    "settings is usually noise. What is informative is the trend — "
                    "whether returns fall off below a given delta, whether a longer "
                    "DTE is systematically worse.")

                st.markdown("**Mean return by each parameter, holding the others**")
                shape = st.columns(3)
                for i, column in enumerate(["put_delta", "put_dte", "call_delta"]):
                    means = grid.groupby(column)["annualised_on_capital_deployed"].mean()
                    shape[i].dataframe(
                        means.reset_index().rename(
                            columns={"annualised_on_capital_deployed": "mean return"}),
                        hide_index=True, width="stretch")
