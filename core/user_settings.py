"""
User-defined settings: account profiles and ranking-weight presets.

`config.yaml` holds the shipped defaults. What Tom defines himself lives in
`config/user_settings.yaml`, which the Settings page writes and which is
versioned like `config/universe.csv`. A user entry with the same name as a
shipped one overrides it; a shipped entry cannot be deleted, only
overridden (deleting the override restores the default).

    account_profiles:            # merged over config.yaml -> account_profiles
      roth_ira:
        net_liquidating_value: 250000
        max_collateral_per_position_pct: 0.05
        allowed_strategies: [csp, pcs, covered_call]
        spread_approval: true
    ranking_weight_presets:      # merged over underlying_rank.weight_presets
      my_mix: {iv_rank: 0.3, iv_rv: 0.3, liquidity: 0.2, trend: 0.1,
               support: 0.05, drawdown: 0.05}

    scan_presets:                # Screener requests saved by name (Phase 14)
      weekly_roth: {strategies: [csp], dte_min: 5, dte_max: 10, ...}

A profile overrides keys of `account:`; anything it leaves out is inherited.
"""
from __future__ import annotations

import re

import yaml

from core.paths import config_dir, load_config

FILE = "user_settings.yaml"

#: Editable profile fields: (type, min, max, label). `None` = unbounded.
PROFILE_FIELDS: dict[str, tuple] = {
    "net_liquidating_value": (float, 0.0, None, "Account value ($)"),
    "cash_buffer_pct": (float, 0.0, 0.9, "Cash buffer (fraction)"),
    "max_collateral_per_position_pct": (float, 0.001, 1.0, "Max per position (fraction)"),
    "max_collateral_per_ticker_pct": (float, 0.001, 1.0, "Max per ticker (fraction)"),
    "max_sector_collateral_pct": (float, 0.001, 1.0, "Max per sector (fraction)"),
    "max_open_positions": (int, 1, 500, "Max open positions"),
    "require_cash_secured": (bool, None, None, "Cash-secured puts only"),
    "spread_approval": (bool, None, None, "Spreads approved"),
    "naked_approval": (bool, None, None, "Naked options approved (margin accounts)"),
    "allowed_strategies": (list, None, None, "Allowed strategies"),
    "account_type": (str, None, None, "Account type"),
    # Phase 17: values not yet entered by Tom (the UI warns until cleared), and
    # naked specs shown for comparison only -- never auto-tracked (Phase 19).
    "placeholder": (bool, None, None, "Placeholder values (not yet entered)"),
    "naked_research_only": (bool, None, None, "Naked strategies research-only"),
}
STRATEGY_CHOICES = ("csp", "pcs", "covered_call")
COMPONENTS = ("iv_rank", "iv_rv", "liquidity", "trend", "support", "drawdown")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class SettingsError(ValueError):
    pass


def path():
    return config_dir() / FILE


def load() -> dict:
    p = path()
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    header = ("# User-defined settings, written by the Settings page (core/user_settings.py).\n"
              "# Entries override config.yaml defaults of the same name.\n")
    path().write_text(header + yaml.safe_dump(data, sort_keys=True), encoding="utf-8")


def _check_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not _NAME.match(name):
        raise SettingsError("names are lower-case letters, digits and _, starting "
                            "with a letter (e.g. roth_ira)")
    return name


# --- Account profiles ----------------------------------------------------------

def validate_profile(fields: dict) -> dict:
    """Coerce and range-check a profile's fields; unknown keys are refused."""
    out = {}
    for key, value in (fields or {}).items():
        if key not in PROFILE_FIELDS:
            raise SettingsError(f"unknown profile field '{key}'")
        kind, lo, hi, label = PROFILE_FIELDS[key]
        if value is None:
            continue
        if kind is list:
            values = [str(v).lower() for v in value]
            bad = [v for v in values if v not in STRATEGY_CHOICES]
            if bad or not values:
                raise SettingsError(f"{label}: choose from {', '.join(STRATEGY_CHOICES)}")
            out[key] = values
        elif kind is bool:
            out[key] = bool(value)
        elif kind is str:
            out[key] = str(value)
        else:
            number = kind(value)
            if (lo is not None and number < lo) or (hi is not None and number > hi):
                raise SettingsError(f"{label} must be between {lo} and {hi}")
            out[key] = number
    return out


