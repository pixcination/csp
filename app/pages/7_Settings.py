"""
Settings -- account profiles and ranking-weight presets, defined by the user.

Both are stored in config/user_settings.yaml (core/user_settings.py) and
override the shipped config.yaml defaults of the same name. A scan request
picks a profile with `account_profile` and weights with `ranking_weights`;
the Command Center Run form offers both.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from analytics import sizing  # noqa: E402
from core import user_settings as us  # noqa: E402

st.set_page_config(page_title="Settings", layout="wide")
st.title("Settings")
st.caption(f"Saved to `{us.path()}`. Entries override the config.yaml defaults of the "
           f"same name; deleting an override restores the default.")

tab_profiles, tab_weights = st.tabs(["Account profiles", "Ranking weights"])

# --- Account profiles ----------------------------------------------------------

with tab_profiles:
    profiles = us.account_profiles()
    user_defined = set((us.load().get("account_profiles") or {}))
    rows = []
    for name in us.profile_names():
        cfg = sizing.account_config(name)
        rows.append({"profile": name, "user defined": name in user_defined,
                     "account value": cfg.get("net_liquidating_value"),
                     "per position": cfg.get("max_collateral_per_position_pct"),
                     "per ticker": cfg.get("max_collateral_per_ticker_pct"),
                     "max positions": cfg.get("max_open_positions"),
                     "cash-secured": cfg.get("require_cash_secured"),
                     "spreads": cfg.get("spread_approval", True),
                     "strategies": ", ".join(cfg.get("allowed_strategies") or [])})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={
                     "account value": st.column_config.NumberColumn(format="$%,.0f"),
                     "per position": st.column_config.NumberColumn(format="percent"),
                     "per ticker": st.column_config.NumberColumn(format="percent")})

    st.markdown("**Add or edit a profile**")
    names = us.profile_names()
    pick = st.selectbox("Start from", ["(new profile)"] + names, key="profile_pick")
    base = sizing.account_config(None if pick == "(new profile)" else pick)
    with st.form("profile_form"):
        name = st.text_input("Name", "" if pick == "(new profile)" else pick,
                             help="e.g. roth_ira, traditional_ira, taxable")
        cols = st.columns(3)
        nlv = cols[0].number_input("Account value ($)", min_value=0.0, step=1000.0,
                                   value=float(base.get("net_liquidating_value", 0.0)))
        per_pos = cols[1].number_input("Max per position (%)", 0.1, 100.0, step=0.5,
                                       value=100 * float(base.get("max_collateral_per_position_pct", 0.04)))
        per_tick = cols[2].number_input("Max per ticker (%)", 0.1, 100.0, step=0.5,
                                        value=100 * float(base.get("max_collateral_per_ticker_pct", 0.06)))
        cols = st.columns(3)
        per_sector = cols[0].number_input("Max per sector (%)", 0.1, 100.0, step=1.0,
                                          value=100 * float(base.get("max_sector_collateral_pct", 0.25)))
        buffer = cols[1].number_input("Cash buffer (%)", 0.0, 90.0, step=1.0,
                                      value=100 * float(base.get("cash_buffer_pct", 0.08)))
        max_pos = cols[2].number_input("Max open positions", 1, 500, step=1,
                                       value=int(base.get("max_open_positions", 25)))
        cols = st.columns(3)
        strategies = cols[0].multiselect("Allowed strategies", list(us.STRATEGY_CHOICES),
                                         default=base.get("allowed_strategies")
                                         or list(us.STRATEGY_CHOICES))
        cash_secured = cols[1].checkbox("Cash-secured puts only",
                                        value=bool(base.get("require_cash_secured", True)))
        spreads = cols[2].checkbox("Spreads approved", value=bool(base.get("spread_approval", True)))
        saved = st.form_submit_button("Save profile", type="primary")
    if saved:
        try:
            stored = us.save_account_profile(name, {
                "net_liquidating_value": nlv,
                "max_collateral_per_position_pct": per_pos / 100,
                "max_collateral_per_ticker_pct": per_tick / 100,
                "max_sector_collateral_pct": per_sector / 100,
                "cash_buffer_pct": buffer / 100, "max_open_positions": int(max_pos),
                "allowed_strategies": strategies, "require_cash_secured": cash_secured,
                "spread_approval": spreads})
            st.success(f"Saved profile '{stored}'.")
            st.rerun()
        except us.SettingsError as exc:
            st.error(str(exc))

    if user_defined:
        with st.form("profile_delete"):
            victim = st.selectbox("Delete a user profile / override", sorted(user_defined))
            if st.form_submit_button("Delete"):
                us.delete_account_profile(victim)
                st.rerun()

# --- Ranking weights ---------------------------------------------------------------

with tab_weights:
    st.caption("How the underlying ranking combines its components. The Phase 14 "
               "calibration (Validation page) found only the IV-rank component (and, weakly, "
               "drawdown) predicting how much premium a short put kept; trend and support "
               "showed nothing, and IV/RV and liquidity cannot be tested. `calibrated` "
               "follows that result. Pick a preset, or define your own.")
    presets = us.weight_presets()
    user_presets = set((us.load().get("ranking_weight_presets") or {}))
    table = pd.DataFrame(presets).T.reindex(columns=list(us.COMPONENTS)).fillna(0.0)
    table.insert(0, "user defined", [n in user_presets for n in table.index])
    table.index.name = "preset"
    st.dataframe(table.reset_index(), hide_index=True, width="stretch")
    st.caption(f"Default preset: `{us.default_weight_preset()}`.")

    st.markdown("**Add or edit a preset**")
    start = st.selectbox("Start from", list(presets), key="preset_pick")
    with st.form("preset_form"):
        preset_name = st.text_input("Name", start if start in user_presets else "")
        cols = st.columns(len(us.COMPONENTS))
        values = {c: cols[i].number_input(c, 0.0, 1.0, step=0.05,
                                          value=float(presets[start].get(c, 0.0)))
                  for i, c in enumerate(us.COMPONENTS)}
        st.caption("Weights need not sum to 1 -- the score renormalises over the "
                   "components that have data.")
        if st.form_submit_button("Save preset", type="primary"):
            try:
                stored = us.save_weight_preset(preset_name, values)
                st.success(f"Saved preset '{stored}'.")
                st.rerun()
            except us.SettingsError as exc:
                st.error(str(exc))
    if user_presets:
        with st.form("preset_delete"):
            victim = st.selectbox("Delete a user preset", sorted(user_presets))
            if st.form_submit_button("Delete"):
                us.delete_weight_preset(victim)
                st.rerun()
