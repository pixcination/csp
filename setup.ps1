# =============================================================================
# One-time environment setup.
#
#   cd D:\csp
#   powershell -ExecutionPolicy Bypass -File setup.ps1
#
# Creates an isolated virtual environment so this project stops sharing an
# interpreter with the ORB and TastyTrade projects, installs pinned
# dependencies, and runs the preflight.
# =============================================================================

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "`n=== Wheel engine setup ===" -ForegroundColor Cyan
Write-Host "Project root: $root`n"

# --- 1. Virtual environment --------------------------------------------------
$venv = Join-Path $root ".venv"
if (Test-Path $venv) {
    Write-Host "[skip] .venv already exists" -ForegroundColor DarkGray
} else {
    Write-Host "[1/4] Creating virtual environment..." -ForegroundColor Yellow
    python -m venv $venv
}

$python = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $python)) { throw "venv creation failed: $python not found" }

# --- 2. Dependencies ---------------------------------------------------------
Write-Host "[2/4] Installing dependencies..." -ForegroundColor Yellow
& $python -m pip install --upgrade pip --quiet
& $python -m pip install -r (Join-Path $root "requirements.txt")

# --- 3. Credentials ----------------------------------------------------------
Write-Host "[3/4] Checking credentials..." -ForegroundColor Yellow
$envFile = Join-Path $root ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $root ".env.example") $envFile
    Write-Host "      Created .env from the template -- fill it in before running." -ForegroundColor Red
} else {
    Write-Host "      .env present." -ForegroundColor DarkGray
}

# The old duplicate is the cause of finding F-01. Warn loudly; do not delete
# the user's file for them.
$stray = "D:\tastytrade\.env"
if (Test-Path $stray) {
    Write-Host ""
    Write-Host "      WARNING: a second .env exists at $stray" -ForegroundColor Red
    Write-Host "      It is no longer read, but if you ever run a script from that" -ForegroundColor Red
    Write-Host "      folder the rotating TastyTrade refresh token will be written" -ForegroundColor Red
    Write-Host "      there instead, and this project's credentials will go stale." -ForegroundColor Red
    Write-Host "      Rename it: Rename-Item '$stray' '.env.retired'" -ForegroundColor Red
    Write-Host ""
}

# --- 4. Preflight ------------------------------------------------------------
Write-Host "[4/4] Running preflight..." -ForegroundColor Yellow
& $python (Join-Path $root "scripts\preflight.py")

Write-Host "`nActivate the environment with:" -ForegroundColor Cyan
Write-Host "    .venv\Scripts\activate`n"
