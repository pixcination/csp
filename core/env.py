"""
Canonical credential loading -- the fix for finding F-01.

THE BUG THIS REPLACES
---------------------
`tastytrade_common.py` calls `load_dotenv()` with no argument and sets
`ENV_PATH = Path(".env")`. Both resolve against the *current working
directory*. TastyTrade rotates the OAuth refresh token on every use and the
client writes the new token back to `ENV_PATH` -- so the token landed in
`D:\\csp\\.env` when a script was run from `D:\\csp`, and in
`D:\\tastytrade\\.env` when run from there. The two files drifted apart on
CLIENT_SECRET, REFRESH_TOKEN and FRED_API_KEY, and only one of them held a
live token at any moment.

THE FIX
-------
1. Exactly one `.env` is authoritative: `<project_root>/.env`, overridable
   with the `WHEEL_ENV_FILE` environment variable.
2. `bind_tastytrade_client()` rewrites the vendored client's module-level
   `ENV_PATH` to that same absolute file *before* any token refresh happens,
   so rotation can no longer depend on where you were standing.
3. Nothing else in the project reads `.env` directly.

Secrets are never printed. `describe()` reports presence and length only.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

from core.paths import project_root

ENV_FILE_VAR = "WHEEL_ENV_FILE"

#: Keys the project expects. `required` means the pipeline cannot run without it.
KNOWN_KEYS: dict[str, bool] = {
    "CLIENT_ID": False,       # informational; the refresh-token grant uses the secret
    "CLIENT_SECRET": True,    # TastyTrade OAuth
    "REFRESH_TOKEN": True,    # TastyTrade OAuth (rotates on every use)
    "MASSIVE_API_KEY": True,  # historical 1-minute archive
    "FRED_API_KEY": False,    # Treasury rates; falls back to config's static rate
}

_LOCK = threading.Lock()
_LOADED = False


def env_file() -> Path:
    """The one and only .env this project reads or writes."""
    override = os.getenv(ENV_FILE_VAR)
    if override:
        return Path(override).expanduser().resolve()
    return (project_root() / ".env").resolve()


def load_env(force: bool = False) -> Path:
    """Load the canonical .env into os.environ. Idempotent.

    Real environment variables win over file contents (`override=False`), so
    a scheduled task or CI runner can inject credentials without editing the
    file. Returns the path that was loaded.
    """
    global _LOADED
    path = env_file()
    with _LOCK:
        if _LOADED and not force:
            return path
        try:
            from dotenv import load_dotenv
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "python-dotenv is not installed. Run: pip install -r requirements.txt"
            ) from exc
        if path.exists():
            load_dotenv(path, override=force)
        _LOADED = True
    return path


def get(key: str, default: str | None = None) -> str | None:
    load_env()
    value = os.getenv(key, default)
    if value is not None:
        value = value.strip()
    return value or default


def require(key: str) -> str:
    """Fetch a credential or raise with an actionable message."""
    value = get(key)
    if not value:
        raise MissingCredential(
            f"{key} is not set. Add it to {env_file()} "
            f"(or export it as an environment variable) and re-run."
        )
    return value


class MissingCredential(RuntimeError):
    """Raised when a credential the current operation needs is absent."""


# --- Refresh-token persistence --------------------------------------------

def persist_refresh_token(new_token: str) -> Path:
    """Rewrite REFRESH_TOKEN in the canonical .env, preserving every other
    line (including comments and ordering). Also updates os.environ so the
    running process sees the new value without a reload."""
    path = env_file()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, found = [], False
    for line in lines:
        if line.startswith("REFRESH_TOKEN="):
            out.append(f"REFRESH_TOKEN={new_token}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"REFRESH_TOKEN={new_token}")
    # Write via a temp file + replace so an interrupted write can never leave
    # a truncated .env -- losing the refresh token means re-authorising by hand.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    tmp.replace(path)
    os.environ["REFRESH_TOKEN"] = new_token
    return path


def bind_tastytrade_client(ttc_module) -> None:
    """Point the vendored TastyTrade client at the canonical .env.

    Must be called immediately after importing `tastytrade_common`, before
    any call that can trigger a token refresh. Without this the client keeps
    its CWD-relative `Path(".env")` and rotation drifts again.
    """
    load_env()
    canonical = env_file()
    ttc_module.ENV_PATH = canonical

    # The vendored client's `_default_data_dir()` hardcodes D:\tastytrade on
    # Windows, and `ensure_dirs()` would recreate that folder outside the
    # project. Redirect it inside so the tree stays portable.
    from core.paths import data_dir
    tasty_data = data_dir() / "tastytrade"
    os.environ.setdefault("TASTYTRADE_DATA_DIR", str(tasty_data))
    ttc_module.BASE_SAVE_DIR = tasty_data
    if hasattr(ttc_module, "STREAM_SAVE_DIR"):
        ttc_module.STREAM_SAVE_DIR = tasty_data / "stream"
    # The client caches CLIENT_SECRET / REFRESH_TOKEN at import time from
    # whatever os.environ held then. Re-seed both from the canonical file so
    # a stale import order can't leave it holding the wrong secret.
    ttc_module.CLIENT_SECRET = get("CLIENT_SECRET")
    ttc_module.REFRESH_TOKEN = get("REFRESH_TOKEN")
    if hasattr(ttc_module, "_REFRESH_TOKEN_CURRENT"):
        ttc_module._REFRESH_TOKEN_CURRENT = get("REFRESH_TOKEN")


# --- Diagnostics -----------------------------------------------------------

@dataclass(frozen=True)
class CredentialStatus:
    key: str
    present: bool
    required: bool
    length: int

    @property
    def ok(self) -> bool:
        return self.present or not self.required

    def render(self) -> str:
        mark = "OK  " if self.present else ("MISS" if self.required else "--  ")
        detail = f"set ({self.length} chars)" if self.present else (
            "REQUIRED, not set" if self.required else "optional, not set")
        return f"  [{mark}] {self.key:<16} {detail}"


def describe() -> list[CredentialStatus]:
    """Presence report for every known key. Never exposes a secret value."""
    load_env()
    out = []
    for key, required in KNOWN_KEYS.items():
        raw = os.getenv(key) or ""
        raw = raw.strip()
        out.append(CredentialStatus(key, bool(raw), required, len(raw)))
    return out


def doctor() -> tuple[bool, str]:
    """Human-readable preflight. Returns (all_required_present, report)."""
    path = env_file()
    statuses = describe()
    lines = [f"Credential file: {path}",
             f"           exists: {path.exists()}", ""]
    lines += [s.render() for s in statuses]
    healthy = all(s.ok for s in statuses)
    if not healthy:
        missing = [s.key for s in statuses if not s.ok]
        lines += ["", f"Cannot run: {', '.join(missing)} must be set in {path}."]
    return healthy, "\n".join(lines)


def stray_env_files() -> list[Path]:
    """Find other .env files that used to be read by CWD-relative loading.

    Reported by the preflight as a warning, because their mere existence is
    what caused F-01: run something from that folder and the rotating token
    silently goes there instead.
    """
    from core.paths import load_config
    candidates: list[Path] = []
    try:
        cfg = load_config()
    except Exception:
        return candidates
    for key in ("tastytrade_pipeline_dir", "pricing_data_root"):
        raw = cfg.get(key)
        if not raw:
            continue
        candidate = (Path(raw) / ".env").resolve()
        if candidate.exists() and candidate != env_file():
            candidates.append(candidate)
    return candidates


if __name__ == "__main__":  # pragma: no cover
    healthy, report = doctor()
    print(report)
    for stray in stray_env_files():
        print(f"\nWARNING: a second .env exists at {stray}\n"
              f"         It is no longer read, but delete or rename it so a\n"
              f"         stray run from that folder can't rotate the token there.")
    raise SystemExit(0 if healthy else 1)
