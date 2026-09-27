"""
Single source of truth for every path the wheel tool touches.

Historically the project resolved paths three different ways: `config.yaml`
keys, `Path(".")` relative to the current working directory, and hardcoded
`D:/...` literals inside individual scripts. That mix is what produced the
duplicate-`.env` incident (see core/env.py) -- two different working
directories, two different files, one rotating credential.

Everything now resolves through this module. The rule is: no other file in
the project constructs a path from a string literal or from the current
working directory.
"""
from __future__ import annotations

import functools
import os
from pathlib import Path

import yaml

# This file lives at <project_root>/core/paths.py, so the root is two levels up.
# Deliberately module-relative, never CWD-relative.
_THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = _THIS_FILE.parent.parent

#: Environment variable that overrides the detected project root. Useful for
#: tests and for running the pipeline from a scheduled task with an odd CWD.
ROOT_ENV_VAR = "WHEEL_PROJECT_ROOT"


def project_root() -> Path:
    """Absolute path to the project root (the folder containing config.yaml)."""
    override = os.getenv(ROOT_ENV_VAR)
    if override:
        return Path(override).expanduser().resolve()
    return PROJECT_ROOT


def config_path() -> Path:
    return project_root() / "config.yaml"


@functools.lru_cache(maxsize=1)
def load_config() -> dict:
    """Parse config.yaml once. Call reload_config() after editing it live."""
    with open(config_path(), "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def reload_config() -> dict:
    load_config.cache_clear()
    return load_config()


# --- Derived directories ---------------------------------------------------
# Each of these is created on demand so a fresh clone works without a setup
# step that has to be remembered.

def _ensure(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def data_dir() -> Path:
    return _ensure(project_root() / "data")


def output_dir() -> Path:
    return _ensure(project_root() / "output")


def logs_dir() -> Path:
    return _ensure(data_dir() / ".job_logs")


def runs_dir() -> Path:
    """One subdirectory per pipeline run: manifest, stage logs, timings."""
    return _ensure(data_dir() / "runs")


def chains_dir() -> Path:
    return _ensure(data_dir() / "chains")


def reference_dir() -> Path:
    """FRED rates, VIX complex, earnings calendar, dividend ex-dates."""
    return _ensure(data_dir() / "reference")


def raw_1m_dir() -> Path:
    return _ensure(data_dir() / "raw_1m")


def config_dir() -> Path:
    """Versioned, hand-maintained inputs: macro calendar, registry snapshot."""
    return _ensure(project_root() / "config")


# --- Databases -------------------------------------------------------------

def db_universe_daily() -> Path:
    return data_dir() / "universe_daily.duckdb"


def db_universe() -> Path:
    """Universe registry, market metrics and the events table (Phase 9)."""
    return data_dir() / "universe.duckdb"


def db_1m_cache() -> Path:
    return data_dir() / "raw_1m_cache.duckdb"


def db_iv_history() -> Path:
    return data_dir() / "iv_history.duckdb"


def db_trade_log() -> Path:
    """The wheel ledger: positions, share lots, cycles. This is user data --
    it is the one file under data/ that must never be deleted to 'rebuild'."""
    return data_dir() / "trade_log.duckdb"


def db_mae() -> Path:
    """Cached empirical move/MAE distributions (analytics/mae.py)."""
    return data_dir() / "move_stats.duckdb"


# --- Path resolution -------------------------------------------------------

def resolve(value: str | Path | None) -> Path | None:
    """Resolve a configured path.

    Relative values resolve against the project root, so `config.yaml` can say
    `vendor/tastytrade` and the whole folder stays movable. Absolute values are
    honoured as-is -- that is how the optional external price archive is
    addressed, and it is the only place an absolute path is legitimate.
    """
    if value in (None, ""):
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else (project_root() / path)


def vendor_dir() -> Path:
    return project_root() / "vendor"


def tastytrade_pipeline_dir() -> Path:
    """The vendored TastyTrade client. Inside the project after consolidation."""
    configured = resolve(load_config().get("tastytrade_pipeline_dir"))
    if configured and (configured / "tastytrade_common.py").exists():
        return configured
    return vendor_dir() / "tastytrade"


# --- The one legitimate external dependency --------------------------------
# The ~1,050-ticker screening pool. Optional by design: everything the app does
# day to day reads data/raw_1m/, and this is needed only to re-screen a new
# universe out of the full pool.

def pricing_data_required() -> bool:
    return bool(load_config().get("pricing_data_required", False))


def pricing_data_root() -> Path | None:
    """The external archive, or None when it is absent and not required."""
    path = resolve(load_config().get("pricing_data_root"))
    if path is None:
        return None
    if path.is_dir():
        return path
    if pricing_data_required():
        raise FileNotFoundError(
            f"pricing_data_root '{path}' not found and pricing_data_required is "
            f"true. Reconnect the archive, or set pricing_data_required: false "
            f"in config.yaml -- the app does not need it for normal operation.")
    return None


def is_portable() -> tuple[bool, list[str]]:
    """Does anything outside the project folder have to exist for this to run?

    Returns (portable, blockers). The external price archive is not a blocker
    when `pricing_data_required` is false -- that is the documented exception.
    """
    blockers: list[str] = []
    root = project_root()

    client = tastytrade_pipeline_dir()
    if not (client / "tastytrade_common.py").exists():
        blockers.append(f"TastyTrade client not vendored (looked in {client})")
    elif root not in client.parents and client != root:
        blockers.append(f"TastyTrade client resolves outside the project: {client}")

    env_file = (root / ".env")
    if not env_file.exists():
        blockers.append(f"no .env at {env_file}")

    if pricing_data_required():
        archive = resolve(load_config().get("pricing_data_root"))
        blockers.append(
            f"pricing_data_required is true, pinning the project to {archive}. "
            f"Set it false unless you are about to re-screen the universe.")

    return (not blockers), blockers


def final_universe_file() -> Path:
    return output_dir() / "final_universe.txt"


def load_universe(scope: str = "csp") -> list[str]:
    """Active symbols from the universe registry (Phase 9).

    scope="csp" (default): what the cash-secured-put engine can trade --
      active and physically settled (no cash-settled indices). Every
      pre-Phase-9 caller meant this, so the default preserves them.
    scope="all": every active symbol, indices included -- for the data
      stages (daily bars, market metrics, events).

    Falls back to `output/final_universe.txt` when the registry has not been
    built, so a fresh clone still runs.
    """
    try:
        from data_sources import universe as registry
        symbols = registry.symbols(scope=scope)
        if symbols:
            return symbols
    except Exception:
        pass
    return load_universe_file()


def load_universe_file() -> list[str]:
    """`output/final_universe.txt`: one ticker per line, '#' comments ignored.
    Now an import format for the registry rather than the source of truth."""
    path = final_universe_file()
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line.upper())
    return out
