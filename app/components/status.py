"""
The session status banner, shared by Command Center and the Screener.

Command Center introduced it (Phase 8): a specific statement of how far the
numbers can be trusted right now -- "marks are Friday's close, 2 days stale"
rather than "market closed". The Screener (Phase 14) is the landing page and
carries the same banner, plus a data-freshness line so a run says up front
what it will download.
"""
from __future__ import annotations

import streamlit as st

BANNER_STYLE = {
    "ok": ("#0B6B62", "#E1EFEC"),
    "info": ("#0B6B62", "#E1EFEC"),
    "warn": ("#8A6410", "#F6EEDC"),
    "error": ("#A6382A", "#F6E5E2"),
}


def banner(severity: str, message: str, title: str = "") -> None:
    fg, bg = BANNER_STYLE.get(severity, BANNER_STYLE["info"])
    st.markdown(
        f"""<div style="border-left:4px solid {fg};background:{bg};
             padding:.85rem 1.1rem;border-radius:4px;margin-bottom:1rem;">
          {'<strong style="color:' + fg + ';">' + title + '</strong><br>' if title else ''}
          <span style="color:#14201E;font-size:.92rem;line-height:1.55;">{message}</span>
        </div>""", unsafe_allow_html=True)


def session_banner():
    """The market-session banner. Returns the calendar classification."""
    from core.market_calendar import classify
    info = classify()
    severity, message = info.banner()
    banner(severity, message,
           "Regular session open" if info.is_open else "Outside regular hours")
    scheduler_alerts()
    return info


def scheduler_alerts() -> None:
    """Phase 19: failed or refused jobs today, and a stopped worker during the
    scheduled day (details on Settings -> Schedule)."""
    try:
        from pipeline import scheduler
        state = scheduler.status()
    except Exception as exc:          # never block the page on a status read
        st.caption(f"Scheduler status unavailable: {exc}")
        return
    problems = [f"{e['job']}{' (' + e['preset'] + ')' if e.get('preset') else ''} "
                f"{e['status']} at {str(e.get('planned', ''))[11:16]}: {e.get('message', '')}"
                for e in state["failures_today"]]
    if state["should_be_running"] and not state["running"]:
        problems.insert(0, "the worker is not running, so today's marks and auto-logs will be "
                           "missed (Settings -> Schedule -> Start worker)")
    if problems:
        import html
        banner("error", "<br>".join(html.escape(p) for p in problems), "Scheduler")


def freshness_summary() -> tuple[str, str, list]:
    """(severity, one-line text, rows) from core.freshness -- which caches a
    full run would refresh."""
    from core import freshness
    try:
        rows = freshness.report()
    except Exception as exc:          # never block the page on a status read
        return "warn", f"freshness check failed: {exc}", []
    stale = [r for r in rows if r.status != freshness.OK]
    if not stale:
        return "ok", "Data current: every cache is inside its freshness window.", rows
    names = ", ".join(f"{r.label} ({r.age})" for r in stale)
    return "warn", f"A full run will refresh: {names}.", rows
