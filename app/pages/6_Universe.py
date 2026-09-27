"""
Universe -- the registry of symbols the tool knows about (Phase 9).

Add a stock, ETF or index and it gets daily and weekly bars, TastyTrade
market metrics, earnings/dividend/split events and a Stage 1 verdict on the
next pipeline run -- or immediately, with "Refresh data for this symbol".
Deactivate rather than delete to keep the tags. Every change is mirrored to
the versioned config/universe.csv.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from data_sources import events, tasty_metrics, universe  # noqa: E402

st.set_page_config(page_title="Universe", layout="wide")
st.title("Universe")
st.caption("One row per symbol. Data stages (bars, metrics, events) cover every "
           "active symbol; the cash-secured-put engine skips cash-settled indices. "
           "Stage 1 is advisory: it never deactivates a symbol on its own.")


def refresh_symbol(symbol: str, asset_class: str) -> list[str]:
    """The data stages for one symbol, synchronously."""
    from analytics import universe_screen
    from data_sources import yfinance_sync
    notes = []
    [daily] = yfinance_sync.sync_daily([symbol])
    notes.append(f"daily bars: {daily.error or f'+{daily.rows_added} rows'}")
    if asset_class == "stock":
        fresh = yfinance_sync.sync_earnings([symbol])      # merges into the file
        notes.append(f"earnings dates: {len(fresh)}")
    result = tasty_metrics.sync([symbol], force=True)
    notes.append(f"market metrics: {'ok' if result['stored'] else 'none returned'}")
    events.build()
    notes.append("events rebuilt")
    stage1 = universe_screen.screen([symbol])
    notes.append(f"stage 1: {stage1['tier'].iloc[0]} {stage1['reasons'].iloc[0] or ''}")
    return notes


# --- Add -------------------------------------------------------------------

with st.expander("Add a symbol", expanded=False):
    with st.form("add_symbol", clear_on_submit=True):
        cols = st.columns([2, 2, 3, 3])
        symbol = cols[0].text_input("Symbol", placeholder="e.g. MSFT, BRK.B, SPX")
        asset_class = cols[1].selectbox("Asset class", universe.ASSET_CLASSES)
        tags = cols[2].text_input("Tags", placeholder="comma separated")
        notes = cols[3].text_input("Notes")
        refresh = st.checkbox("Refresh its data now (about 10 s)", value=True)
        submitted = st.form_submit_button("Add", type="primary")
    if submitted and symbol.strip():
        record = universe.add(symbol, asset_class=asset_class, tags=tags, notes=notes)
        st.success(f"Added {record['symbol']} (Yahoo `{record['yf_symbol']}`, "
                   f"TastyTrade `{record['tt_symbol']}`).")
        if refresh:
            with st.spinner("Pulling bars, metrics and events..."):
                try:
                    for line in refresh_symbol(record["symbol"], record["asset_class"]):
                        st.write("- " + line)
                except Exception as exc:
                    st.error(f"{type(exc).__name__}: {exc}")

# --- Registry --------------------------------------------------------------

registry = universe.load()
metrics = tasty_metrics.latest()
if not metrics.empty:
    registry = registry.merge(
        metrics[["symbol", "ivr", "ivp", "iv_index", "liquidity_rating", "beta",
                 "snapshot_date"]], on="symbol", how="left")
upcoming_earnings = events.load()
if not upcoming_earnings.empty:
    today = dt.date.today()
    nxt = (upcoming_earnings[(upcoming_earnings["type"] == "earnings")
                             & (upcoming_earnings["date"] >= today)]
           .sort_values("date").groupby("symbol").first()[["date", "sources_disagree"]]
           .rename(columns={"date": "next_earnings", "sources_disagree": "earnings_disagree"}))
    registry = registry.merge(nxt, left_on="symbol", right_index=True, how="left")

counts = registry[registry["active"]]["asset_class"].value_counts()
top = st.columns(5)
top[0].metric("Active symbols", int(registry["active"].sum()))
top[1].metric("Stocks", int(counts.get("stock", 0)))
top[2].metric("ETFs", int(counts.get("etf", 0)))
top[3].metric("Indices", int(counts.get("index", 0)))
top[4].metric("Stage 1 pass", int(registry["stage1_pass"].fillna(False).astype(bool).sum()))

filter_cols = st.columns([2, 2, 3])
show = filter_cols[0].selectbox("Show", ["active", "all", "inactive", "stage 1 rejected"])
classes = filter_cols[1].multiselect("Asset class", universe.ASSET_CLASSES,
                                     default=list(universe.ASSET_CLASSES))
search = filter_cols[2].text_input("Search symbol / sector / tags")

view = registry[registry["asset_class"].isin(classes)]
if show == "active":
    view = view[view["active"]]
elif show == "inactive":
    view = view[~view["active"]]
elif show == "stage 1 rejected":
    view = view[view["stage1_tier"] == "rejected"]
if search:
    needle = search.lower()
    view = view[view.apply(lambda r: needle in " ".join(
        str(r.get(c, "")) for c in ("symbol", "sector", "industry", "category", "tags")).lower(),
        axis=1)]

EDITABLE = ["active", "tags", "notes"]
columns = ["symbol", "active", "asset_class", "sector", "category", "tags",
           "ivr", "ivp", "liquidity_rating", "next_earnings", "earnings_disagree",
           "weeklies", "settlement", "settlement_times", "stage1_tier", "stage1_reasons",
           "yf_symbol", "tt_symbol", "notes"]
columns = [c for c in columns if c in view.columns]
edited = st.data_editor(
    view[columns], hide_index=True, width="stretch", key="registry_editor",
    disabled=[c for c in columns if c not in EDITABLE],
    column_config={
        "ivr": st.column_config.ProgressColumn("IVR", min_value=0.0, max_value=1.0, format="%.2f"),
        "ivp": st.column_config.ProgressColumn("IVP", min_value=0.0, max_value=1.0, format="%.2f"),
        "liquidity_rating": st.column_config.NumberColumn("Liq.", format="%d"),
        "earnings_disagree": st.column_config.CheckboxColumn("Dates disagree"),
        "stage1_reasons": st.column_config.TextColumn("Stage 1 reasons", width="medium"),
    })

changes = []
original = view[columns].set_index("symbol")
for _, row in edited.iterrows():
    before = original.loc[row["symbol"]]
    diff = {c: row[c] for c in EDITABLE
            if c in row and not (pd.isna(row[c]) and pd.isna(before[c])) and row[c] != before[c]}
    if diff:
        changes.append((row["symbol"], diff))
if changes:
    st.info(f"{len(changes)} unsaved change(s): " +
            "; ".join(f"{s} {d}" for s, d in changes[:5]))
    if st.button("Save changes", type="primary"):
        for symbol_, diff in changes:
            universe.update(symbol_, **{k: (bool(v) if k == "active" else v)
                                        for k, v in diff.items()})
        st.success("Saved.")
        st.rerun()

# --- Detail ----------------------------------------------------------------

st.divider()
st.subheader("Symbol detail")
choice = st.selectbox("Symbol", registry["symbol"].tolist(),
                      index=registry["symbol"].tolist().index("SPY")
                      if "SPY" in registry["symbol"].tolist() else 0)
row = registry[registry["symbol"] == choice].iloc[0]
detail = st.columns(3)
with detail[0]:
    st.markdown(f"**{choice}** · {row['asset_class']} · {row['settlement']}-settled, "
                f"{row['exercise']}")
    st.caption(f"Yahoo `{row['yf_symbol']}` × {row['price_scale']:g} · "
               f"TastyTrade `{row['tt_symbol']}`")
    if st.button("Refresh data for this symbol"):
        with st.spinner("Refreshing..."):
            for line in refresh_symbol(choice, row["asset_class"]):
                st.write("- " + line)

with detail[1]:
    from analytics import bars
    weekly = bars.load_weekly(choice)
    if weekly.empty:
        st.caption("No bars yet.")
    else:
        st.caption(f"Weekly bars (resampled from daily; {len(weekly):,} weeks)")
        st.dataframe(weekly.tail(6)[["week_end", "open", "high", "low", "close", "complete"]],
                     hide_index=True, width="stretch")

with detail[2]:
    if row["asset_class"] == "stock":
        from analytics import earnings_history
        summary = earnings_history.summary(choice)
        if summary.get("n"):
            st.caption(f"Last {summary['n']} earnings reactions (price basis)")
            st.metric("Median |move|", f"{summary['median_abs_move']:.1%}",
                      delta=f"max {summary['max_abs_move']:.1%}", delta_color="off")
            st.metric("Median in ATR units", f"{summary['median_atr_multiple']:.2f}")
            st.caption("Beat-the-implied-move rate appears once market-metric "
                       "snapshots span a report." if summary["implied_known"] == 0 else
                       f"Beat the implied move {summary['beat_rate']:.0%} of "
                       f"{summary['implied_known']} reports")
        else:
            st.caption("No earnings history yet.")
    else:
        st.caption("ETFs and indices do not report earnings.")

sym_events = events.upcoming(choice, days=60)
if not sym_events.empty:
    st.dataframe(sym_events[["date", "symbol", "type", "time_of_day", "confirmed",
                             "source", "note"]], hide_index=True, width="stretch")

# --- Market calendar --------------------------------------------------------

st.divider()
st.subheader("Next 45 days: market-wide events")
market = events.upcoming(days=45)
market = market[market["symbol"] == events.MARKET] if not market.empty else market
if market.empty:
    st.caption("No events table yet -- run the pipeline.")
else:
    policy = events._policy("csp")
    market = market.assign(policy=market["type"].map(
        lambda t: (policy.get(t) or {}).get("action", "ignore")))
    st.dataframe(market[["date", "type", "time_of_day", "policy", "note"]],
                 hide_index=True, width="stretch")
    st.caption("Policy per type is config.yaml -> event_policy. FOMC/CPI/NFP dates "
               "come from config/macro_calendar.yaml -- keep it current.")
