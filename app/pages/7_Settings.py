"""
Settings -- account profiles, ranking-weight presets and the schedule.

All are stored in config/user_settings.yaml (core/user_settings.py) and
override the shipped config.yaml defaults of the same name. A scan request
picks a profile with `account_profile` and weights with `ranking_weights`;
the Command Center Run form offers both. The Schedule tab (Phase 19) edits
the worker's timetable, marks saved presets auto, and shows the worker's
heartbeat and job history (pipeline/scheduler.py).
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

tab_profiles, tab_weights, tab_schedule = st.tabs(["Account profiles", "Ranking weights",
                                                  "Schedule"])

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
                     "account type": cfg.get("account_type"),
                     "naked": cfg.get("naked_approval", False),
                     "naked research-only": cfg.get("naked_research_only", False),
                     "placeholder": cfg.get("placeholder", False),
                     "strategies": ", ".join(cfg.get("allowed_strategies") or [])})
    table = st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                         on_select="rerun", selection_mode="single-row", key="profile_table",
                         column_config={
                             "account value": st.column_config.NumberColumn(format="$%,.0f"),
                             "per position": st.column_config.NumberColumn(format="percent"),
                             "per ticker": st.column_config.NumberColumn(format="percent")})
    st.caption("Click a row to edit that profile below.")

    st.markdown("**Add or edit a profile**")
    names = us.profile_names()
    # A newly clicked row picks the profile; the picker still works on its own
    # (a selection is applied once, not on every rerun).
    selected = table.selection.rows if table is not None else []
    clicked = rows[selected[0]]["profile"] if selected else None
    if clicked != st.session_state.get("profile_row_applied"):
        st.session_state["profile_row_applied"] = clicked
        if clicked is not None:
            st.session_state["profile_pick"] = clicked
    pick = st.selectbox("Start from", ["(new profile)"] + names, key="profile_pick")
    base = sizing.account_config(None if pick == "(new profile)" else pick)
    # Every field is keyed to the picked profile, so switching profiles always
    # loads that profile's saved values rather than the previous form state.
    k = f"pf|{pick}|"
    with st.form(f"profile_form|{pick}"):
        name = st.text_input("Name", "" if pick == "(new profile)" else pick, key=k + "name",
                             help="e.g. roth_ira, traditional_ira, taxable")
        cols = st.columns(3)
        nlv = cols[0].number_input("Account value ($)", min_value=0.0, step=1000.0,
                                   value=float(base.get("net_liquidating_value", 0.0)),
                                   key=k + "nlv")
        per_pos = cols[1].number_input(
            "Max per position (%)", 0.1, 100.0, step=0.5, key=k + "per_pos",
            value=100 * float(base.get("max_collateral_per_position_pct", 0.04)))
        per_tick = cols[2].number_input(
            "Max per ticker (%)", 0.1, 100.0, step=0.5, key=k + "per_tick",
            value=100 * float(base.get("max_collateral_per_ticker_pct", 0.06)))
        cols = st.columns(3)
        per_sector = cols[0].number_input(
            "Max per sector (%)", 0.1, 100.0, step=1.0, key=k + "per_sector",
            value=100 * float(base.get("max_sector_collateral_pct", 0.25)))
        buffer = cols[1].number_input("Cash buffer (%)", 0.0, 90.0, step=1.0, key=k + "buffer",
                                      value=100 * float(base.get("cash_buffer_pct", 0.08)))
        max_pos = cols[2].number_input("Max open positions", 1, 500, step=1, key=k + "max_pos",
                                       value=int(base.get("max_open_positions", 25)))
        cols = st.columns(3)
        strategies = cols[0].multiselect("Allowed strategies", list(us.STRATEGY_CHOICES),
                                         default=base.get("allowed_strategies")
                                         or list(us.STRATEGY_CHOICES), key=k + "strategies")
        cash_secured = cols[1].checkbox("Cash-secured puts only", key=k + "cash_secured",
                                        value=bool(base.get("require_cash_secured", True)))
        spreads = cols[2].checkbox("Spreads approved", key=k + "spreads",
                                   value=bool(base.get("spread_approval", True)))
        cols = st.columns(3)
        types = ["research", "taxable", "margin", "roth_ira", "traditional_ira"]
        current_type = str(base.get("account_type") or "research")
        if current_type not in types:
            types.append(current_type)
        account_type = cols[0].selectbox("Account type", types, index=types.index(current_type),
                                         key=k + "account_type",
                                         help="Naked strategies need a margin account.")
        naked = cols[1].checkbox("Naked options approved", key=k + "naked",
                                 value=bool(base.get("naked_approval", False)),
                                 help="Phase 16: strangles and other naked specs. Needs a "
                                      "margin account; never allowed in an IRA.")
        research_only = cols[2].checkbox(
            "Naked strategies research-only", key=k + "research_only",
            value=bool(base.get("naked_research_only", False)),
            help="Phase 17: strangles appear for comparison but are never auto-tracked.")
        placeholder = st.checkbox(
            "These are placeholder values", key=k + "placeholder",
            value=bool(base.get("placeholder", False)),
            help="Untick once the account value and limits are real; the Screener warns "
                 "while it is set, and the scheduler refuses auto runs on it.")
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
                "spread_approval": spreads, "account_type": account_type,
                "naked_approval": naked, "naked_research_only": research_only,
                "placeholder": placeholder})
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

# --- Schedule (Phase 19) -------------------------------------------------------------

with tab_schedule:
    import datetime as dt

    from analytics.scan_request import missing_fields
    from pipeline import scheduler

    info = scheduler.status()
    beat = info["heartbeat"] or {}
    cols = st.columns([1.2, 1.2, 1, 1])
    if info["running"]:
        age = ""
        stamp = scheduler._parse(beat.get("at"))
        if stamp:
            age = f", heartbeat {(dt.datetime.now(stamp.tzinfo) - stamp).total_seconds():.0f}s ago"
        cols[0].success(f"Worker running (pid {info['pid']}{age})")
        cols[1].caption(f"State: {beat.get('state', '?')}")
        if cols[3].button("Stop worker", help="The worker finishes its current job first."):
            scheduler.request_stop()
            st.rerun()
    else:
        cols[0].warning("Worker not running")
        cols[1].caption("`python launch.py` starts it with the UI; so does the Task "
                        "Scheduler entry at logon.")
        if cols[3].button("Start worker", type="primary"):
            pid = scheduler.start_worker()
            st.success(f"Started (pid {pid}).")
            st.rerun()
    if not info["enabled"]:
        st.info("The schedule is disabled: the worker runs but plans no slots.")
    for entry in info["failures_today"]:
        st.error(f"{entry['job']}{' (' + entry['preset'] + ')' if entry.get('preset') else ''} "
                 f"{entry['status']}: {entry.get('message', '')}")

    st.markdown("**Today**")
    done = {h["key"]: h for h in info["today"]}
    plan_rows = [{"time (ET)": slot.when.strftime("%H:%M"), "job": slot.job,
                  "preset": slot.preset or "", "status": done.get(slot.key, {}).get("status",
                                                                                    "pending"),
                  "message": done.get(slot.key, {}).get("message", ""),
                  "run": done.get(slot.key, {}).get("run_id") or ""}
                 for slot in info["plan"]]
    if plan_rows:
        st.dataframe(pd.DataFrame(plan_rows), hide_index=True, width="stretch",
                     column_config={"message": st.column_config.TextColumn(width="large")})
    else:
        st.caption("No slots today (market closed, or the schedule is disabled).")
    upcoming = beat.get("next") or []
    if upcoming:
        st.caption("Next: " + "; ".join(n["label"] + f" ({n['when'][:10]})" for n in upcoming))

    with st.expander("Job history (last 7 days)"):
        recent = scheduler.history(since=dt.date.today() - dt.timedelta(days=7))
        if recent:
            frame = pd.DataFrame(recent[::-1])
            keep = [c for c in ("planned", "job", "preset", "status", "started", "finished",
                                "message", "run_id") if c in frame]
            st.dataframe(frame[keep], hide_index=True, width="stretch",
                         column_config={"message": st.column_config.TextColumn(width="large")})
        else:
            st.caption("No jobs recorded yet.")

    cfg = scheduler.settings()
    st.markdown("**Timetable** (ET, trading days; half days stop at the close)")
    with st.form("schedule_form"):
        c = st.columns(4)
        enabled = c[0].checkbox("Schedule enabled", value=bool(cfg.get("enabled", True)))
        grace = c[1].number_input("Grace for missed slots (min)", 0, 240,
                                  value=int(cfg.get("grace_minutes", 30)))
        c = st.columns(4)
        mark_start = c[0].text_input("Marks from", cfg["mark"]["start"])
        mark_end = c[1].text_input("Marks until", cfg["mark"]["end"])
        every = c[2].number_input("Every (min)", 5, 240, value=int(cfg["mark"].get("every_minutes", 60)))
        c = st.columns(4)
        log_time = c[0].text_input("Auto-log (scan_and_log)", cfg["scan_and_log"]["time"],
                                   help="Once a day per auto preset.")
        archive_time = c[1].text_input("Chain archive", cfg["archive"]["time"])
        nightly_time = c[2].text_input("Nightly data refresh", cfg["nightly"]["time"])
        if st.form_submit_button("Save timetable", type="primary"):
            try:
                us.save_schedule({"enabled": enabled, "grace_minutes": int(grace),
                                  "mark": {"start": mark_start, "end": mark_end,
                                           "every_minutes": int(every)},
                                  "scan_and_log": {"time": log_time},
                                  "archive": {"time": archive_time},
                                  "nightly": {"time": nightly_time}})
                st.success("Saved; the worker picks it up at its next poll.")
                st.rerun()
            except (us.SettingsError, ValueError) as exc:
                st.error(str(exc))

    st.markdown("**Auto presets** -- logged once a day at the auto-log time: the top K, "
                "M random passing rows lower down and M near-miss rejects; never naked or "
                "research-only rows; at most the daily cap of new positions.")
    saved = us.scan_presets()
    autos = scheduler.auto_presets(cfg)
    if not saved:
        st.caption("No saved presets yet: save one on the Screener first.")
    else:
        rows = []
        for name, fields in saved.items():
            profile = fields.get("account_profile", "default")
            pcfg = sizing.account_config(profile)
            missing = missing_fields(fields)
            auto = autos.get(name)
            rows.append({"preset": name, "auto": auto is not None, "profile": profile,
                         "placeholder profile": bool(pcfg.get("placeholder")),
                         "fully explicit": not missing,
                         "K": auto["top_k"] if auto else None,
                         "M": auto["control_m"] if auto else None,
                         "daily cap": auto["daily_cap"] if auto else None,
                         "observe hourly": auto["observe_hourly"] if auto else None})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        blocked = [r["preset"] for r in rows if r["auto"] and r["placeholder profile"]]
        if blocked:
            st.warning(f"{', '.join(blocked)}: the profile holds placeholder values, so each "
                       f"run will be refused (and shown as refused in the history) until the "
                       f"profile's Placeholder box is cleared.")
        with st.form("auto_form"):
            c = st.columns([2, 1, 1, 1, 1, 1])
            name = c[0].selectbox("Preset", list(saved))
            current = autos.get(name) or {}
            tcfg = (sizing.load_config().get("tracking") or {})
            k = c[1].number_input("K (top)", 0, 50, value=int(current.get("top_k",
                                                                          tcfg.get("top_k", 5))))
            m = c[2].number_input("M (control)", 0, 50,
                                  value=int(current.get("control_m", tcfg.get("control_m", 3))))
            cap = c[3].number_input("Daily cap", 0, 100,
                                    value=int(current.get("daily_cap", k + 2 * m)),
                                    help="New positions per day for this preset (K + 2M = "
                                         "one full sample).")
            observe = c[4].checkbox("Observe hourly", value=bool(current.get("observe_hourly")),
                                    help="Also run the preset at the other mark slots, adding "
                                         "observations to already-tracked rows only (a full "
                                         "scan each hour).")
            on = c[5].checkbox("Auto", value=name in autos)
            if st.form_submit_button("Save", type="primary"):
                try:
                    us.set_auto_preset(name, {"top_k": k, "control_m": m, "daily_cap": cap,
                                              "observe_hourly": observe} if on else None)
                    st.success(f"{name}: {'auto' if on else 'not auto'}.")
                    st.rerun()
                except us.SettingsError as exc:
                    st.error(str(exc))

    with st.expander("Start the worker at logon (Windows Task Scheduler)"):
        st.markdown("Run once from `D:\\csp`:\n\n"
                    "```\n.venv\\Scripts\\python scripts\\scheduler_task.py install\n```\n"
                    "`status` shows the entry, `remove` deletes it. The worker refuses to start "
                    "a second copy, so the logon entry and `launch.py` can both be used.")
