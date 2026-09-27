"""
Single-command entry point for the CSP Wheel Analyzer.

Thin wrapper around `streamlit run app/main.py` -- kept separate from the
Streamlit app itself so the user-facing command stays `python launch.py`
regardless of how the internals evolve (e.g. if a persistent background
process for scheduled chain scans gets added later; see docs/PROJECT_SPEC.md
"Look, feel, and extensibility" -- no separate backend process is needed
yet, so this doesn't speculatively build one).

Usage (from D:\\csp):
    python launch.py
"""
import subprocess
import sys
from pathlib import Path


def main():
    project_root = Path(__file__).resolve().parent
    app_entry = project_root / "app" / "main.py"

    cmd = [sys.executable, "-m", "streamlit", "run", str(app_entry)]
    print(f"Starting CSP Wheel Analyzer -- {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(project_root))


if __name__ == "__main__":
    main()
