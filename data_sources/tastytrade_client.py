"""
The one supported way into the TastyTrade client.

Everything that talks to TastyTrade goes through here rather than importing
`tastytrade_common` directly, for one reason: the vendored client resolves
`.env` against the current working directory and rewrites the rotating OAuth
refresh token there. `core.env.bind_tastytrade_client()` repoints it at the
project's single authoritative file, and this module guarantees that call
happens before any code path that can trigger a refresh.

Import the module yourself and you skip the binding, and the live token
starts going wherever your shell happened to be standing. That is finding
F-01, and it is why this indirection exists.

Resolution order for the vendored code:
  1. `vendor/tastytrade/` inside the project (created by scripts/consolidate.py)
  2. `config.yaml -> tastytrade_pipeline_dir`, for a pre-consolidation checkout
"""
from __future__ import annotations

import functools
import sys
import threading
from pathlib import Path

from core import env
from core.paths import load_config, project_root

_IMPORT_LOCK = threading.Lock()
_BOUND = False


class TastytradeUnavailable(RuntimeError):
    """The vendored client could not be located or imported."""


def _candidate_dirs() -> list[Path]:
    out = [project_root() / "vendor" / "tastytrade"]
    try:
        configured = load_config().get("tastytrade_pipeline_dir")
    except Exception:
        configured = None
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = project_root() / path
        if path not in out:
            out.append(path)
    return out


@functools.lru_cache(maxsize=1)
def _load() -> tuple:
    """Import the vendored client and bind it to the canonical .env.

    Returns (tastytrade_common_module, pull_equity_callable).
    """
    global _BOUND
    with _IMPORT_LOCK:
        last_error: Exception | None = None
        for directory in _candidate_dirs():
            if not (directory / "tastytrade_common.py").exists():
                continue
            path_str = str(directory)
            if path_str not in sys.path:
                sys.path.insert(0, path_str)
            try:
                import tastytrade_common as ttc
            except Exception as exc:
                last_error = exc
                continue

            # THE CRITICAL LINE. Must run before any get_access_token() call,
            # or a token rotation writes to the wrong file.
            env.bind_tastytrade_client(ttc)
            _BOUND = True

            try:
                from snapshot_loop import pull_equity
            except Exception:
                pull_equity = None  # optional; chains.py has its own capture
            return ttc, pull_equity

        searched = "\n  ".join(str(d) for d in _candidate_dirs())
        raise TastytradeUnavailable(
            "Could not import tastytrade_common.py. Searched:\n  " + searched
            + "\n\nRun: python scripts/consolidate.py"
            + (f"\n\nLast import error: {type(last_error).__name__}: {last_error}"
               if last_error else "")
        )


def common():
    """The bound `tastytrade_common` module."""
    return _load()[0]


def pull_equity_fn():
    """`snapshot_loop.pull_equity`, or None if the loop was not vendored."""
    return _load()[1]


def is_bound() -> bool:
    return _BOUND


# --- Session helpers -------------------------------------------------------

def authenticate(force: bool = False) -> str:
    """Fetch an access token, persisting any rotated refresh token correctly.

    The vendored client writes rotations through its own `_persist_refresh_token`,
    which now targets the canonical file because of the binding above. This
    wrapper additionally mirrors the current value into `os.environ` so a
    long-running Streamlit process does not hold a stale one in memory.
    """
    ttc = common()
    token = ttc.get_access_token(force=force)
    current = getattr(ttc, "_REFRESH_TOKEN_CURRENT", None)
    if current and current != env.get("REFRESH_TOKEN"):
        env.persist_refresh_token(current)
    return token


def check_connection() -> tuple[bool, str]:
    """Cheap liveness probe for the preflight and the Command Center banner.

    Never raises -- returns (ok, message), because a failed credential check
    should render as a readable banner, not a stack trace on page load.
    """
    try:
        authenticate(force=True)
    except TastytradeUnavailable as exc:
        return False, str(exc).splitlines()[0]
    except Exception as exc:
        message = str(exc)
        if "invalid_grant" in message or "400" in message:
            return False, (
                "OAuth refresh rejected. The stored REFRESH_TOKEN is stale -- most "
                "likely it was rotated into a different .env by an older script. "
                f"Regenerate the credential and put it in {env.env_file()}.")
        if "401" in message or "403" in message:
            return False, ("Authentication rejected. Check CLIENT_SECRET in "
                           f"{env.env_file()}.")
        return False, f"{type(exc).__name__}: {message[:200]}"
    return True, "Authenticated."


def fetch_chain(symbol: str) -> dict:
    return common().fetch_equity_chain(symbol)


def select_expirations(expirations: list[dict], tokens) -> list[str]:
    return common().select_expirations(expirations, tokens)


def dte_token(dte_min: int, dte_max: int) -> str:
    """Build the client's inclusive DTE-range selection token.

    `between_0_21_dte` captures everything from same-day out to three weeks --
    the entry window plus the roll targets. Keeping the token narrow is what
    holds dxFeed subscription counts down during a multi-symbol scan.
    """
    return f"between_{int(dte_min)}_{int(dte_max)}_dte"
