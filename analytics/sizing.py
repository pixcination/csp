"""
Position sizing: capital limits, and -- at portfolio scale -- liquidity limits.

WHY THIS MODULE CHANGED SHAPE
-----------------------------
At $50,000 the binding constraint was cash. A single cash-secured put on a
$300 stock consumed 60% of the account, so half the universe was untradable
and the interesting question was "can I afford this at all."

At $3,000,000 that question answers itself -- every universe name fits inside
a 4% position cap. The interesting question becomes **"can I actually get
filled?"** A position large enough to matter to a $3M portfolio is large
enough to move a weekly option's market, and a fill you cannot get is not a
signal. So size is now capped four ways:

    capital      collateral vs the position and ticker concentration caps
    open interest    never more than a few percent of what exists at the strike
    option volume    never more than a slice of what actually trades in a day
    underlying ADV   never a notional the stock itself cannot absorb

`SizingResult.binding_constraint` names which one decided, so the decision
sheet can say *why* a size is what it is instead of presenting a bare number.
That matters: "capped at 12 contracts by open interest" is a different piece
of information from "capped at 12 by the concentration limit," and only the
first one tells you the trade does not scale.

Collateral accounting stays cash-secured regardless of where the trade is
actually executed. That is deliberately conservative: it is the honest
denominator for return-on-capital, and it never flatters a position by
assuming margin the executing account may not extend.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from core.paths import load_config


@dataclass(frozen=True)
class AccountState:
    net_liquidating_value: float
    cash_available: float
    committed_collateral: float = 0.0
    share_value: float = 0.0
    open_positions: int = 0
    profile: str = "default"
    #: Per-run override of max_collateral_per_position_pct (a scan request's
    #: max_pct_capital risk mode).
    position_pct_override: float | None = None

    @property
    def config(self) -> dict:
        cfg = account_config(self.profile)
        if self.position_pct_override is not None:
            cfg["max_collateral_per_position_pct"] = float(self.position_pct_override)
        return cfg

    @property
    def deployable(self) -> float:
        cfg = self.config
        buffer_pct = cfg.get("cash_buffer_pct", 0.10)
        reserve = self.net_liquidating_value * buffer_pct
        return max(self.cash_available - reserve, 0.0)

    @property
    def utilization(self) -> float:
        if self.net_liquidating_value <= 0:
            return 0.0
        return (self.committed_collateral + self.share_value) / self.net_liquidating_value


def account_config(profile: str | None = None) -> dict:
    """`account:` with the named profile merged over it (roadmap B.7).
    Profiles come from config.yaml -> account_profiles and the user's own
    config/user_settings.yaml (core.user_settings). `default`, or an
    unknown/None profile, is the account block."""
    from core import user_settings
    cfg = load_config()
    base = dict(cfg.get("account", {}) or {})
    try:
        profiles = user_settings.account_profiles()
    except Exception:
        profiles = cfg.get("account_profiles") or {}
    base.update(profiles.get(profile or "default") or {})
    base.setdefault("allowed_strategies", ["csp", "pcs", "covered_call"])
    return base


def account_from_config(profile: str | None = None,
                        position_pct_override: float | None = None) -> AccountState:
    cfg = account_config(profile)
    nlv = float(cfg.get("net_liquidating_value", 0.0))
    return AccountState(net_liquidating_value=nlv, cash_available=nlv,
                        profile=profile or "default",
                        position_pct_override=position_pct_override)


@dataclass(frozen=True)
class SizingResult:
    contracts: int
    collateral: float
    pct_of_account: float
    ok: bool
    reasons: tuple[str, ...] = ()
    binding_constraint: str = ""
    limits: dict = field(default_factory=dict)
    warnings: tuple[str, ...] = ()          # Phase 19: shown on the row, never a rejection

    @property
    def rejected(self) -> bool:
        return not self.ok or self.contracts < 1

    @property
    def scales(self) -> bool:
        """True when capital, not liquidity, is what caps this trade.

        A position limited by open interest does not get bigger by adding
        capital -- it is as large as the market will bear. Knowing which
        trades scale is what tells you whether the strategy has room to grow.
        """
        return self.binding_constraint in ("position_cap", "ticker_cap",
                                            "cash", "position_count")

    def explain(self) -> str:
        if self.rejected:
            return "; ".join(self.reasons) or "no size available"
        label = {
            "open_interest": "open interest at the strike",
            "option_volume": "the day's option volume",
            "underlying_adv": "the underlying's dollar turnover",
            "position_cap": "the per-position collateral cap",
            "ticker_cap": "the per-ticker collateral cap",
            "cash": "deployable cash",
            "position_count": "the open-position limit",
        }.get(self.binding_constraint, self.binding_constraint)
        return f"{self.contracts} contracts, capped by {label}"


def max_contracts_for_strike(strike: float, account: AccountState | None = None,
                              ticker_committed: float = 0.0,
                              open_interest: float | None = None,
                              option_volume: float | None = None,
                              adv_dollars: float | None = None) -> SizingResult:
    """How many puts to sell at `strike`, and which constraint decided.

    Capital limits and liquidity limits are evaluated together; the smallest
    wins and is named in `binding_constraint`. Liquidity arguments are
    optional -- omit them and only the capital limits apply, which is the right
    behaviour when a chain snapshot has not been captured yet.

    Returns 0 contracts with readable reasons rather than raising, so a
    high-scoring name that cannot be traded shows *why* instead of vanishing.
    Since Phase 12 this is `max_contracts_for_position` with one leg and
    collateral = strike x 100 (numbers pinned by tests/test_phase12.py).
    """
    if strike <= 0:
        return SizingResult(0, 0.0, 0.0, False, ("invalid strike",))
    return max_contracts_for_position(
        strike * 100.0, account, ticker_committed,
        legs=[(open_interest, option_volume, "")], adv_dollars=adv_dollars,
        notional_per_contract=strike * 100.0, capital_label="collateral")


def spread_leg_floors() -> dict:
    """`liquidity_limits.spread_legs` (Phase 17): the absolute floors for the
    legs of a multi-leg position, where the percent-of-OI cap already scales
    the requirement with the contract count."""
    liq = load_config().get("liquidity_limits", {}) or {}
    return dict(liq.get("spread_legs") or {})


def max_contracts_for_position(per_contract: float, account: AccountState | None = None,
                               ticker_committed: float = 0.0,
                               legs: list[tuple] | None = None,
                               adv_dollars: float | None = None,
                               notional_per_contract: float | None = None,
                               capital_label: str = "buying power",
                               leg_floors: dict | None = None) -> SizingResult:
    """Contracts for any position (Phase 12).

    `per_contract` is the capital one contract ties up (CSP collateral, or a
    spread's max loss = its buying-power reduction). `legs` holds each leg's
    (open interest, option volume, label): the liquidity caps apply to EVERY
    leg, so the least liquid one binds. `notional_per_contract` is the
    underlying exposure the ADV cap is measured in (short strike x 100).

    `leg_floors` ({min_open_interest, min_option_volume}) replaces the
    single-leg floors (Phase 17, spread legs). The percent-of-OI and
    percent-of-volume caps still apply, so n contracts need OI >= n / 3% --
    the floor scales with the size, and the absolute floor is a minimum.
    A volume floor of 0 turns volume off as a cap and a gate: a 30-60 DTE
    leg often trades nothing by mid-morning and is still fillable. A leg
    that traded under `warn_option_volume` today is then a WARNING on the
    row (Tom, 2026-09-28: OI 100, no volume gate, low volume shown).
    """
    liq = load_config().get("liquidity_limits", {})
    acct = account or account_from_config()
    cfg = acct.config
    reasons: list[str] = []
    limits: dict[str, float] = {}
    legs = legs or [(None, None, "")]
    notional_per_contract = notional_per_contract or per_contract

    if per_contract <= 0:
        return SizingResult(0, 0.0, 0.0, False, ("no capital at risk to size against",))
    nlv = acct.net_liquidating_value

    # --- Capital ----------------------------------------------------------
    by_cash = math.floor(acct.deployable / per_contract)
    limits["cash"] = by_cash
    if by_cash < 1:
        reasons.append(f"needs ${per_contract:,.0f} {capital_label}, "
                       f"only ${acct.deployable:,.0f} deployable")

    pos_cap = nlv * cfg.get("max_collateral_per_position_pct", 0.15)
    by_position = math.floor(pos_cap / per_contract)
    limits["position_cap"] = by_position
    if by_position < 1:
        reasons.append(
            f"one contract is ${per_contract:,.0f} = {per_contract / nlv:.1%} of the "
            f"account, over the {cfg.get('max_collateral_per_position_pct', 0.15):.0%} "
            f"per-position cap" + (f" (max tradable strike ~${pos_cap / 100:,.0f})"
                                      if capital_label == "collateral" else ""))

    ticker_cap = nlv * cfg.get("max_collateral_per_ticker_pct", 0.20)
    by_ticker = math.floor(max(ticker_cap - ticker_committed, 0.0) / per_contract)
    limits["ticker_cap"] = by_ticker
    if by_ticker < 1 and ticker_committed > 0:
        reasons.append(f"already holding ${ticker_committed:,.0f} in this ticker")

    # The position limit is a GATE, not a contract count. Being allowed three
    # more positions does not mean three contracts -- mixing the two units is
    # how a $3M book silently gets sized like a $50k one.
    max_positions = cfg.get("max_open_positions", 8)
    at_position_limit = acct.open_positions >= max_positions
    if at_position_limit:
        reasons.append(f"already at the {max_positions}-position limit")
        limits["position_count"] = 0

    # --- Liquidity (every leg; the thinnest binds) ---------------------------
    # These are what actually bind at portfolio scale. A contract count you
    # cannot fill is not a smaller trade, it is a different trade.
    floors = leg_floors or {}
    min_oi = floors.get("min_open_interest", liq.get("min_open_interest", 250))
    min_volume = floors.get("min_option_volume", liq.get("min_option_volume", 25))
    pct_oi = liq.get("max_pct_of_open_interest", 0.03)
    warn_volume = floors.get("warn_option_volume") if not min_volume else None
    warnings: list[str] = []
    for leg in legs:
        open_interest, option_volume = leg[0], leg[1]
        label = f"{leg[2]}: " if len(leg) > 2 and leg[2] else ""
        if open_interest is not None:
            if open_interest < min_oi:
                reasons.append(
                    f"{label}open interest {open_interest:,.0f} is below the {min_oi:,} floor -- "
                    f"exiting or rolling this strike would move the market against you")
                by_oi = 0
            else:
                by_oi = math.floor(open_interest * pct_oi)
            limits["open_interest"] = min(limits.get("open_interest", by_oi), by_oi)

        if option_volume is not None and warn_volume and option_volume < warn_volume:
            warnings.append(f"{label}low volume: "
                            + (f"{option_volume:,.0f} contracts traded today" if option_volume > 0
                               else "no volume reported today (stale off-hours)")
                            + f" (under {warn_volume:,}; not a gate)")
        if option_volume is not None and min_volume:
            # Off-hours snapshots report zero volume; that is a stale field, not a
            # dead contract, so fall back to open interest rather than rejecting.
            if option_volume <= 0 and open_interest:
                by_volume = math.floor(open_interest * pct_oi)
            elif option_volume < min_volume:
                reasons.append(f"{label}only {option_volume:,.0f} contracts traded today "
                               f"(floor {min_volume})")
                by_volume = 0
            else:
                by_volume = math.floor(option_volume * liq.get("max_pct_of_option_volume", 0.10))
            limits["option_volume"] = min(limits.get("option_volume", by_volume), by_volume)

    if adv_dollars:
        notional_cap = adv_dollars * liq.get("max_pct_of_underlying_adv", 0.005)
        limits["underlying_adv"] = math.floor(notional_cap / notional_per_contract)

    usable = {k: v for k, v in limits.items() if v is not None}
    contracts = max(min(usable.values()), 0) if usable else 0
    binding = min(usable, key=lambda k: usable[k]) if usable and contracts >= 0 else ""

    collateral = contracts * per_contract
    return SizingResult(
        contracts=contracts,
        collateral=collateral,
        pct_of_account=collateral / nlv if nlv else 0.0,
        ok=contracts >= 1,
        reasons=tuple(reasons),
        binding_constraint=binding,
        limits=usable,
        warnings=tuple(warnings),
    )


def max_tradable_strike(account: AccountState | None = None) -> float:
    """Highest strike this account can sell a single put on, after the
    per-position concentration cap. The single most useful number for
    filtering a screen down to what is actually actionable."""
    acct = account or account_from_config()
    cfg = acct.config
    cap_dollars = min(
        acct.net_liquidating_value * cfg.get("max_collateral_per_position_pct", 0.15),
        acct.deployable,
    )
    return cap_dollars / 100.0


def capacity_report(account: AccountState | None = None) -> dict:
    """What the book can hold, in plain numbers, for the Command Center."""
    acct = account or account_from_config()
    cfg = acct.config
    ceiling = max_tradable_strike(acct)
    return {
        "net_liquidating_value": acct.net_liquidating_value,
        "deployable_cash": acct.deployable,
        "cash_buffer": acct.net_liquidating_value * cfg.get("cash_buffer_pct", 0.10),
        "max_tradable_strike": ceiling,
        "max_open_positions": cfg.get("max_open_positions", 8),
        "open_positions": acct.open_positions,
        "committed_collateral": acct.committed_collateral,
        "share_value": acct.share_value,
        "utilization": acct.utilization,
        "typical_position_size": acct.net_liquidating_value
                                 * cfg.get("max_collateral_per_position_pct", 0.15),
    }


# --- Kelly-style sizing (used once the wheel backtest supplies real stats) --

def fractional_kelly_contracts(win_rate: float, avg_win: float, avg_loss: float,
                                collateral_per_contract: float,
                                account: AccountState | None = None,
                                kelly_fraction: float = 0.25) -> int:
    """Quarter-Kelly contract count from empirical wheel statistics.

    `avg_win` and `avg_loss` are per-contract dollar outcomes, both positive.
    Quarter-Kelly is the working default because a short-put payoff has a fat
    left tail, and full Kelly on a fat-tailed edge is a drawdown machine.

    Returns 0 when the edge is non-positive -- which is the correct answer and
    one a fixed-contract-count scheme can never give you.
    """
    acct = account or account_from_config()
    if avg_loss <= 0 or not (0.0 < win_rate < 1.0):
        return 0
    b = avg_win / avg_loss
    edge = (b * win_rate - (1.0 - win_rate)) / b
    if edge <= 0:
        return 0
    target_dollars = acct.deployable * edge * kelly_fraction
    return max(int(target_dollars // collateral_per_contract), 0)


def screen_universe_by_price(rows, price_key: str = "last_price",
                              delta_strike_ratio: float = 0.94,
                              account: AccountState | None = None):
    """Split a candidate list into tradable / untradable on capital grounds.

    `delta_strike_ratio` approximates where a ~20-delta weekly put strike sits
    relative to spot, so the filter reflects the strike you would actually
    sell rather than the share price.
    """
    ceiling = max_tradable_strike(account)
    tradable, blocked = [], []
    for row in rows:
        try:
            price = float(row[price_key])
        except (KeyError, TypeError, ValueError):
            continue
        approx_strike = price * delta_strike_ratio
        target = tradable if approx_strike <= ceiling else blocked
        target.append({**row, "approx_strike": approx_strike,
                        "collateral": approx_strike * 100.0})
    return tradable, blocked
