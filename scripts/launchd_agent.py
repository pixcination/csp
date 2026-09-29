"""macOS: a launchd LaunchAgent that starts the scheduler worker at login
(the counterpart of scripts/scheduler_task.py on Windows; docs/MACOS.md).

    .venv/bin/python scripts/launchd_agent.py install     # create (or replace) the agent
    .venv/bin/python scripts/launchd_agent.py install --no-caffeinate
    .venv/bin/python scripts/launchd_agent.py status
    .venv/bin/python scripts/launchd_agent.py remove

The agent runs `.venv/bin/python pipeline/scheduler.py` from the project
folder when you log in (RunAtLoad; no KeepAlive -- the worker exits when
another copy already runs, and launchd would otherwise respawn it every 10 s).
By default it is wrapped in `caffeinate -i`, which keeps the Mac from idle
sleep while the worker runs; closing the lid on battery still sleeps it.
launchd's own output goes to data/scheduler/launchd.log; the jobs log to
data/scheduler/worker.log as on Windows.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABEL = "com.csp.scheduler-worker"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _python() -> Path:
    exe = ROOT / ".venv" / "bin" / "python"
    return exe if exe.exists() else Path(sys.executable)


def plist_bytes(python: Path | str, root: Path | str = ROOT, caffeinate: bool = True) -> bytes:
    """The LaunchAgent property list."""
    root = Path(root)
    argv = [str(python), str(root / "pipeline" / "scheduler.py")]
    if caffeinate:
        argv = ["/usr/bin/caffeinate", "-i"] + argv
    log = str(root / "data" / "scheduler" / "launchd.log")
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": argv,
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": log,
        "StandardErrorPath": log,
    })


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    action = args[0] if args else "status"
    if sys.platform != "darwin":
        print("launchd is macOS-only; on Windows use scripts/scheduler_task.py.")
        return 2
    domain = f"gui/{os.getuid()}"
    path = plist_path()
    if action == "install":
        (ROOT / "data" / "scheduler").mkdir(parents=True, exist_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        _launchctl("bootout", f"{domain}/{LABEL}")            # replace: ignore "not loaded"
        path.write_bytes(plist_bytes(_python(), ROOT, caffeinate="--no-caffeinate" not in args))
        result = _launchctl("bootstrap", domain, str(path))
        print((result.stdout or result.stderr).strip() or f"installed {path}; the worker starts "
                                                             f"now and at every login")
        return result.returncode
    if action == "remove":
        result = _launchctl("bootout", f"{domain}/{LABEL}")
        path.unlink(missing_ok=True)
        print((result.stderr or result.stdout).strip() or f"removed {path}")
        return 0
    if action == "status":
        result = _launchctl("print", f"{domain}/{LABEL}")
        print(result.stdout.strip() if result.returncode == 0 else
              f"not installed ({path} {'exists' if path.exists() else 'absent'})")
        return result.returncode
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
