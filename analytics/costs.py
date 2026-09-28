"""
Transaction-cost model -- the fix for finding F-06 (net-of-fill yield).

Every premium figure the old tool displayed was a mid-market mark with zero
costs applied, while the trade log charged commission and the backtest charged
nothing. Three different accountings for the same trade. This module is the
single place costs are computed, so the scanner, the roll engine, the backtest
and the ledger all agree.

Rates are tastytrade's published equity-option schedule (retail, as of the
2026-07-30 revision) and live in `config.yaml` under `costs:` so they can be
corrected without a code change.

WHY THIS MATTERS MORE AT 7 DTE THAN AT 45 DTE
---------------------------------------------
A 7-DTE 25-delta put on a $50 stock might collect $0.22. Round-trip cost to
open and let expire is $1.12 on $22 of premium -- 5.1% of gross. Add a
half-penny of slippage on the fill and you are near 8%. The same $1.12 against
a 45-DTE trade collecting $1.10 is 1.0%. Short-dated wheels live or die on
this line, and the old tool could not see it at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.paths import load_config

# --- Published schedule (defaults; overridable in config.yaml) -------------
DEFAULTS = {
    "option_open_commission_per_contract": 1.00,
    "option_close_commission_per_contract": 0.00,
    "option_commission_cap_per_leg": 10.00,
    "option_clearing_fee_per_contract": 0.10,
    "option_regulatory_fee_per_contract": 0.02,      # ORF
    "option_finra_taf_per_contract": 0.00329,        # sells only
    "assignment_or_exercise_fee": 5.00,
    "stock_clearing_fee_per_share": 0.0008,
    "stock_finra_taf_per_share": 0.000195,           # sells only
    "stock_finra_taf_cap": 9.79,
    "sec_fee_per_million_sold": 20.60,               # sells only
    # Fill assumption: fraction of the half-spread given up versus mid.
    # 0.0 = always filled at mid (fantasy), 1.0 = always at the bid/ask
    # (pessimistic). 0.40 is a fair working assumption for a limit order
    # working the mid on names that passed the Stage 3 liquidity screen.
    "slippage_fraction_of_half_spread": 0.40,
}


def _rates() -> dict:
    cfg = load_config().get("costs", {}) or {}
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    return merged


@dataclass(frozen=True)
class Fees:
    """Dollar costs for one action. `total` is always what leaves the account."""
    commission: float = 0.0
    clearing: float = 0.0
    regulatory: float = 0.0
    exchange: float = 0.0
    breakdown: dict = field(default_factory=dict)

    @property
    def total(self) -> float:
        # Deliberately unrounded. The FINRA TAF rate carries five decimals, and
        # rounding each leg before summing drifts by cents across a hundred-
        # trade backtest. Round at the point of display, not here.
        return self.commission + self.clearing + self.regulatory + self.exchange

    def __add__(self, other: "Fees") -> "Fees":
        merged = dict(self.breakdown)
        for k, v in other.breakdown.items():
            merged[k] = merged.get(k, 0.0) + v
        return Fees(self.commission + other.commission,
                    self.clearing + other.clearing,
                    self.regulatory + other.regulatory,
                    self.exchange + other.exchange,
                    merged)


# --- Option legs -----------------------------------------------------------

def option_open(contracts: int, side: str = "sell") -> Fees:
    """Cost to open an option position. `side` is 'sell' (credit) or 'buy'."""
    r = _rates()
    n = max(int(contracts), 0)
    commission = min(n * r["option_open_commission_per_contract"],
                     r["option_commission_cap_per_leg"])
    clearing = n * r["option_clearing_fee_per_contract"]
    reg = n * r["option_regulatory_fee_per_contract"]
    taf = n * r["option_finra_taf_per_contract"] if side == "sell" else 0.0
    return Fees(commission, clearing, reg + taf, 0.0,
                {"open_commission": commission, "clearing": clearing,
                 "orf": reg, "taf": taf})


def option_close(contracts: int, side: str = "buy") -> Fees:
    """Cost to close an option position. tastytrade charges no commission to
    close, but the clearing and regulatory fees still apply."""
    r = _rates()
    n = max(int(contracts), 0)
    commission = min(n * r["option_close_commission_per_contract"],
                     r["option_commission_cap_per_leg"])
    clearing = n * r["option_clearing_fee_per_contract"]
    reg = n * r["option_regulatory_fee_per_contract"]
    taf = n * r["option_finra_taf_per_contract"] if side == "sell" else 0.0
    return Fees(commission, clearing, reg + taf, 0.0,
                {"close_commission": commission, "clearing": clearing,
                 "orf": reg, "taf": taf})


def option_expire(contracts: int) -> Fees:
    """Letting a short option expire worthless is free. This function exists
    so callers never have to special-case the happy path."""
    return Fees(breakdown={"expiration": 0.0})


def assignment(contracts: int) -> Fees:
    """Assignment or exercise fee, charged per contract event."""
    r = _rates()
    fee = max(int(contracts), 0) * r["assignment_or_exercise_fee"]
    return Fees(0.0, 0.0, 0.0, fee, {"assignment": fee})


# --- Stock legs ------------------------------------------------------------

def stock_sell(shares: int, price: float) -> Fees:
    """Cost to sell shares (being called away, or exiting a wheel manually)."""
    r = _rates()
    n = max(int(shares), 0)
    clearing = n * r["stock_clearing_fee_per_share"]
    taf = min(n * r["stock_finra_taf_per_share"], r["stock_finra_taf_cap"])
    sec = (n * price / 1_000_000.0) * r["sec_fee_per_million_sold"]
    return Fees(0.0, clearing, taf + sec, 0.0,
                {"clearing": clearing, "taf": taf, "sec": sec})


def stock_buy(shares: int) -> Fees:
    r = _rates()
    n = max(int(shares), 0)
    clearing = n * r["stock_clearing_fee_per_share"]
    return Fees(0.0, clearing, 0.0, 0.0, {"clearing": clearing})


# --- Fills -----------------------------------------------------------------

def realistic_fill(bid: float | None, ask: float | None, mid: float | None = None,
                    side: str = "sell", fraction: float | None = None) -> float | None:
    """Price you should expect to actually get, not the mid.

    Selling gives up `fraction` of the half-spread below mid; buying pays the
    same above it. With one-sided or missing quotes the function degrades to
    whatever is knowable rather than inventing a number.
    """
    r = _rates()
    frac = r["slippage_fraction_of_half_spread"] if fraction is None else fraction

    if mid is None and bid is not None and ask is not None:
        mid = (bid + ask) / 2.0
    if mid is None:
        return None
    if bid is None or ask is None or ask < bid:
        return round(mid, 4)

    half = (ask - bid) / 2.0
    adjusted = mid - frac * half if side == "sell" else mid + frac * half
    # Never model a fill outside the quoted market.
    adjusted = max(bid, min(ask, adjusted))
    return round(adjusted, 4)


# --- Whole-trade economics -------------------------------------------------

@dataclass(frozen=True)
class TradeEconomics:
    contracts: int
    gross_credit: float        # premium collected before costs, in dollars
    entry_fees: float
    exit_fees: float
    net_credit: float          # what actually lands in the account
    collateral: float          # cash reserved (cash-secured put) in dollars
    cost_drag_pct: float       # costs as a share of gross credit
    net_return_on_collateral: float
    net_annualized: float

    def render(self) -> str:
        return (f"{self.contracts}x  gross ${self.gross_credit:,.2f}  "
                f"fees ${self.entry_fees + self.exit_fees:,.2f} "
                f"({self.cost_drag_pct:.1%} of gross)  "
                f"net ${self.net_credit:,.2f}  "
                f"{self.net_annualized:.1%} annualized on ${self.collateral:,.0f}")


def csp_economics(strike: float, fill_price: float, contracts: int, dte: int,
                   outcome: str = "expire") -> TradeEconomics:
    """Full economics for one cash-secured put at a modelled fill price.

    outcome: 'expire'  -- short option expires worthless (no exit cost)
             'close'   -- bought back before expiration
             'assign'  -- assigned; includes the $5 assignment fee
    """
    contracts = max(int(contracts), 1)
    gross = fill_price * 100.0 * contracts
    entry = option_open(contracts, "sell").total
    if outcome == "close":
        exit_fees = option_close(contracts, "buy").total
    elif outcome == "assign":
        exit_fees = assignment(contracts).total
    else:
        exit_fees = option_expire(contracts).total

    net = gross - entry - exit_fees
    collateral = strike * 100.0 * contracts
    drag = (entry + exit_fees) / gross if gross > 0 else float("nan")
    ret = net / collateral if collateral > 0 else float("nan")
    ann = ret * (365.0 / max(dte, 1))
    return TradeEconomics(contracts, gross, entry, exit_fees, net, collateral,
                          drag, ret, ann)


def covered_call_economics(basis: float, strike: float, fill_price: float,
                            contracts: int, dte: int,
                            outcome: str = "expire") -> TradeEconomics:
    """Economics for a covered call written against shares held at `basis`.

    Collateral here is the capital already tied up in the shares (basis x 100),
    which is the right denominator in a cash account: that money cannot be
    doing anything else until the position closes.
    """
    contracts = max(int(contracts), 1)
    gross = fill_price * 100.0 * contracts
    entry = option_open(contracts, "sell").total
    if outcome == "close":
        exit_fees = option_close(contracts, "buy").total
    elif outcome == "called_away":
        exit_fees = assignment(contracts).total + stock_sell(100 * contracts, strike).total
    else:
        exit_fees = option_expire(contracts).total

    net = gross - entry - exit_fees
    collateral = basis * 100.0 * contracts
    drag = (entry + exit_fees) / gross if gross > 0 else float("nan")
    ret = net / collateral if collateral > 0 else float("nan")
    ann = ret * (365.0 / max(dte, 1))
    return TradeEconomics(contracts, gross, entry, exit_fees, net, collateral,
                          drag, ret, ann)


def minimum_viable_credit(contracts: int = 1, max_drag: float = 0.10) -> float:
    """Smallest per-contract credit where costs stay under `max_drag` of gross.

    A practical entry filter for short-dated wheels: below this, you are
    working for tastytrade. At the default 10% drag and a one-contract
    open-and-expire, this lands around $0.11.
    """
    entry = option_open(contracts, "sell").total
    return round(entry / (max_drag * 100.0 * max(contracts, 1)), 4)


# --- Multi-leg (Phase 12) --------------------------------------------------

def legs_open(legs: list[tuple[str, int]]) -> Fees:
    """Open several legs: `legs` = [(side, contracts)], side 'sell' | 'buy'.
    The commission cap applies PER LEG (tastytrade: $10 per leg), which
    `option_open` already enforces for each call."""
    total = Fees()
    for side, contracts in legs:
        total = total + option_open(contracts, side)
    return total


def legs_close(legs: list[tuple[str, int]]) -> Fees:
    """Close several legs: a short leg is bought back ('buy'), a long leg sold."""
    total = Fees()
    for side, contracts in legs:
        total = total + option_close(contracts, side)
    return total


@dataclass(frozen=True)
class SpreadEconomics:
    contracts: int
    gross_credit: float          # dollars
    entry_fees: float
    exit_fees: dict              # outcome -> dollars
    max_loss: float              # dollars, before fees
    bpr: float                   # buying-power reduction, dollars
    cost_drag_pct: float         # entry fees / gross credit
    net_credit: float            # gross - entry fees (if it expires worthless)
    net_return_on_risk: float
    net_annualized: float


def vertical_exit_fees(contracts: int, cash_settled: bool) -> dict:
    """Exit cost of a two-leg vertical in each way it can end.

        expire_otm  both legs worthless -- free
        close       buy back the short, sell the long before expiry
        short_itm   short assigned / cash-settled, long expires worthless
        max_loss    both in the money: short assigned and long exercised

    Physically settled: each assignment or exercise is a $5 event plus the
    share trades it creates (bought on assignment, sold again or delivered by
    the long). Cash-settled index options settle in cash; the assignment /
    exercise fee is still charged here -- the conservative reading until the
    executing broker's statement shows otherwise.
    EARLY ASSIGNMENT (American equity options): a short put deep in the money
    with little extrinsic value left can be assigned before expiry, most often
    ahead of an ex-dividend date; the long put still caps the loss, but the
    account holds the shares overnight.
    """
    n = max(int(contracts), 1)
    event = assignment(n).total
    close = legs_close([("buy", n), ("sell", n)]).total
    if cash_settled:
        return {"expire_otm": 0.0, "close": close, "short_itm": event, "max_loss": 2 * event}
    shares = 100 * n
    return {"expire_otm": 0.0, "close": close,
            "short_itm": event + stock_buy(shares).total,
            "max_loss": 2 * event + stock_buy(shares).total}


def vertical_economics(short_strike: float, long_strike: float, net_credit: float,
                       contracts: int, dte: int, cash_settled: bool = False) -> SpreadEconomics:
    """Economics of a credit vertical (e.g. a put credit spread) at a modelled
    net credit per share."""
    n = max(int(contracts), 1)
    width = abs(short_strike - long_strike)
    gross = net_credit * 100.0 * n
    entry = legs_open([("sell", n), ("buy", n)]).total
    max_loss = max(width - net_credit, 0.0) * 100.0 * n
    net = gross - entry
    drag = entry / gross if gross > 0 else float("nan")
    ror = net / max_loss if max_loss > 0 else float("nan")
    return SpreadEconomics(n, gross, entry, vertical_exit_fees(n, cash_settled), max_loss,
                           max_loss, drag, net, ror, ror * 365.0 / max(dte, 1))


def package_fill(short_bid, short_ask, long_bid, long_ask,
                 fraction: float | None = None) -> dict | None:
    """Multi-leg fill model: net mid minus `slippage_fraction_of_half_spread`
    of the SUMMED half-spreads, never better than the net mid nor worse than
    the natural (short bid - long ask)."""
    r = _rates()
    frac = r["slippage_fraction_of_half_spread"] if fraction is None else fraction
    values = [short_bid, short_ask, long_bid, long_ask]
    if any(v is None for v in values) or short_ask < short_bid or long_ask < long_bid:
        return None
    short_mid, long_mid = (short_bid + short_ask) / 2.0, (long_bid + long_ask) / 2.0
    net_mid = short_mid - long_mid
    natural = short_bid - long_ask
    half = (short_ask - short_bid) / 2.0 + (long_ask - long_bid) / 2.0
    modelled = min(max(net_mid - frac * half, natural), net_mid)
    return {"net_mid": round(net_mid, 4), "natural": round(natural, 4),
            "modelled": round(modelled, 4), "summed_half_spread": round(half, 4)}
