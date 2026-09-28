"""
Strategies -- the multi-strategy screener (Phase 16, roadmap C.8).

    Recommend   for each name, the strategies whose entry conditions its IV
                regime, trend and earnings calendar meet -- resolved on the
                stored chain, run through the probability engine and ranked
                by blended EV per day on buying power; or pick specs and
                scan the universe for their best instances
    Library     every strategy spec in strategies/*.yaml: legs, conditions,
                exit rules, margin class, and whether your profile may trade it
    Matrix      the condition matrix (IV regime x trend -> strategies), and
                today's conditions per name

Results come from the latest run when it ran the recommender (a scan request
with `specs` or `recommend`), or from a scan run here on the stored chains.
Research only: nothing here places an order.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from analytics import margin, sizing, strategy_spec  # noqa: E402
from analytics import trade_detail as td  # noqa: E402
from analytics.strategies import resolver  # noqa: E402
from app.components import charts  # noqa: E402
from app.components.run_state import active_run  # noqa: E402
from core import user_settings  # noqa: E402

st.set_page_config(page_title="Strategies", layout="wide")
st.title("Strategies")
st.caption("Strategies described as data (strategies/*.yaml), resolved on the stored chains, "
           "priced, sized for your profile and ranked by blended EV per day on buying power. "
           "Research only: nothing here places an order.")

try:
    SPECS = strategy_spec.load_all()
except strategy_spec.SpecError as exc:
    st.error(f"A strategy spec is invalid: {exc}")
    SPECS = {}

tabs = st.tabs(["Recommend", "Library", "Condition matrix"])

# --- Recommend ------------------------------------------------------------------------------

def _items(value) -> list:
    """Rejections / warnings as a list, whether a tuple from a live scan or
    an array read back from a run's parquet."""
    if value is None or (isinstance(value, float) and value != value):
        return []
    return [str(v) for v in value] if not isinstance(value, str) else [value]


GRID = ["ticker", "label", "legs", "expiration", "dte_calendar", "modelled_fill", "contracts",
        "collateral", "max_loss", "pop_blend", "p_hit_50_blend", "headline_policy",
        "headline_ev", "ev_per_day_bpr", "iv_regime", "trend_state", "conditions",
        "accepted", "why_not"]

