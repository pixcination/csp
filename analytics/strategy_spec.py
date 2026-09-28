"""
Strategy specs -- a strategy described as data (Phase 16, roadmap C.8 task 1).

One YAML file per strategy under `strategies/`. A spec says which legs to
open, how each strike and expiration is chosen, when the strategy applies,
and how it is managed:

    id: iron_condor
    label: Iron condor
    family: neutral premium          # grouping for display
    margin_class: defined_risk       # cash_secured | defined_risk | covered | naked
    premium: credit                  # credit | debit (Phase 17: which IV extreme excludes it)
    event_policy_as: pcs             # whose config.yaml event_policy it obeys
    expirations:
      front: {dte_target: 45, tolerance: 14}
    legs:
      - {name: short_put,  type: put,  side: short, expiration: front, select: {delta: -0.16}}
      - {name: long_put,   type: put,  side: long,  expiration: front,
         select: {offset_from: short_put, width_pct: 3}}
      ...
    entry:
      iv_regime: [mid, high]         # low | mid | high: preferred, not required (below)
      trend: [range]                 # uptrend | range | downtrend
      earnings: block                # block | allow: earnings inside the trade
      min_credit: 0.10               # credit strategies; max_debit_pct for debits
    exit:
      profit_target_pct: 50
      loss_stop_multiple: 2.0
      time_stop_dte: 21

LEG FIELDS
    type      put | call | stock
    side      short | long
    qty       per contract of the position (ratio), default 1; for stock 1 = 100 shares
    expiration  a key of `expirations` (options only)
    select    exactly one of (strikes are the listed ones, nearest wins):
                delta: -0.20            the option whose chain delta is nearest
                moneyness: -0.05        strike nearest spot x (1 + m)
                em_multiple: 1.0        puts at spot - k x EM, calls at spot + k x EM
                atm: true               strike nearest spot
                offset_from: <leg>, with width | width_pct | width_em
                                        puts below the reference, calls above
                same_strike_as: <leg>   calendars: the reference strike, this expiry

EXPIRATIONS
    Each role picks the listed expiration nearest `dte_target` within
    `tolerance` days; roles are resolved in order and each must be later
    than the one before (so a calendar's back month is after its front).

The recommender (analytics/recommender.py) reads `entry` to decide where a
strategy applies: the condition matrix is simply every spec's conditions
laid out by IV regime and trend.

IV REGIME (Phase 17, review decision 1)
    Measured on the TastyTrade IV PERCENTILE (`recommender.iv_regime.measure`),
    clamped to [0, 1] -- the headline `tos` rank is unbounded (EWZ read 1.11)
    and one spike in its lookback distorts it. A name changes regime only
    once it clears a boundary by `hysteresis` relative to its previous
    snapshot, so 0.24 / 0.26 does not flip strategy families run to run.
    The regime is SOFT: a spec outside its preferred regimes still resolves
    and ranks after the ones inside (`regime_fit`), except at the extremes --
    credit specs are excluded below `credit_min`, debit specs above
    `debit_max`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from core.paths import load_config, strategies_dir

MARGIN_CLASSES = ("cash_secured", "defined_risk", "covered", "naked")
PREMIUMS = ("credit", "debit")
LEG_TYPES = ("put", "call", "stock")
SELECTORS = ("delta", "moneyness", "em_multiple", "atm", "offset_from", "same_strike_as")
IV_REGIMES = ("low", "mid", "high")
TRENDS = ("uptrend", "range", "downtrend")


class SpecError(ValueError):
    pass


@dataclass
class LegSpec:
    name: str
    type: str
    side: str
    qty: int = 1
    expiration: str | None = None
    select: dict = field(default_factory=dict)

    @property
    def selector(self) -> str | None:
        keys = [k for k in self.select if k in SELECTORS]
        return keys[0] if keys else None


@dataclass
class Spec:
    id: str
    label: str
    legs: list[LegSpec]
    expirations: dict[str, dict]
    margin_class: str
    family: str = ""
    description: str = ""
    event_policy_as: str = "pcs"
    premium: str = "credit"
    entry: dict = field(default_factory=dict)
    exit: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    path: str = ""

    @property
    def has_stock(self) -> bool:
        return any(l.type == "stock" for l in self.legs)

    @property
    def multi_expiry(self) -> bool:
        return len({l.expiration for l in self.legs if l.type != "stock"}) > 1

    def leg_text(self) -> str:
        parts = []
        for leg in self.legs:
            if leg.type == "stock":
                parts.append(f"{leg.side} {100 * leg.qty} shares")
                continue
            sel = leg.select
            key = leg.selector
            how = {"delta": lambda: f"{sel['delta']:+.2f}d",
                   "moneyness": lambda: f"{sel['moneyness']:+.0%} OTM",
                   "em_multiple": lambda: f"{sel['em_multiple']:g} EM",
                   "atm": lambda: "ATM",
                   "offset_from": lambda: (f"{sel.get('width_pct'):g}% past {sel['offset_from']}"
                                           if sel.get("width_pct") is not None else
                                           f"{sel.get('width_em'):g} EM past {sel['offset_from']}"
                                           if sel.get("width_em") is not None else
                                           f"${sel.get('width'):g} past {sel['offset_from']}"),
                   "same_strike_as": lambda: f"strike of {sel['same_strike_as']}"}[key]()
            exp = self.expirations.get(leg.expiration, {})
            qty = f"{leg.qty}x " if leg.qty != 1 else ""
            parts.append(f"{leg.side} {qty}{leg.type} {how} @{exp.get('dte_target')}DTE")
        return "; ".join(parts)

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "family": self.family,
                "margin_class": self.margin_class, "premium": self.premium,
                "legs": self.leg_text(),
                "iv_regime": ", ".join(self.entry.get("iv_regime", IV_REGIMES)),
                "trend": ", ".join(self.entry.get("trend", TRENDS)),
                "earnings": self.entry.get("earnings", "block"),
                "exit": ", ".join(f"{k}={v}" for k, v in self.exit.items()),
                "model_risk": "calendar/diagonal: back leg valued by BS at its IV"
                if self.multi_expiry else ""}


def _validate(data: dict, path: str = "") -> Spec:
    where = f"{path}: " if path else ""
    for key in ("id", "label", "legs", "margin_class"):
        if key not in data:
            raise SpecError(f"{where}missing '{key}'")
    if data["margin_class"] not in MARGIN_CLASSES:
        raise SpecError(f"{where}margin_class must be one of {MARGIN_CLASSES}")
    if data.get("premium", "credit") not in PREMIUMS:
        raise SpecError(f"{where}premium must be one of {PREMIUMS}")
    expirations = dict(data.get("expirations") or {})
    for role, exp in expirations.items():
        if "dte_target" not in (exp or {}):
            raise SpecError(f"{where}expiration '{role}' needs dte_target")
        exp.setdefault("tolerance", 14)
    legs, names = [], set()
    for raw in data["legs"]:
        leg = LegSpec(name=raw.get("name", ""), type=raw.get("type", ""),
                      side=raw.get("side", ""), qty=int(raw.get("qty", 1)),
                      expiration=raw.get("expiration"), select=dict(raw.get("select") or {}))
        if not leg.name or leg.name in names:
            raise SpecError(f"{where}every leg needs a unique name")
        if leg.type not in LEG_TYPES or leg.side not in ("short", "long") or leg.qty < 1:
            raise SpecError(f"{where}leg {leg.name}: type put|call|stock, side short|long, qty >= 1")
        if leg.type != "stock":
            if leg.expiration not in expirations:
                raise SpecError(f"{where}leg {leg.name}: expiration '{leg.expiration}' "
                                f"is not one of {list(expirations)}")
            if len([k for k in leg.select if k in SELECTORS]) != 1:
                raise SpecError(f"{where}leg {leg.name}: exactly one selector of {SELECTORS}")
            ref = leg.select.get("offset_from") or leg.select.get("same_strike_as")
            if ref is not None and ref not in names:
                raise SpecError(f"{where}leg {leg.name}: refers to '{ref}', which must be "
                                f"an earlier leg")
            if leg.selector == "offset_from" and not any(
                    k in leg.select for k in ("width", "width_pct", "width_em")):
                raise SpecError(f"{where}leg {leg.name}: offset_from needs width, width_pct "
                                f"or width_em")
        names.add(leg.name)
        legs.append(leg)
    if not any(l.type != "stock" for l in legs):
        raise SpecError(f"{where}a spec needs at least one option leg")
    entry = dict(data.get("entry") or {})
    for key, allowed in (("iv_regime", IV_REGIMES), ("trend", TRENDS)):
        values = entry.get(key)
        if values is not None and (not values or any(v not in allowed for v in values)):
            raise SpecError(f"{where}entry.{key} must be a list from {allowed}")
    if entry.get("earnings", "block") not in ("block", "allow"):
        raise SpecError(f"{where}entry.earnings must be block or allow")
    return Spec(id=str(data["id"]), label=str(data["label"]), legs=legs,
                expirations=expirations, margin_class=data["margin_class"],
                family=str(data.get("family", "")), description=str(data.get("description", "")),
                event_policy_as=str(data.get("event_policy_as", "pcs")),
                premium=str(data.get("premium", "credit")), entry=entry,
                exit=dict(data.get("exit") or {}), notes=list(data.get("notes") or []),
                path=path)


def from_dict(data: dict) -> Spec:
    return _validate(data)


def load(path: str | Path) -> Spec:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _validate(data, path.name)


def load_all(folder: str | Path | None = None) -> dict[str, Spec]:
    """Every spec under `strategies/`, by id. A duplicate id is an error."""
    folder = Path(folder) if folder else strategies_dir()
    out: dict[str, Spec] = {}
    for path in sorted(folder.glob("*.yaml")):
        spec = load(path)
        if spec.id in out:
            raise SpecError(f"duplicate spec id '{spec.id}' ({path.name})")
        out[spec.id] = spec
    return out


# --- Conditions ------------------------------------------------------------------------

def regime_config() -> dict:
    cfg = (load_config().get("recommender", {}) or {}).get("iv_regime", {}) or {}
    return {"measure": str(cfg.get("measure", "ivp")),
            "low_below": float(cfg.get("low_below", 0.25)),
            "high_above": float(cfg.get("high_above", 0.50)),
            "hysteresis": float(cfg.get("hysteresis", 0.03) or 0.0),
            "soft": bool(cfg.get("soft", True)),
            "credit_min": cfg.get("credit_min", 0.10),
            "debit_max": cfg.get("debit_max", 0.90)}


def clamp_rank(value) -> float | None:
    """An IV rank or percentile bounded to [0, 1]; None when missing."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value:
        return None
    return min(max(value, 0.0), 1.0)