def shipped_profiles() -> dict:
    return {k: dict(v or {}) for k, v in (load_config().get("account_profiles")
                                          or {"default": {}}).items()}


def account_profiles() -> dict[str, dict]:
    """Every profile name -> its overrides of `account:` (user over shipped)."""
    merged = shipped_profiles()
    merged.setdefault("default", {})
    for name, fields in (load().get("account_profiles") or {}).items():
        merged[name] = {**merged.get(name, {}), **(fields or {})}
    return merged


def profile_names() -> list[str]:
    names = list(account_profiles())
    return ["default"] + sorted(n for n in names if n != "default")


def profile_label(name: str) -> str:
    """'roth_ira -- $100,000, roth_ira (PLACEHOLDER)' for pickers: the account
    value and type are what make a profile choice deliberate."""
    from analytics import sizing
    cfg = sizing.account_config(name)
    nlv = float(cfg.get("net_liquidating_value") or 0.0)
    kind = cfg.get("account_type") or "research"
    tag = " (PLACEHOLDER)" if cfg.get("placeholder") else ""
    return f"{name} -- ${nlv:,.0f}, {kind}{tag}"


def save_account_profile(name: str, fields: dict) -> str:
    name = _check_name(name)
    data = load()
    data.setdefault("account_profiles", {})[name] = validate_profile(fields)
    _write(data)
    return name


def delete_account_profile(name: str) -> bool:
    """Remove a user profile (or a user override of a shipped one)."""
    data = load()
    profiles = data.get("account_profiles") or {}
    if name not in profiles:
        return False
    del profiles[name]
    _write(data)
    return True


# --- Ranking weight presets ------------------------------------------------------

def validate_weights(weights: dict) -> dict[str, float]:
    out = {}
    for key, value in (weights or {}).items():
        if key not in COMPONENTS:
            raise SettingsError(f"unknown ranking component '{key}' "
                                f"(choose from {', '.join(COMPONENTS)})")
        number = float(value)
        if number < 0:
            raise SettingsError(f"weight for {key} must be >= 0")
        out[key] = number
    if sum(out.values()) <= 0:
        raise SettingsError("at least one ranking weight must be positive")
    return {k: out.get(k, 0.0) for k in COMPONENTS}


def weight_presets() -> dict[str, dict[str, float]]:
    rank_cfg = load_config().get("underlying_rank", {}) or {}
    merged = {k: dict(v) for k, v in (rank_cfg.get("weight_presets") or {}).items()}
    if not merged and rank_cfg.get("weights"):
        merged["balanced"] = dict(rank_cfg["weights"])
    for name, weights in (load().get("ranking_weight_presets") or {}).items():
        merged[name] = dict(weights or {})
    return merged


def default_weight_preset() -> str:
    rank_cfg = load_config().get("underlying_rank", {}) or {}
    return rank_cfg.get("default_weight_preset", "balanced")


def save_weight_preset(name: str, weights: dict) -> str:
    name = _check_name(name)
    data = load()
    data.setdefault("ranking_weight_presets", {})[name] = validate_weights(weights)
    _write(data)
    return name


def delete_weight_preset(name: str) -> bool:
    data = load()
    presets = data.get("ranking_weight_presets") or {}
    if name not in presets:
        return False
    del presets[name]
    _write(data)
    return True


# --- Saved scan requests (Phase 14) --------------------------------------------------
# The Screener's input form saves a whole ScanRequest under a name:
#
#     scan_presets:
#       weekly_csp_roth: {strategies: [csp], dte_min: 5, dte_max: 10, ...}
#
# The request files in examples/ are offered alongside as read-only presets.

def scan_presets() -> dict[str, dict]:
    """Saved requests by name (user file), each a ScanRequest dict."""
    return {name: dict(fields or {}) for name, fields in
            (load().get("scan_presets") or {}).items()}


def example_requests() -> dict[str, dict]:
    """The shipped request files in examples/, by file stem."""
    import json

    from core.paths import project_root
    out = {}
    for file in sorted((project_root() / "examples").glob("*.json")):
        try:
            out[file.stem] = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return out


