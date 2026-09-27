"""
Background job runner for the Scanner page's data-refresh buttons (sync
1-minute data / run a Stage 3 chain scan). A Stage 3 scan takes 60-100
minutes (README.md), so these launch as a detached subprocess with output
tailed from a log file rather than blocking the page behind st.spinner.

Scoped to what this tool needs: a single-user local research app, so tracking
the running subprocess handle in st.session_state (per browser session) is
enough -- no separate job queue/database.
"""
import subprocess
import sys
from pathlib import Path

import streamlit as st


def _proc_key(key: str) -> str:
    return f"_job_proc_{key}"


def _log_key(key: str) -> str:
    return f"_job_log_{key}"


def start_background_script(key: str, script_path: Path, project_root: Path, args=None) -> None:
    log_path = project_root / "data" / ".job_logs" / f"{key}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "w")
    cmd = [sys.executable, str(script_path)] + list(args or [])
    proc = subprocess.Popen(cmd, cwd=str(project_root), stdout=log_file, stderr=subprocess.STDOUT)
    st.session_state[_proc_key(key)] = proc
    st.session_state[_log_key(key)] = str(log_path)


def job_status(key: str, tail_lines: int = 25):
    """Returns (status, log_tail) where status is one of
    'idle' | 'running' | 'done' | 'failed'."""
    proc = st.session_state.get(_proc_key(key))
    log_path = st.session_state.get(_log_key(key))
    tail = ""
    if log_path and Path(log_path).exists():
        lines = Path(log_path).read_text(errors="replace").splitlines()
        tail = "\n".join(lines[-tail_lines:])
    if proc is None:
        return "idle", tail
    ret = proc.poll()
    if ret is None:
        return "running", tail
    return ("done" if ret == 0 else "failed"), tail


def is_running(key: str) -> bool:
    return job_status(key)[0] == "running"