with tabs[0]:
    _, results, _source = active_run()
    stored = results.strategies if results is not None else pd.DataFrame()
    options = (["Latest run"] if not stored.empty else []) + ["Scan the stored chains now"]
    source = st.radio("Results", options, horizontal=True, key="strat_source")
    sheet = pd.DataFrame()
    if source == "Latest run":
        st.caption(f"From {results.label}.")
        sheet = stored
    else:
        with st.form("strat_scan"):
            c = st.columns([2, 2, 1, 1])
            tickers_text = c[0].text_input("Tickers", "SPY, QQQ, IWM, AAPL",
                                           help="Names with a stored chain. Run the pipeline "
                                                "with a request that includes specs to capture "
                                                "the expirations they need.")
            mode = c[1].radio("Which strategies", ["Recommend by conditions", "Chosen specs"],
                              horizontal=True)
            chosen = c[1].multiselect("Specs", list(SPECS), default=[],
                                      help="Used with 'Chosen specs': resolved on every ticker "
                                           "whatever the conditions.")
            profiles = user_settings.profile_names()
            profile = c[2].selectbox("Profile", profiles)
            paths = c[3].selectbox("Paths", [2000, 5000, 10000], index=1,
                                   help="Probability-engine paths per trade.")
            go = st.form_submit_button("Scan", type="primary")
        if go:
            from analytics import recommender
            from analytics.scan_request import ScanRequest
            tickers = [t.strip().upper() for t in tickers_text.split(",") if t.strip()]
            request = ScanRequest.default(account_profile=profile)
            spec_ids = chosen if mode == "Chosen specs" and chosen else None
            with st.spinner("Resolving and pricing..."):
                out = recommender.run(tickers, request=request, spec_ids=spec_ids,
                                      recommend=spec_ids is None, n_paths=int(paths))
            st.session_state["strat_scan_out"] = out
        out = st.session_state.get("strat_scan_out")
        if out is not None:
            sheet = out["sheet"]
            st.caption(f"{len(sheet)} position(s) in {out['seconds']:.1f} s at "
                       f"{out['n_paths']:,} paths per trade.")
            with st.expander("Conditions per ticker and strategy"):
                st.dataframe(out["conditions"], hide_index=True, width="stretch")

    if sheet.empty:
        st.info("No strategy positions yet. Scan the stored chains above, or run the pipeline "
                "with a scan request that has `recommend: true` or a list of `specs`.",
                icon=":material/info:")
    else:
        grid = sheet.copy()
        grid["why_not"] = grid.apply(
            lambda r: "" if r.get("accepted") else "; ".join(_items(r.get("rejections"))[:2]),
            axis=1)
        show_all = st.toggle("Show rejected positions", value=True, key="strat_all")
        if not show_all:
            grid = grid[grid["accepted"]]
        event = st.dataframe(
            grid[[c for c in GRID if c in grid.columns]], hide_index=True, width="stretch",
            on_select="rerun", selection_mode="multi-row", key="strat_grid",
            column_config={
                "modelled_fill": st.column_config.NumberColumn("Credit", format="$%.2f",
                                                               help="Net per share; negative = debit"),
                "collateral": st.column_config.NumberColumn("BPR", format="$%.0f"),
                "max_loss": st.column_config.NumberColumn("Max loss", format="$%.0f"),
                "pop_blend": st.column_config.ProgressColumn("POP", format="percent",
                                                             min_value=0, max_value=1),
                "p_hit_50_blend": st.column_config.ProgressColumn("P(50%)", format="percent",
                                                                  min_value=0, max_value=1),
                "headline_ev": st.column_config.NumberColumn("EV", format="$%.0f"),
                "ev_per_day_bpr": st.column_config.NumberColumn("EV/day/BPR", format="%.4f"),
            })
        picked = event.selection.rows if event and event.selection else []
        # Phase 18: several rows can be logged as tracked forward tests; the
        # detail below shows the first selected.
        if picked:
            from analytics import tracking
            if st.button(f"Log {len(picked)} selected as tracked", key="strat_log",
                         help="Tracking page. Research-only (naked) rows are logged but "
                              "never auto-tracked (Phase 19)."):
                run_id = results.run_id if source == "Latest run" and results is not None                     else None
                outcome = tracking.log(grid.iloc[picked], run_id=run_id, preset="strategies")
                opened = sum(o["action"] == "opened" for o in outcome)
                st.success(f"{opened} new tracked position(s); "
                           f"{sum(o['action'] == 'observed' for o in outcome)} already tracked.")
                for o in outcome:
                    if o["action"] == "skipped":
                        st.warning(o["message"])
        row = grid.iloc[picked[0]].to_dict() if picked else grid.iloc[0].to_dict()
        spec = SPECS.get(row["strategy"])

        st.subheader(f"{row['ticker']} {row['label']}: {row['legs']}")
        m = st.columns(6)
        m[0].metric("Credit / share", f"${row['modelled_fill']:,.2f}",
                    delta=f"natural ${row['natural']:,.2f}", delta_color="off")
        m[1].metric("Contracts", f"{int(row['contracts'])}")
        m[2].metric("BPR", f"${row['collateral']:,.0f}", delta=row["margin_class"],
                    delta_color="off")
        m[3].metric("Max loss", f"${row['max_loss']:,.0f}")
        m[4].metric("POP (blend)", f"{row['pop_blend']:.0%}"
                    if pd.notna(row.get("pop_blend")) else "--")
        m[5].metric(f"EV ({row.get('headline_policy')})", f"${row['headline_ev']:,.0f}"
                    if pd.notna(row.get("headline_ev")) else "--")
        if not row.get("accepted"):
            for reason in _items(row.get("rejections")):
                st.warning(reason, icon=":material/block:")
        for warning in _items(row.get("warnings")):
            st.caption(f"⚠ {warning}")
        position = resolver.position_from_row(row)
        size = max(int(row["contracts"]), 1)
        left, right = st.columns([3, 2])
        with left:
            payoff = td.payoff_frame(position, float(row["spot"]), int(row["dte_calendar"]),
                                     size)
            strikes = [(f"{l.side} {l.strike:g}{l.option_type[0].upper()}", l.strike, l.side)
                       for l in position.legs if not l.is_stock]
            st.plotly_chart(charts.payoff_chart(payoff, float(row["spot"]),
                                                position.breakevens, strikes),
                            width="stretch")
            st.caption("P&L at the front expiry and part-way there, before fees."
                       + (" The later leg is valued at the forward vol the entry term "
                          "structure implies -- model risk." if position.multi_expiry else ""))
        with right:
            legs = pd.DataFrame([{"leg": f"{l.side} {l.qty}x {l.option_type}",
                                  "strike": l.strike, "expiration": str(l.expiration),
                                  "bid": l.bid, "ask": l.ask, "model IV": l.iv,
                                  "delta": l.delta, "OI": l.open_interest}
                                 for l in position.legs])
            st.dataframe(legs, hide_index=True, width="stretch")
            st.markdown(f"**Why these strikes.** {row.get('strike_reasons', '')}")
            probs = {k: row.get(k) for k in ("pop_blend", "p_hit_25_blend", "p_hit_50_blend",
                                             "p_hit_100_blend", "p_touch_short_blend",
                                             "p_max_loss_blend") if pd.notna(row.get(k))}
            if probs:
                st.dataframe(pd.DataFrame([probs]).T.rename(columns={0: "blend"}),
                             width="stretch")
            st.caption(f"Conditions: {row.get('conditions')} · IV regime "
                       f"{row.get('iv_regime')} · trend {row.get('trend_state')}"
                       + (f" (Outlook Direction {row['outlook_direction']:.1f})"
                          if row.get("trend_source") == "outlook"
                          and pd.notna(row.get("outlook_direction")) else "") + " · "
                       f"models {row.get('models', '')}")
            if spec and spec.notes:
                for note in spec.notes:
                    st.caption(f"· {note}")

        with st.expander("Record to the paper book"):
            if spec is not None and spec.has_stock:
                st.info("Stock legs are not recorded here: buy the shares in the executing "
                        "account, then write the call against them on the Wheel page.")
            else:
                with st.form("strat_accept"):
                    f = st.columns(4)
                    qty = f[0].number_input("Contracts", 1, 1000, size)
                    fill = f[1].number_input("Actual net credit (negative = debit)",
                                             value=float(row["modelled_fill"]), step=0.01,
                                             format="%.2f")
                    entry = f[2].date_input("Entry date", value=dt.date.today())
                    note = f[3].text_input("Note", "")
                    ok = st.form_submit_button("Accept and record", type="primary")
                if ok:
                    from analytics import paper
                    try:
                        real = abs(fill - float(row["modelled_fill"])) > 1e-9
                        result = paper.accept(row, contracts=int(qty),
                                              actual_fill=float(fill) if real else None,
                                              entry_date=entry,
                                              run_id=results.run_id if source == "Latest run"
                                              and results is not None else None, notes=note)
                        st.success(result.message)
                    except Exception as exc:
                        st.error(f"{type(exc).__name__}: {exc}")