def regime_value(metrics: dict | None, cfg: dict | None = None) -> tuple[float | None, str]:
    """(clamped value, measure used): the configured measure (IV percentile),
    falling back to the headline IV rank when it is missing."""
    cfg = cfg or regime_config()
    metrics = metrics or {}
    for measure in dict.fromkeys([cfg["measure"], "ivp", "ivr"]):
        value = clamp_rank(metrics.get(measure))
        if value is not None:
            return value, measure
    return None, cfg["measure"]


def iv_regime(value: float | None, previous: str | None = None,
              cfg: dict | None = None) -> str | None:
    """low / mid / high from a clamped IV percentile (or rank). Given the
    `previous` regime, a boundary must be cleared by `hysteresis` before the
    regime changes."""
    value = clamp_rank(value)
    if value is None:
        return None
    cfg = cfg or regime_config()
    low, high, band = cfg["low_below"], cfg["high_above"], cfg["hysteresis"]
    plain = "low" if value < low else "high" if value > high else "mid"
    if previous not in IV_REGIMES or previous == plain or not band:
        return plain
    if previous == "low" and value < low + band:
        return "low"
    if previous == "high" and value > high - band:
        return "high"
    if previous == "mid" and low - band < value < high + band:
        return "mid"
    return plain


def regime_fit(spec: Spec, regime: str | None) -> bool | None:
    """Is `regime` one the spec prefers? None when unknown or unconstrained."""
    regimes = spec.entry.get("iv_regime")
    if not regimes or regime is None:
        return None
    return regime in regimes


