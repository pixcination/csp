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
    _write(data)
    return True
