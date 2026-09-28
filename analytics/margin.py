"""
Buying power per strategy class, and what an account profile may trade
(Phase 16, roadmap C.8 task 4).

Every strategy spec declares a `margin_class`:

    cash_secured   short puts backed by cash: strike x 100 per short put
                   (the CSP rule the book has always used; no credit offset)
    defined_risk   verticals, condors, butterflies, calendars, diagonals:
                   the maximum loss x 100 (a debit trade's max loss is its debit)
    covered        long stock with short calls against it: the stock's cost
                   less the premium, i.e. the position's max loss x 100
    naked          uncovered short options (strangles): the broker formula,
                   approximated below -- margin accounts with naked approval
                   only

Naked requirement, per short option (the standard Reg-T/CBOE broker rule,
per share; tastytrade and most US brokers use it or something stricter):

    max( 20% x underlying - out-of-the-money amount + premium,
         10% x (strike for a put, underlying for a call) + premium )

A strangle is charged the larger side's requirement plus the other side's
premium. Brokers can and do charge more (house rules, concentrated
positions, index vs equity percentages); treat the figure as a floor.

Permissions come from the account profile (`core.user_settings`):
`spread_approval` for multi-leg defined risk, `naked_approval` AND
`account_type: margin` for naked. IRAs cannot hold naked short calls, so
the naked class also refuses any profile whose account type is an IRA.
"""
from __future__ import annotations

from core.paths import load_config

MARGIN_CLASSES = ("cash_secured", "defined_risk", "covered", "naked")


def _cfg() -> dict:
    cfg = dict(load_config().get("margin", {}) or {})
    cfg.setdefault("naked_pct_underlying", 0.20)
    cfg.setdefault("naked_min_pct", 0.10)
    return cfg


def naked_requirement(spot: float, strike: float, premium: float, option_type: str) -> float:
    """Per-share requirement for one uncovered short option."""
    cfg = _cfg()
    if option_type == "put":
        otm = max(spot - strike, 0.0)
        floor_base = strike
    else:
        otm = max(strike - spot, 0.0)
        floor_base = spot
    a = cfg["naked_pct_underlying"] * spot - otm + premium
    b = cfg["naked_min_pct"] * floor_base + premium
    return max(a, b)


def bpr_per_contract(position, margin_class: str, spot: float) -> float:
    """Dollars of buying power one contract of `position` ties up."""
    if margin_class not in MARGIN_CLASSES:
        raise ValueError(f"margin_class must be one of {MARGIN_CLASSES}")
    shorts = [l for l in position.legs if l.side == "short" and not l.is_stock]
    if margin_class == "cash_secured":
        return sum(l.strike * l.qty for l in shorts if l.option_type == "put") * 100.0
    if margin_class in ("defined_risk", "covered"):
        return position.max_loss * 100.0
    # naked: the larger side's requirement plus the other sides' premium
    reqs = []
    for leg in shorts:
        premium = leg.mid if leg.mid is not None else 0.0
        reqs.append((naked_requirement(spot, leg.strike, premium, leg.option_type) * leg.qty,
                     premium * leg.qty))
    if not reqs:
        return position.max_loss * 100.0
    biggest = max(range(len(reqs)), key=lambda i: reqs[i][0])
    others = sum(p for i, (_, p) in enumerate(reqs) if i != biggest)
    return (reqs[biggest][0] + others) * 100.0


def permitted(margin_class: str, profile_cfg: dict) -> tuple[bool, str]:
    """May an account with `profile_cfg` (sizing.account_config) trade this
    class? Returns (allowed, reason when not)."""
    account_type = str(profile_cfg.get("account_type") or "").lower()
    if margin_class == "defined_risk" and not profile_cfg.get("spread_approval", True):
        return False, "the profile has no spread approval"
    if margin_class == "naked":
        if "ira" in account_type:
            return False, "naked short options are not allowed in an IRA"
        if account_type != "margin":
            return False, f"naked options need a margin account (profile is '{account_type or 'unset'}')"
        if not profile_cfg.get("naked_approval", False):
            return False, "the profile has no naked-option approval"
    return True, ""