def save_scan_preset(name: str, request: dict) -> str:
    """Validate through ScanRequest before writing; returns the stored name."""
    from analytics.scan_request import RequestError, ScanRequest
    name = _check_name(name)
    try:
        fields = ScanRequest.from_dict(dict(request)).to_dict()
    except RequestError as exc:
        raise SettingsError(str(exc)) from exc
    fields["name"] = fields.get("name") or name
    data = load()
    data.setdefault("scan_presets", {})[name] = fields
    _write(data)
    return name


def delete_scan_preset(name: str) -> bool:
    data = load()
    presets = data.get("scan_presets") or {}
    if name not in presets:
        return False
    del presets[name]
    ((data.get("schedule") or {}).get("auto_presets") or {}).pop(name, None)
    _write(data)
    return True


# --- Schedule (Phase 19) ---------------------------------------------------------------
# Overrides of config.yaml -> schedule, edited on the Settings page:
#
#     schedule:
#       scan_and_log: {time: "10:45"}
#       auto_presets:
#         csp_roth: {top_k: 5, control_m: 3, daily_cap: 11, observe_hourly: false}
#
# A preset can be marked auto only when it spells out every ScanRequest field,
# so a later config change cannot silently change what is being tracked.

_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
SCHEDULE_TIMES = {"mark": ("start", "end"), "scan_and_log": ("time",), "archive": ("time",),
                  "nightly": ("time",)}


def validate_schedule(fields: dict) -> dict:
    """Coerce the schedule's own settings (not the auto presets)."""
    out: dict = {}
    for key, value in (fields or {}).items():
        if key == "enabled":
            out[key] = bool(value)
        elif key in ("grace_minutes", "poll_seconds"):
            number = int(value)
            if number < (5 if key == "poll_seconds" else 0) or number > 240:
                raise SettingsError(f"{key} must be between 5 and 240")
            out[key] = number
        elif key in SCHEDULE_TIMES:
            block = dict(value or {})
            for part in SCHEDULE_TIMES[key]:
                if part in block and not _TIME.match(str(block[part])):
                    raise SettingsError(f"{key}.{part} must be HH:MM (24-hour, ET)")
            if key == "mark" and "every_minutes" in block:
                block["every_minutes"] = int(block["every_minutes"])
                if not 5 <= block["every_minutes"] <= 240:
                    raise SettingsError("mark.every_minutes must be between 5 and 240")
            out[key] = block
        else:
            raise SettingsError(f"unknown schedule setting '{key}'")
    return out


def save_schedule(fields: dict) -> None:
    data = load()
    current = data.get("schedule") or {}
    data["schedule"] = {**current, **validate_schedule(fields)}
    _write(data)


def set_auto_preset(name: str, auto: dict | None) -> None:
    """Mark a saved preset auto ({top_k, control_m, daily_cap, observe_hourly,
    optional time HH:MM for its own log slot})
    or clear it (None). Refused unless the preset is fully explicit."""
    from analytics.scan_request import missing_fields
    data = load()
    schedule = data.setdefault("schedule", {})
    autos = schedule.setdefault("auto_presets", {})
    if auto is None:
        autos.pop(name, None)
        _write(data)
        return
    presets = data.get("scan_presets") or {}
    if name not in presets:
        raise SettingsError(f"no saved preset '{name}'")
    missing = missing_fields(presets[name])
    if missing:
        raise SettingsError(f"'{name}' is not fully explicit (missing {', '.join(missing)}); "
                            f"open it on the Screener and save it again first")
    clean = {}
    for key in ("top_k", "control_m", "daily_cap"):
        if auto.get(key) is not None:
            clean[key] = int(auto[key])
            if not 0 <= clean[key] <= 100:
                raise SettingsError(f"{key} must be between 0 and 100")
    clean["observe_hourly"] = bool(auto.get("observe_hourly", False))
    log_time = auto.get("time", (autos.get(name) or {}).get("time"))   # kept across edits
    if log_time:
        try:
            hour, minute = (int(x) for x in str(log_time).split(":"))
            assert 0 <= hour < 24 and 0 <= minute < 60
        except (ValueError, AssertionError):
            raise SettingsError(f"time must be HH:MM, got {log_time!r}") from None
        clean["time"] = f"{hour:02d}:{minute:02d}"
    autos[name] = clean
    _write(data)
