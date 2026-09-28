"""
The scan request -- what a run is asked to find (Phase 11).

Before Phase 11 a run's question was fixed by config: CSP, 5-10 DTE, the
entry delta band, the whole universe. `ScanRequest` makes the question
explicit and portable:

    strategies            csp / pcs
    dte_min, dte_max      a DTE range, or
    dte_targets           a list of targets, each +/- dte_target_tolerance_days
    risk_mode             delta_range | min_pop | max_loss_per_trade | max_pct_capital
                          (with its value in the field of the same name)
    spread_widths         PCS long-leg widths in dollars
    spread_width_pct      PCS widths as % of spot; when set they replace the
                          dollars (Phase 15 default: 4%)
    pcs_dte_targets       DTE targets for spreads only (Phase 15 default: 45);
                          unset, spreads use the shared DTE window
    profit_targets        % of max profit to report odds for
    account_profile       a key of config.yaml -> account_profiles
    universe              all | csp | stock | etf | index | tag:<tag> | [symbols]
    top_n_underlyings     how many ranked names get a chain pulled, or "all"
    ranking_weights       a weight preset name (config / Settings page) or
                          {component: weight}
    event_policy_overrides  {event type: {action, days_before, ...}} merged
                          over config.yaml -> event_policy
    strike_rule, em_multiple  how the short strike is chosen

It is serialisable to JSON, and every run embeds it in its manifest, so a
result can always be traced back to the question that produced it.

`ScanRequest.default()` reproduces the pre-Phase-11 run: CSP, the
`management.entry` DTE window and delta band. A run without `--request` does
exactly what it did before, apart from pulling chains for the top N only.

WHAT IS APPLIED WHERE (Phase 11)
--------------------------------
Ranking and chain capture read the whole request. CSP construction applies
the DTE window/targets, the delta range, `min_pop` (a gate on the empirical
P(finish OTM)), `max_pct_capital` and `max_loss_per_trade` (contract caps),
the account profile and the event overrides. PCS construction (Phase 12)
also applies the spread widths and the strike rule: `delta` (put delta
nearest the middle of delta_range), `em_multiple` (highest strike at or below
spot - em_multiple x EM), `support` (below the strongest respected support
minus its median pierce depth), or `conservative` (the default: all three,
and the lowest short strike that passes is the default choice).
Profit-target odds arrive in Phase 13.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path

from core.paths import load_config

STRATEGIES = ("csp", "pcs")
RISK_MODES = ("delta_range", "min_pop", "max_loss_per_trade", "max_pct_capital")
STRIKE_RULES = ("conservative", "delta", "em_multiple", "support")
UNIVERSE_KEYWORDS = ("all", "csp", "stock", "etf", "index")


class RequestError(ValueError):
    pass


@dataclass
class ScanRequest:
    strategies: list[str] = field(default_factory=lambda: ["csp"])
    dte_min: int | None = None
    dte_max: int | None = None
    dte_targets: list[int] | None = None
    risk_mode: str = "delta_range"
    delta_range: list[float] | None = None          # put deltas, e.g. [-0.30, -0.12]
    min_pop: float | None = None                    # 0-1
    max_loss_per_trade: float | None = None         # dollars
    max_pct_capital: float | None = None            # 0-1 of the profile's NLV
    spread_widths: list[float] = field(default_factory=lambda: [1, 2.5, 5, 10])
    spread_width_pct: list[float] | None = None     # % of spot; when set, replaces the dollars
    pcs_dte_targets: list[int] | None = None        # spreads' own DTE targets (else the shared window)
    profit_targets: list[int] = field(default_factory=lambda: [25, 30, 50, 100])
    account_profile: str = "default"
    universe: str | list[str] = "all"
    top_n_underlyings: int | str = 15
    ranking_weights: str | dict | None = None
    event_policy_overrides: dict = field(default_factory=dict)
    strike_rule: str = "conservative"
    em_multiple: float = 1.0
    name: str = ""

    # --- Construction ------------------------------------------------------

    @classmethod
    def default(cls, **overrides) -> "ScanRequest":
        """Config defaults, with `management.entry` filling the DTE window and
        delta band when `scan_defaults` leaves them null."""
        cfg = load_config()
        base = dict(cfg.get("scan_defaults") or {})
        base.pop("dte_target_tolerance_days", None)
        base.pop("pcs_dte_tolerance_days", None)
        base.update({k: v for k, v in overrides.items() if v is not None})
        return cls.from_dict(base)

    @classmethod
    def from_dict(cls, data: dict) -> "ScanRequest":
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise RequestError(f"unknown request field(s): {', '.join(unknown)}")
        entry = load_config().get("management", {}).get("entry", {})
        data = dict(data)
        if data.get("dte_targets") in ([], None):
            data["dte_targets"] = None
            if data.get("dte_min") is None:
                data["dte_min"] = entry.get("dte_min", 5)
            if data.get("dte_max") is None:
                data["dte_max"] = entry.get("dte_max", 10)
        if data.get("delta_range") is None:
            data["delta_range"] = list(entry.get("delta_band", [-0.30, -0.12]))
        request = cls(**data)
        request.validate()
        return request

    @classmethod
    def from_json(cls, text: str) -> "ScanRequest":
        return cls.from_dict(json.loads(text))

    @classmethod
    def load(cls, path: str | Path) -> "ScanRequest":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    # --- Validation --------------------------------------------------------

    def validate(self) -> None:
        self.strategies = [s.lower() for s in self.strategies]
        bad = [s for s in self.strategies if s not in STRATEGIES]
        if not self.strategies or bad:
            raise RequestError(f"strategies must be a non-empty subset of {STRATEGIES}")
        if self.dte_targets:
            self.dte_targets = sorted(int(t) for t in self.dte_targets)
            if self.dte_targets[0] < 0:
                raise RequestError("dte_targets must be >= 0")
        else:
            if self.dte_min is None or self.dte_max is None:
                raise RequestError("give dte_min and dte_max, or dte_targets")
            self.dte_min, self.dte_max = int(self.dte_min), int(self.dte_max)
            if not 0 <= self.dte_min <= self.dte_max:
                raise RequestError(f"need 0 <= dte_min <= dte_max, got "
                                   f"{self.dte_min}-{self.dte_max}")
        if self.pcs_dte_targets:
            self.pcs_dte_targets = sorted(int(t) for t in self.pcs_dte_targets)
            if self.pcs_dte_targets[0] < 0:
                raise RequestError("pcs_dte_targets must be >= 0")
        else:
            self.pcs_dte_targets = None
        if self.spread_width_pct:
            self.spread_width_pct = sorted(float(p) for p in self.spread_width_pct)
            if self.spread_width_pct[0] <= 0 or self.spread_width_pct[-1] > 50:
                raise RequestError("spread_width_pct are percents of spot in (0, 50]")
        else:
            self.spread_width_pct = None
        if self.risk_mode not in RISK_MODES:
            raise RequestError(f"risk_mode must be one of {RISK_MODES}")
        if self.delta_range is not None:
            lo, hi = sorted(float(d) for d in self.delta_range)
            if lo > 0 or hi > 0 or lo < -1:
                # Accept positive magnitudes ([0.12, 0.30]) and store put deltas.
                if 0 <= lo <= hi <= 1:
                    lo, hi = -hi, -lo
                else:
                    raise RequestError("delta_range must be put deltas in [-1, 0]")
            self.delta_range = [lo, hi]
        needed = {"min_pop": self.min_pop, "max_loss_per_trade": self.max_loss_per_trade,
                  "max_pct_capital": self.max_pct_capital}
        if self.risk_mode in needed and needed[self.risk_mode] is None:
            raise RequestError(f"risk_mode {self.risk_mode} needs a value for "
                               f"`{self.risk_mode}`")
        if self.min_pop is not None and not 0 < self.min_pop < 1:
            raise RequestError("min_pop is a probability in (0, 1)")
        if self.max_pct_capital is not None and not 0 < self.max_pct_capital <= 1:
            raise RequestError("max_pct_capital is a fraction in (0, 1]")
        if self.max_loss_per_trade is not None and self.max_loss_per_trade <= 0:
            raise RequestError("max_loss_per_trade must be positive dollars")
        if self.strike_rule not in STRIKE_RULES:
            raise RequestError(f"strike_rule must be one of {STRIKE_RULES}")
        if str(self.top_n_underlyings).lower() in ("all", "0"):
            self.top_n_underlyings = "all"
        else:
            try:
                self.top_n_underlyings = int(self.top_n_underlyings)
            except (TypeError, ValueError):
                raise RequestError('top_n_underlyings must be a count or "all"') from None
            if self.top_n_underlyings < 1:
                raise RequestError('top_n_underlyings must be >= 1 (or "all")')
        from core import user_settings
        if self.ranking_weights is None:
            self.ranking_weights = user_settings.default_weight_preset()
        if isinstance(self.ranking_weights, str):
            if self.ranking_weights not in user_settings.weight_presets():
                raise RequestError(
                    f"ranking_weights '{self.ranking_weights}' is not a preset "
                    f"({', '.join(user_settings.weight_presets())})")
        else:
            try:
                self.ranking_weights = user_settings.validate_weights(self.ranking_weights)
            except (user_settings.SettingsError, TypeError, ValueError) as exc:
                raise RequestError(f"ranking_weights: {exc}") from None
        self.spread_widths = sorted(float(w) for w in self.spread_widths)
        self.profit_targets = sorted(int(t) for t in self.profit_targets)
        if isinstance(self.universe, str):
            key = self.universe
            if key not in UNIVERSE_KEYWORDS and not key.startswith("tag:"):
                raise RequestError(f"universe must be one of {UNIVERSE_KEYWORDS}, "
                                   f"tag:<tag>, or a list of symbols")
        else:
            self.universe = [str(s).strip().upper() for s in self.universe if str(s).strip()]
            if not self.universe:
                raise RequestError("universe list is empty")
        profiles = user_settings.profile_names()
        if self.account_profile not in profiles:
            raise RequestError(f"account_profile '{self.account_profile}' is not defined "
                               f"({', '.join(profiles)}); add it on the Settings page")
        for kind, rule in (self.event_policy_overrides or {}).items():
            if not isinstance(rule, dict):
                raise RequestError(f"event_policy_overrides.{kind} must be a mapping")
            if rule.get("action") not in (None, "block", "warn", "ignore"):
                raise RequestError(f"event_policy_overrides.{kind}.action must be "
                                   f"block, warn or ignore")

    # --- Derived windows ---------------------------------------------------

    @property
    def tolerance(self) -> int:
        return int((load_config().get("scan_defaults") or {}).get(
            "dte_target_tolerance_days", 3))

    def _targets_for(self, strategy: str | None) -> list[int] | None:
        """DTE targets that apply to `strategy`: spreads use `pcs_dte_targets`
        when set (Phase 15 default: 45), everything else the shared ones."""
        if strategy == "pcs" and self.pcs_dte_targets:
            return self.pcs_dte_targets
        return self.dte_targets

    @property
    def pcs_tolerance(self) -> int:
        """How far from a spread DTE target an expiration may be. Wider than
        the shared tolerance because many names list only monthlies near 45
        DTE; `nearest_pcs_dtes` then keeps just the closest one."""
        return int((load_config().get("scan_defaults") or {}).get(
            "pcs_dte_tolerance_days", 14))

    def _tolerance_for(self, strategy: str | None) -> int:
        return self.pcs_tolerance if strategy == "pcs" and self.pcs_dte_targets \
            else self.tolerance

    def _window_for(self, strategy: str | None) -> tuple[int, int]:
        targets = self._targets_for(strategy)
        if targets:
            tol = self._tolerance_for(strategy)
            return max(targets[0] - tol, 0), targets[-1] + tol
        return int(self.dte_min), int(self.dte_max)

    def nearest_pcs_dtes(self, dtes) -> set[int]:
        """With `pcs_dte_targets`, the listed DTEs spreads are built at: for
        each target, the one closest to it inside the tolerance. Without
        them, every DTE the spread window accepts."""
        listed = sorted({int(d) for d in dtes if self.accepts_dte(int(d), "pcs")})
        if not self.pcs_dte_targets or not listed:
            return set(listed)
        return {min(listed, key=lambda d: (abs(d - t), d)) for t in self.pcs_dte_targets}

    def dte_window(self, strategy: str | None = None) -> tuple[int, int]:
        """The entry window for one strategy -- [dte_min, dte_max], or the span
        of its targets widened by the tolerance -- or, with no strategy, the
        span covering every requested strategy."""
        if strategy is not None:
            return self._window_for(strategy)
        spans = [self._window_for(s) for s in self.strategies]
        return min(lo for lo, _ in spans), max(hi for _, hi in spans)

    def reference_dte(self, strategy: str | None = None) -> float:
        """One DTE to express expected moves in: the window midpoint, or the
        median target. With no strategy, a spreads-only request uses the
        spreads' targets."""
        if strategy is None and self.strategies == ["pcs"]:
            strategy = "pcs"
        targets = self._targets_for(strategy)
        if targets:
            mid = len(targets) // 2
            return float(targets[mid] if len(targets) % 2
                         else (targets[mid - 1] + targets[mid]) / 2)
        lo, hi = self._window_for(strategy)
        return (lo + hi) / 2.0

    def accepts_dte(self, dte: int, strategy: str | None = None) -> bool:
        """Whether an expiration `dte` days out is in scope for `strategy`
        (with no strategy: for any requested strategy)."""
        if strategy is None:
            return any(self.accepts_dte(dte, s) for s in self.strategies)
        targets = self._targets_for(strategy)
        if targets:
            tol = self._tolerance_for(strategy)
            return any(abs(dte - t) <= tol for t in targets)
        lo, hi = self._window_for(strategy)
        return lo <= dte <= hi

    def pcs_widths(self, spot: float | None) -> list[float]:
        """Spread widths in dollars for an underlying at `spot`: the
        `spread_width_pct` percents of spot when set (Phase 15 default 4%),
        else the dollar `spread_widths`. Snapped to listed strikes later."""
        if self.spread_width_pct and spot:
            return sorted(p / 100.0 * float(spot) for p in self.spread_width_pct)
        return sorted(self.spread_widths)

    def chain_dte_window(self) -> tuple[int, int]:
        """What to download: the entry window plus the roll buffer beyond it,
        so a roll of anything opened now can be ranked from the same pull."""
        buffer = int(load_config().get("chain_capture", {}).get("roll_buffer_days", 14))
        lo, hi = self.dte_window()
        return lo, hi + buffer

    @property
    def top_n(self) -> int | None:
        """The chain-pull count; None = every eligible name."""
        return None if self.top_n_underlyings == "all" else int(self.top_n_underlyings)

    def weights(self) -> dict[str, float]:
        """The ranking weights this request resolves to."""
        from core import user_settings
        if isinstance(self.ranking_weights, dict):
            return dict(self.ranking_weights)
        presets = user_settings.weight_presets()
        name = self.ranking_weights or user_settings.default_weight_preset()
        return {k: float(v) for k, v in presets[name].items()}

    def label(self) -> str:
        if self.name:
            return self.name
        dte = (f"{','.join(map(str, self.dte_targets))} DTE" if self.dte_targets
               else f"{self.dte_min}-{self.dte_max} DTE")
        if "pcs" in self.strategies and self.pcs_dte_targets:
            pcs = f"PCS {','.join(map(str, self.pcs_dte_targets))} DTE"
            dte = pcs if self.strategies == ["pcs"] else f"CSP {dte} + {pcs}"
            return f"{dte}, top {self.top_n_underlyings}"
        return f"{'+'.join(s.upper() for s in self.strategies)} {dte}, top {self.top_n_underlyings}"


