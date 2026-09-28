"""
Put credit spread construction (Phase 12, roadmap B.6).

For each expiration (and option root -- SPX has SPXW PM and SPX AM) inside
the request's DTE window:

1. **Short strike by rule.** `delta`: the put whose delta is nearest the
   middle of the request's delta range (and inside it). `em_multiple`: the
   highest listed put at or below spot - k x EM (k = request.em_multiple,
   EM = the configured expected-move method for that expiration).
   `support`: the highest listed put at or below the strongest respected
   support (Phase 10 "strong", highest edge CI lower bound) minus its median
   pierce depth (ATR units x today's ATR). `conservative` (the default)
   builds all three; a strike chosen by several rules carries all their
   names, and the LOWEST short strike that passes every gate is marked
   `default_choice` per ticker, expiration and width.
2. **Long legs** at each requested dollar width, snapped to the nearest
   listed put below the short (the actual width is reported; two widths
   snapping to the same long strike are built once).
3. **Pricing.** Net mid, natural (short bid - long ask), and the modelled
   fill: net mid minus `slippage_fraction_of_half_spread` of the summed
   half-spreads (`costs.package_fill`).
4. **Risk.** Max profit = credit, max loss = width - credit, BPR = max loss,
   return on risk, breakeven = short - credit, net Greeks.
5. **Probabilities and EV** over the SAME empirical terminal sample the CSP
   uses (`moves.select_windows`: 10 years, vol-conditioned): POP = P(S_T >
   breakeven), P(max loss) = P(S_T <= long), P(short ITM), P(touch short),
   and EV = mean P&L at expiry net of entry fees and each outcome's exit
   fees. Annualised on BPR.
6. **Sizing** by BPR against the account profile, with the liquidity caps
   applied to BOTH legs (the thinner binds), then the regime multiplier and
   the request's max-loss cap.
7. **Gates** (rejections): the event policy for `pcs` (single-stock
   earnings block like CSP), minimum credit, cost drag, non-positive EV,
   sizing (incl. OI / volume floors on either leg), inverted skew, the
   short leg's IV/RV floor, and the request's `min_pop`. Credit/width below
   `pcs.credit_width_floor` is a WARNING, not a rejection.
8. **Tier** by width, narrowest first: Conservative / Moderate / Aggressive.

Index options (cash-settled, European) have no early assignment; the row
says whether the expiration settles AM or PM. American equity puts can be
assigned early -- noted on each row.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from analytics import costs, liquidity, moves, regime, sizing, vrp
from analytics.strategies.base import Leg, Position
from core.market_calendar import trading_days_between
from core.paths import load_config


@dataclass
class SpreadRecommendation:
    ticker: str
    expiration: dt.date
    strike: float                     # short strike
    long_strike: float
    width: float
    requested_width: float
    spot: float
    dte_calendar: int
    dte_trading: int
    root_symbol: str | None
    settlement_type: str | None

    # Pricing (per share)
    net_mid: float
    natural: float
    modelled_fill: float              # the modelled net credit
    bid: float | None                 # short leg
    ask: float | None
    mid: float | None
    long_bid: float | None
    long_ask: float | None
    long_mid: float | None
    delta: float | None               # short leg delta (chain sign)
    long_delta: float | None
    implied_vol: float | None         # short leg IV
    long_iv: float | None

    # Sizing and economics
    contracts: int
    collateral: float                 # BPR in dollars (max loss x contracts)
    binding_constraint: str
    scales: bool
    gross_credit: float
    fees: float                       # entry fees
    net_credit: float
    cost_drag: float
    max_profit: float                 # dollars
    max_loss: float                   # dollars
    return_on_risk: float
    breakeven: float
    credit_width: float
    expected_value: float
    ev_annualised: float
    net_annualised_if_expires: float

    # Probabilities (empirical, 10y vol-conditioned)
    prob_otm_empirical: float | None  # POP = P(S_T > breakeven)
    prob_short_itm: float | None
    prob_max_loss: float | None
    prob_touch: float | None
    sample_label: str
    effective_n: int

    # Context and verdict
    iv_rv_ratio: float | None
    strike_rule: str
    strike_rule_reason: str
    tier: str
    accepted: bool
    rejections: tuple = ()
    warnings: tuple = ()
    notes: tuple = ()
    rationale: str = ""
    open_interest: float | None = None        # short leg
    option_volume: float | None = None
    long_open_interest: float | None = None
    long_option_volume: float | None = None
    fillability: float | None = None
    weakest_leg: str = ""
    net_delta: float | None = None            # per contract, position-signed
    net_theta: float | None = None
    net_vega: float | None = None
    ivr: float | None = None
    ivp: float | None = None
    iv_index: float | None = None
    liquidity_rating: int | None = None
    events: tuple = ()
    normalised_skew: float | None = None
    skew_class: str = ""
    strategy: str = "pcs"

    def to_dict(self) -> dict:
        out = asdict(self)
        out["expiration"] = str(self.expiration)
        return out

    @property
    def rank_key(self) -> float:
        return self.ev_annualised if self.accepted else -1e9


def _f(value) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if np.isfinite(value) else None


def _tier(index: int, count: int, labels: list[str]) -> str:
    if count <= 1:
        return labels[1] if len(labels) > 1 else labels[0]
    position = index / (count - 1)
    return labels[0] if position <= 1 / 3 else labels[1] if position <= 2 / 3 else labels[2]


# --- Short strike rules ------------------------------------------------------------

def short_strikes(puts: pd.DataFrame, spot: float, request, em: float | None,
                  strongest: dict | None, atr: float | None) -> dict[float, list[tuple[str, str]]]:
    """{short strike: [(rule, reason), ...]} for the rules the request asks for.
    `puts` = one expiration's quoted OTM puts (strike_price, put_delta)."""
    rules = ["delta", "em_multiple", "support"] if request.strike_rule == "conservative" \
        else [request.strike_rule]
    strikes = puts["strike_price"].astype(float)
    out: dict[float, list[tuple[str, str]]] = {}

    def add(strike, rule, reason):
        out.setdefault(float(strike), []).append((rule, reason))

    if "delta" in rules and "put_delta" in puts:
        lo, hi = request.delta_range
        band = puts[(puts["put_delta"] >= lo) & (puts["put_delta"] <= hi)]
        if not band.empty:
            target = (lo + hi) / 2.0
            row = band.iloc[(band["put_delta"] - target).abs().argmin()]
            add(row["strike_price"], "delta",
                f"delta {row['put_delta']:+.2f}, nearest the middle ({target:+.2f}) of the "
                f"requested [{lo:+.2f}, {hi:+.2f}] band")
    if "em_multiple" in rules and em:
        k = float(request.em_multiple)
        target = spot - k * em
        eligible = strikes[strikes <= target]
        if not eligible.empty:
            add(eligible.max(), "em_multiple",
                f"highest strike at or below spot - {k:g} x EM (${spot:,.2f} - "
                f"{k:g} x ${em:,.2f} = ${target:,.2f})")
    if "support" in rules and strongest and atr:
        pierce = float(strongest.get("median_pierce_atr") or 0.0)
        target = float(strongest["level"]) - pierce * atr
        eligible = strikes[strikes <= target]
        if not eligible.empty:
            add(eligible.max(), "support",
                f"below the {strongest['level_id']} at ${float(strongest['level']):,.2f} "
                f"(strong: {strongest.get('summary', '')}) minus its median pierce "
                f"{pierce:.2f} ATR (${pierce * atr:,.2f}) = ${target:,.2f}")
    return out


