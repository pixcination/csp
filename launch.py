"""
Single-command entry point for the CSP Wheel Analyzer.

Starts the scheduler worker (Phase 19, pipeline/scheduler.py) unless one is
already running, then `streamlit run app/main.py`. The worker is detached: it
keeps marking and logging on schedule after the UI closes (stop it from
Settings -> Schedule). `--no-worker` starts the UI alone.

Usage (from D:\\csp):
    python launch.py
    python launch.py --no-worker
"""
import subprocess
import sys
from pathlib import Path


def main():
    project_root = Path(__file__).resolve().parent
    app_entry = project_root / "app" / "main.py"

    if "--no-worker" not in sys.argv:
        sys.path.insert(0, str(project_root))
        try:
            from pipeline import scheduler
            if scheduler.settings().get("enabled", True):
                pid = scheduler.start_worker()
                print(f"Scheduler worker running (pid {pid}); see Settings -> Schedule.")
        except Exception as exc:          # the UI must start regardless
            print(f"Scheduler worker not started: {exc}")

    cmd = [sys.executable, "-m", "streamlit", "run", str(app_entry)]
    print(f"Starting CSP Wheel Analyzer -- {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(project_root))


if __name__ == "__main__":
    main()
