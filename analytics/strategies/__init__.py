"""
The strategy package (Phase 12, roadmap B.6).

    base.py     Leg and Position: payoff, value, max profit/loss, breakevens,
                buying power, net Greeks
    csp.py      cash-secured put (ported from candidates.py, numbers pinned)
    pcs.py      put credit spread: strike rules, width ladder, risk tiers
    context.py  per-ticker context every row carries (EM, support, flags)

`STRATEGIES` stays importable from here for the older callers.

Originally: a small strategy-definition layer -- avoids hardcoding "put"/"cash-secured"
throughout the Scanner presets and Trade Log, per docs/PROJECT_SPEC.md's "Look,
feel, and extensibility" (room to add strategies later without a rebuild,
without over-building a plugin framework for strategies that don't exist
yet). Just CSP + covered call today.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyDef:
    id: str
    label: str
    option_type: str  # "put" | "call"


STRATEGIES: dict[str, StrategyDef] = {
    "csp": StrategyDef(id="csp", label="Cash-Secured Put", option_type="put"),
    "covered_call": StrategyDef(id="covered_call", label="Covered Call", option_type="call"),
    "pcs": StrategyDef(id="pcs", label="Put Credit Spread", option_type="put"),
}