def resolve_universe(request: ScanRequest) -> list[str]:
    """The request's universe as active registry symbols, restricted to what
    at least one requested strategy can trade: CSP needs physical settlement,
    PCS runs on anything optionable (indices included)."""
    from data_sources import universe

    frame = universe.load(active_only=True)
    if frame.empty:
        from core.paths import load_universe
        return list(request.universe) if isinstance(request.universe, list) \
            else load_universe(scope="csp")

    if isinstance(request.universe, list):
        known = set(frame["symbol"])
        # Unregistered symbols pass through (treated as physically settled stocks).
        frame = frame[frame["symbol"].isin(request.universe)]
        extra = [s for s in request.universe if s not in known]
    else:
        extra = []
        key = request.universe
        if key in ("stock", "etf", "index"):
            frame = frame[frame["asset_class"] == key]
        elif key == "csp":
            frame = frame[frame["settlement"].fillna("physical") != "cash"]
        elif key.startswith("tag:"):
            tag = key[4:].lower()
            frame = frame[frame["tags"].fillna("").str.lower().str.split(r"[,; ]+")
                          .map(lambda tags: tag in tags)]

    cash = frame["settlement"].fillna("physical") == "cash"
    if "pcs" not in request.strategies:
        frame = frame[~cash]
    return sorted(frame["symbol"].tolist()) + sorted(extra)


def strategies_for(symbol_settlement: str | None, request: ScanRequest) -> list[str]:
    """Which requested strategies apply to a symbol with this settlement."""
    cash = (symbol_settlement or "physical") == "cash"
    return [s for s in request.strategies if not (s == "csp" and cash)]
