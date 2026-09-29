#!/usr/bin/env bash
# =============================================================================
# One-time environment setup on macOS / Linux (setup.ps1 on Windows).
#
#   cd ~/csp
#   bash setup.sh
#
# Creates the project's own virtual environment, installs the pinned
# dependencies, creates .env from the template if missing, and runs the
# preflight. Nothing here moves data or credentials between machines: read
# docs/MACOS.md (the single-machine rule) first.
# =============================================================================
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root"

echo
echo "=== Wheel engine setup ==="
echo "Project root: $root"
echo

# --- 1. Virtual environment --------------------------------------------------
venv="$root/.venv"
if [ -d "$venv" ]; then
    echo "[skip] .venv already exists"
else
    echo "[1/4] Creating virtual environment..."
    # The Windows install runs Python 3.13; any 3.11+ works.
    py="${PYTHON:-python3}"
    "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
        echo "Python 3.11+ required ($py is $("$py" --version 2>&1)); set PYTHON=/path/to/python3.13"
        exit 1
    }
    "$py" -m venv "$venv"
fi

python="$venv/bin/python"
if [ ! -x "$python" ]; then
    echo "venv creation failed: $python not found"
    exit 1
fi

# --- 2. Dependencies ---------------------------------------------------------
echo "[2/4] Installing dependencies..."
"$python" -m pip install --upgrade pip --quiet
"$python" -m pip install -r "$root/requirements.txt"

# --- 3. Credentials ----------------------------------------------------------
echo "[3/4] Checking credentials..."
if [ ! -f "$root/.env" ]; then
    cp "$root/.env.example" "$root/.env"
    chmod 600 "$root/.env"
    echo "      Created .env from the template -- fill it in before running."
    echo "      The TastyTrade refresh token ROTATES: only one machine may hold it"
    echo "      (docs/MACOS.md, the single-machine rule)."
else
    echo "      .env present."
fi

# --- 4. Preflight ------------------------------------------------------------
echo "[4/4] Running preflight..."
"$python" "$root/scripts/preflight.py" || true

echo
echo "Activate the environment with:"
echo "    source .venv/bin/activate"
echo "Start the app (and the scheduler worker) with:"
echo "    .venv/bin/python launch.py"
echo "Start the worker at every login with:"
echo "    .venv/bin/python scripts/launchd_agent.py install"
echo
