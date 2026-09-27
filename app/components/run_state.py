"""
Which run a page is showing -- this session's, or the latest on disk.

Every results page (Command Center, Decisions, Wheel, Portfolio) used to read
`st.session_state["last_manifest"]` and show nothing after a browser refresh.
They now call `active_run()`, which prefers the run made in this session and
otherwise loads the newest finished run from `data/runs/`, and
`run_caption()`, which always says which run is on screen and how old it is.
"""
from __future__ import annotations

import datetime as dt

import streamlit as st

from pipeline.results import RunResults, latest_run, load_run


def active_run() -> tuple[object | None, RunResults | None, str]:
    """(manifest, results, source) -- source is "this session" or "disk"."""
    manifest = st.session_state.get("last_manifest")
    if manifest is not None:
        return manifest, load_run(manifest.run_id), "this session"
    results = latest_run()
    if results is not None:
        return results.manifest, results, "disk"
    return None, None, ""


def analyse_stage(manifest) -> dict:
    if manifest is None:
        return {}
    return (getattr(manifest, "stages", None) or {}).get("analyse", {}) or {}


def run_caption(manifest, source: str) -> None:
    if manifest is None:
        return
    finished = getattr(manifest, "finished_at", None)
    age = ""
    try:
        stamp = dt.datetime.fromisoformat(finished)
        hours = (dt.datetime.now() - stamp).total_seconds() / 3600
        age = f"{hours:.0f}h ago" if hours < 48 else f"{hours / 24:.0f}d ago"
        finished = stamp.strftime("%a %Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        pass
    origin = ("run in this session" if source == "this session"
              else "latest run on disk (loaded after a refresh/restart)")
    st.caption(f"Showing run `{manifest.run_id}` · finished {finished}"
               + (f" ({age})" if age else "") + f" · {origin} · "
               f"session block {getattr(manifest, 'session_block', '?')}")