# --- Library ---------------------------------------------------------------------------------

with tabs[1]:
    profiles = user_settings.profile_names()
    profile = st.selectbox("Check permissions for profile", profiles, key="lib_profile")
    cfg = sizing.account_config(profile)
    rows = []
    for spec in SPECS.values():
        allowed, why = margin.permitted(spec.margin_class, cfg)
        rows.append({**spec.to_dict(), "allowed": allowed, "why not": why})
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    for spec in SPECS.values():
        with st.expander(f"{spec.label} ({spec.path})"):
            st.markdown(spec.description)
            for note in spec.notes:
                st.caption(f"· {note}")
            try:
                from core.paths import strategies_dir
                st.code((strategies_dir() / spec.path).read_text(encoding="utf-8"),
                        language="yaml")
            except OSError:
                pass
    st.caption("Add a strategy by adding a YAML file to strategies/ -- the format is in "
               "analytics/strategy_spec.py.")

# --- Matrix ----------------------------------------------------------------------------------

with tabs[2]:
    st.markdown("**IV regime x trend -> strategies whose entry conditions fit**")
    st.caption("Derived from the specs' `entry` blocks; earnings inside the trade blocks "
               "most of them on top of this. Trend (Phase 20): the Outlook's Direction dial at "
               "the spec's nearest expiry -- >= 6 uptrend, <= 4 downtrend, else range; it sits "
               "at 5 (range) where Direction has no walk-forward skill. IV regime: TastyTrade IV rank below "
               "recommender.iv_regime.low_below is low, above high_above is high.")
    if SPECS:
        st.dataframe(strategy_spec.condition_matrix(SPECS), hide_index=True, width="stretch")
    conditions = (results.strategy_conditions if results is not None
                  else pd.DataFrame())
    scan = st.session_state.get("strat_scan_out")
    if scan is not None and not scan["conditions"].empty:
        conditions = scan["conditions"]
    if not conditions.empty:
        st.markdown("**Conditions per name** (latest scan)")
        st.dataframe(conditions, hide_index=True, width="stretch")
