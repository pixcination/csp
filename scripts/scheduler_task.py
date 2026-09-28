r"""Phase 19: a Windows Task Scheduler entry that starts the scheduler worker at logon.

    .venv\Scripts\python scripts\scheduler_task.py install   # create (or replace) the entry
    .venv\Scripts\python scripts\scheduler_task.py status
    .venv\Scripts\python scripts\scheduler_task.py remove

The entry runs `pythonw pipeline\scheduler.py` (no console window) when you log
on, so marks and auto-logs happen even if the UI is never opened. The worker
refuses to start a second copy, so this and `python launch.py` can coexist.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASK = "CSP Scheduler Worker"


def _python() -> Path:
    exe = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    return exe if exe.exists() else Path(sys.executable)


def main() -> int:
    action = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    if sys.platform != "win32":
        print("Task Scheduler is Windows-only; start `python pipeline/scheduler.py` another way.")
        return 2
    if action == "install":
        command = f'"{_python()}" "{ROOT / "pipeline" / "scheduler.py"}"'
        args = ["schtasks", "/Create", "/F", "/TN", TASK, "/SC", "ONLOGON", "/RL", "LIMITED",
                "/TR", command]
    elif action == "remove":
        args = ["schtasks", "/Delete", "/F", "/TN", TASK]
    elif action == "status":
        args = ["schtasks", "/Query", "/TN", TASK, "/V", "/FO", "LIST"]
    else:
        print(__doc__)
        return 2
    result = subprocess.run(args, capture_output=True, text=True)
    print((result.stdout or result.stderr).strip())
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