def applies(spec: Spec, conditions: dict, soft_regime: bool | None = None
            ) -> tuple[bool, list[str]]:
    """Do a ticker's conditions ({iv_regime, iv_value, trend,
    earnings_in_window}) meet the spec's entry conditions? Unknown conditions
    do not block; they are reported. With a soft regime (the default,
    `recommender.iv_regime.soft`) a regime outside the spec's preference is
    reported, not blocking -- only the IV extremes exclude."""
    cfg = regime_config()
    soft = cfg["soft"] if soft_regime is None else soft_regime
    why = []
    ok = True
    regimes = spec.entry.get("iv_regime")
    regime = conditions.get("iv_regime")
    value = clamp_rank(conditions.get("iv_value"))
    if regimes and regime is not None and regime not in regimes:
        if soft:
            why.append(f"IV regime {regime} (prefers {'/'.join(regimes)}; soft)")
        else:
            ok = False
            why.append(f"IV regime {regime} (wants {'/'.join(regimes)})")
    elif regimes and regime is None:
        why.append("IV regime unknown")
    if regimes and value is not None:
        lo, hi = cfg["credit_min"], cfg["debit_max"]
        if spec.premium == "credit" and lo is not None and value < float(lo):
            ok = False
            why.append(f"IV percentile {value:.2f} is under {float(lo):.2f}: too cheap to sell")
        if spec.premium == "debit" and hi is not None and value > float(hi):
            ok = False
            why.append(f"IV percentile {value:.2f} is over {float(hi):.2f}: too rich to buy")
    trends = spec.entry.get("trend")
    trend = conditions.get("trend")
    if trends and trend is not None and trend not in trends:
        ok = False
        why.append(f"trend {trend} (wants {'/'.join(trends)})")
    elif trends and trend is None:
        why.append("trend unknown")
    if spec.entry.get("earnings", "block") == "block" and conditions.get("earnings_in_window"):
        ok = False
        why.append("earnings inside the trade")
    return ok, why


def condition_matrix(specs: dict[str, Spec]) -> "pd.DataFrame":
    """IV regime x trend -> the specs that apply (earnings aside): the
    recommender's condition matrix, derived from the specs themselves."""
    import pandas as pd
    rows = []
    for regime in IV_REGIMES:
        row = {"iv_regime": regime}
        for trend in TRENDS:
            # The matrix shows PREFERENCES, so the regime applies strictly here.
            ids = [s.id for s in specs.values()
                   if applies(s, {"iv_regime": regime, "trend": trend},
                              soft_regime=False)[0]]
            row[trend] = ", ".join(ids)
        rows.append(row)
    return pd.DataFrame(rows)