def snap_long(puts: pd.DataFrame, short: float, width: float) -> float | None:
    """The listed put strike nearest short - width, strictly below the short."""
    below = puts["strike_price"].astype(float)
    below = below[below < short]
    if below.empty:
        return None
    return float(below.iloc[(below - (short - width)).abs().argmin()])


# --- Construction ------------------------------------------------------------------

def build_candidates(ticker: str, ctx, daily: pd.DataFrame, adv_dollars: float | None,
                     account, cfg: dict, regime_reading, request, checks: dict,
                     metrics: dict | None = None, skew: dict | None = None,
                     today: dt.date | None = None) -> list[SpreadRecommendation]:
    """Every PCS the request asks for on one ticker. `ctx` is the ticker's
    `strategies.context.TickerContext`; `checks` maps expiration -> the
    events.EventCheck for strategy 'pcs'."""
    today = today or ctx.today
    pcs_cfg = cfg.get("pcs", {}) or {}
    entry_cfg = cfg.get("management", {}).get("entry", {}) or {}
    labels = list(pcs_cfg.get("tiers", ["Conservative", "Moderate", "Aggressive"]))
    metrics = metrics or {}
    skew = skew or {}
    chain = ctx.chain.copy()
    if chain.empty or "put_bid" not in chain:
        return []
    chain["expiration"] = pd.to_datetime(chain["expiration"])
    chain["dte_calendar"] = (chain["expiration"].dt.date - today).apply(lambda d: d.days)
    # With spread DTE targets (Phase 15 default 45), build at the listed
    # expiration nearest each target; otherwise at every one in the window.
    keep = request.nearest_pcs_dtes(chain["dte_calendar"].unique())
    chain = chain[chain["dte_calendar"].isin(keep)]
    if "root_symbol" not in chain:
        chain["root_symbol"] = None
    strongest = ctx.strongest_support()
    widths = request.pcs_widths(ctx.spot)
    out: list[SpreadRecommendation] = []

    for (expiration, root), group in chain.groupby(["expiration", "root_symbol"], dropna=False):
        root = None if pd.isna(root) else root
        exp_date = pd.Timestamp(expiration).date()
        puts = group[(group["put_bid"].fillna(0) > 0) & group["put_ask"].notna()
                     & (group["strike_price"].astype(float) < ctx.spot)].sort_values("strike_price")
        # The long leg may be quoted at a zero bid; it must have an ask.
        longs = group[group["put_ask"].fillna(0) > 0].sort_values("strike_price")
        if puts.empty:
            continue
        em = ctx.expected_move(expiration, root)
        em_value = em.em if em is not None and np.isfinite(em.em) else None
        chosen = short_strikes(puts, ctx.spot, request, em_value, strongest, ctx.atr)
        dte_cal = max((exp_date - today).days, 0)
        dte_trd = max(trading_days_between(today, exp_date), 1)
        selected = moves.select_windows(daily, dte_trd, lookback_years=10,
                                        vol_conditioned=True, min_observations=40) \
            if not daily.empty else None
        terminal = selected[0] if selected else None
        check = checks.get(exp_date)
        settlement_type = group["settlement_type"].dropna().iloc[0] \
            if "settlement_type" in group and group["settlement_type"].notna().any() else None

        for short, rules in sorted(chosen.items()):
            s_row = group[group["strike_price"].astype(float) == short].iloc[0]
            built: set[float] = set()
            for index, width in enumerate(widths):
                long = snap_long(longs, short, width)
                if long is None or long in built:
                    continue
                built.add(long)
                l_row = group[group["strike_price"].astype(float) == long].iloc[0]
                rec = _evaluate(ticker, ctx, s_row, l_row, rules, exp_date, root,
                                settlement_type, dte_cal, dte_trd, terminal,
                                selected[1] if selected else "no empirical sample",
                                daily, adv_dollars, account, cfg, pcs_cfg, entry_cfg,
                                regime_reading, request, check, metrics, skew,
                                _tier(index, len(widths), labels), width)
                if rec is not None:
                    out.append(rec)
    return out


def _evaluate(ticker, ctx, s_row, l_row, rules, exp_date, root, settlement_type,
              dte_cal, dte_trd, terminal, sample_label, daily, adv, account, cfg,
              pcs_cfg, entry_cfg, regime_reading, request, check, metrics, skew,
              tier, requested_width) -> SpreadRecommendation | None:
    short, long = float(s_row["strike_price"]), float(l_row["strike_price"])
    width = short - long
    sb, sa, lb, la = (_f(s_row.get("put_bid")), _f(s_row.get("put_ask")),
                      _f(l_row.get("put_bid")), _f(l_row.get("put_ask")))
    fill = costs.package_fill(sb, sa, lb if lb is not None else 0.0, la)
    if fill is None or fill["modelled"] <= 0:
        return None
    credit = fill["modelled"]
    reasons: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []

    # Position shape
    legs = [Leg("put", "short", short, exp_date, 1, sb, sa, _f(s_row.get("put_mark")),
                _f(s_row.get("put_iv")), _f(s_row.get("put_delta")), _f(s_row.get("put_gamma")),
                _f(s_row.get("put_theta")), _f(s_row.get("put_vega")),
                _f(s_row.get("put_open_interest")), _f(s_row.get("put_volume"))),
            Leg("put", "long", long, exp_date, 1, lb, la, _f(l_row.get("put_mark")),
                _f(l_row.get("put_iv")), _f(l_row.get("put_delta")), _f(l_row.get("put_gamma")),
                _f(l_row.get("put_theta")), _f(l_row.get("put_vega")),
                _f(l_row.get("put_open_interest")), _f(l_row.get("put_volume")))]
    position = Position("pcs", ticker, legs, credit)
    per_contract_risk = position.max_loss * 100.0
    if per_contract_risk <= 0:
        return None            # a credit at or above the width is a bad quote, not a trade

    # Sizing: BPR against the profile; liquidity on both legs
    size = sizing.max_contracts_for_position(
        per_contract_risk, account,
        legs=[(legs[0].open_interest, legs[0].volume, "short leg"),
              (legs[1].open_interest, legs[1].volume, "long leg")],
        adv_dollars=adv, notional_per_contract=short * 100.0, capital_label="max loss")
    contracts = size.contracts
    contracts, _ = regime.apply_to_sizing(contracts, regime_reading) if contracts else (0, "")
    if request.max_loss_per_trade is not None:
        by_loss = int(request.max_loss_per_trade // per_contract_risk)
        if by_loss < 1:
            reasons.append(f"one spread risks ${per_contract_risk:,.0f}, over the requested "
                           f"${request.max_loss_per_trade:,.0f} max loss per trade")
        contracts = min(contracts, max(by_loss, 0))
    n = max(contracts, 1)

    econ = costs.vertical_economics(short, long, credit, n, max(dte_cal, 1), ctx.cash_settled)

    # Probabilities and EV over the empirical terminal sample
    pop = p_itm = p_max = p_touch = None
    ev = float("nan")
    eff_n = 0
    if terminal is not None and len(terminal):
        s_t = ctx.spot * (1.0 + terminal["terminal_return"].to_numpy(float))
        low = ctx.spot * (1.0 + terminal["mae"].to_numpy(float))
        breakeven = short - credit
        pnl = position.payoff(s_t) * 100.0 * n - econ.entry_fees
        exit_fee = np.where(s_t <= long, econ.exit_fees["max_loss"],
                            np.where(s_t < short, econ.exit_fees["short_itm"], 0.0))
        ev = float(np.mean(pnl - exit_fee))
        pop = float(np.mean(s_t > breakeven))
        p_itm = float(np.mean(s_t < short))
        p_max = float(np.mean(s_t <= long))
        p_touch = float(np.mean(low <= short))
        eff_n = moves._effective_n(len(terminal), dte_trd)
    bpr_total = per_contract_risk * n
    ev_ann = ev / bpr_total * 365.0 / max(dte_cal, 1) if bpr_total else float("nan")

    # IV/RV on the short leg, same horizon match as the CSP gate
    ratio = None
    if legs[0].iv and not daily.empty:
        reading = vrp.reading(ticker, legs[0].iv, daily, vrp.match_rv_window_to_dte(dte_cal))
        ratio = reading.ratio if reading and np.isfinite(reading.ratio) else None

    # Gates
    if check is not None:
        reasons.extend(check.texts("block"))
        warnings.extend(check.texts("warn"))
    min_credit = float(pcs_cfg.get("min_credit", 0.10))
    if credit < min_credit:
        reasons.append(f"net credit ${credit:.2f} is below the ${min_credit:.2f} floor")
    max_drag = float(entry_cfg.get("max_cost_drag", 0.10))
    if np.isfinite(econ.cost_drag_pct) and econ.cost_drag_pct > max_drag:
        reasons.append(f"fees are {econ.cost_drag_pct:.1%} of gross credit (limit {max_drag:.0%})")
    if size.rejected:
        reasons.extend(size.reasons or ("no tradable size",))
    if np.isfinite(ev) and ev <= 0:
        reasons.append(f"expected value is ${ev:,.0f} -- the empirical loss tail outweighs "
                       f"the credit at this short strike and width")
    if skew.get("skew_class") == "inverted" and cfg.get("signals", {}).get(
            "block_inverted_skew", True):
        reasons.append(skew.get("skew_note", "put skew is inverted"))
    min_ratio = float(entry_cfg.get("min_iv_rv_ratio", 1.05))
    if pcs_cfg.get("apply_iv_rv_gate", True) and ratio is not None and ratio < min_ratio:
        reasons.append(f"short-leg IV/RV is {ratio:.2f}, below the {min_ratio:.2f} floor")
    if request.min_pop is not None and (pop is None or pop < request.min_pop):
        reasons.append(f"POP {pop:.0%} is below the requested {request.min_pop:.0%}"
                       if pop is not None else "no empirical POP to test against min_pop")
    floor = float(pcs_cfg.get("credit_width_floor", 1 / 3))
    credit_width = credit / width if width else float("nan")
    if credit_width < floor:
        warnings.append(f"credit is {credit_width:.0%} of the width, under the "
                        f"{floor:.0%} rule of thumb -- a lot of risk per dollar collected")
    if abs(width - requested_width) > 0.5 * requested_width:
        warnings.append(f"requested a ${requested_width:g} width; the nearest listed long "
                        f"strike makes it ${width:g} -- use widths that fit this chain's "
                        f"strike spacing")
    if lb is None or lb == 0:
        notes.append("long leg bid is zero; the fill assumes paying its ask side")
    if ctx.cash_settled:
        notes.append(f"cash-settled, European: no early assignment; "
                     f"{settlement_type or '?'}-settled expiration"
                     + (" (AM settlement: the final value is set at the open)"
                        if settlement_type == "AM" else ""))
    else:
        notes.append("American-style: the short put can be assigned early when deep in the "
                     "money with little extrinsic left; the long put still caps the loss")

    liq = liquidity.position([liquidity.leg(s_row, "put"), liquidity.leg(l_row, "put")])
    greeks = position.net_greeks()
    accepted = not reasons and contracts >= 1
    rule_names = "+".join(r for r, _ in rules)
    rationale = (f"Sell the ${short:g}/${long:g} put spread ({width:g} wide) for ~${credit:.2f}"
                 f" x{contracts}: max loss ${per_contract_risk * n:,.0f}, "
                 f"breakeven ${short - credit:,.2f}"
                 + (f", POP {pop:.0%}" if pop is not None else "")
                 + (f", EV {ev_ann:.1%} annualised on risk" if np.isfinite(ev_ann) else "")
                 + f". Short strike by {rule_names}.")

    return SpreadRecommendation(
        ticker=ticker, expiration=exp_date, strike=short, long_strike=long, width=width,
        requested_width=float(requested_width),
        spot=ctx.spot, dte_calendar=dte_cal, dte_trading=dte_trd, root_symbol=root,
        settlement_type=settlement_type,
        net_mid=fill["net_mid"], natural=fill["natural"], modelled_fill=credit,
        bid=sb, ask=sa, mid=legs[0].mid, long_bid=lb, long_ask=la, long_mid=legs[1].mid,
        delta=legs[0].delta, long_delta=legs[1].delta, implied_vol=legs[0].iv,
        long_iv=legs[1].iv,
        contracts=contracts, collateral=bpr_total, binding_constraint=size.binding_constraint,
        scales=size.scales, gross_credit=econ.gross_credit, fees=econ.entry_fees,
        net_credit=econ.net_credit, cost_drag=econ.cost_drag_pct,
        max_profit=position.max_profit * 100.0 * n, max_loss=position.max_loss * 100.0 * n,
        return_on_risk=credit / (width - credit), breakeven=short - credit,
        credit_width=credit_width, expected_value=ev, ev_annualised=ev_ann,
        net_annualised_if_expires=econ.net_annualized,
        prob_otm_empirical=pop, prob_short_itm=p_itm, prob_max_loss=p_max, prob_touch=p_touch,
        sample_label=sample_label, effective_n=eff_n, iv_rv_ratio=ratio,
        strike_rule=rule_names, strike_rule_reason=" | ".join(r for _, r in rules),
        tier=tier, accepted=accepted, rejections=tuple(reasons), warnings=tuple(warnings),
        notes=tuple(notes), rationale=rationale,
        open_interest=legs[0].open_interest, option_volume=legs[0].volume,
        long_open_interest=legs[1].open_interest, long_option_volume=legs[1].volume,
        fillability=liq.fillability, weakest_leg=f"{liq.weakest.side} {liq.weakest.strike:g}"
        + (" (long)" if liq.weakest_index == 1 else " (short)"),
        net_delta=greeks["delta"], net_theta=greeks["theta"], net_vega=greeks["vega"],
        ivr=metrics.get("ivr"), ivp=metrics.get("ivp"), iv_index=metrics.get("iv_index"),
        liquidity_rating=metrics.get("liquidity_rating"),
        events=tuple(h.text() for h in check.hits) if check is not None else (),
        normalised_skew=skew.get("normalised_skew"), skew_class=skew.get("skew_class", ""))


def mark_default_choice(frame: pd.DataFrame) -> pd.DataFrame:
    """Per ticker, expiration and tier: the LOWEST short strike that passed
    every gate -- 'the most conservative that passes' (roadmap B.6)."""
    out = frame.copy()
    out["default_choice"] = False
    pcs = out[(out["strategy"] == "pcs") & out["accepted"]]
    if pcs.empty:
        return out
    keys = ["ticker", "expiration", "tier"]
    idx = pcs.sort_values("strike").groupby(keys, dropna=False).head(1).index
    out.loc[idx, "default_choice"] = True
    return out
