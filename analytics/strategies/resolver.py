"""
The generic resolver: strategy spec + chain -> priced positions
(Phase 16, roadmap C.8 task 2).

For one ticker and one spec (analytics/strategy_spec.py):

1. **Expirations.** Each role in the spec's `expirations` takes the listed
   expiration nearest its `dte_target` within `tolerance`, later than the
   role before it. Index chains with several roots (SPX / SPXW) are
   resolved per root.
2. **Strikes.** Each option leg's selector picks a listed strike (delta,
   moneyness, EM multiple, ATM, an offset from an earlier leg, or an earlier
   leg's strike at another expiration). Short legs need a bid, every leg an
   ask. A stock leg is bought at spot.
3. **Pricing.** Net mid, natural, and the modelled fill over all option
   legs (`costs.multi_leg_fill`); a stock leg's price is subtracted from the
   credit.
4. **Risk.** `strategies.base.Position`: payoff at the front expiry (later
   legs valued by Black-Scholes -- model risk, flagged), max profit / loss,
   breakevens, net Greeks. Buying power from the spec's margin class
   (`analytics.margin`).
5. **Sizing** against the account profile with every leg's liquidity caps
   (`sizing.max_contracts_for_position`).
6. **Empirical odds and EV** over the same vol-conditioned terminal sample
   the CSP and PCS rows use: POP, P(max loss) for defined risk, and EV net
   of entry and expiry fees.
7. **Gates** (rejections): account permission for the margin class, the
   event policy the spec obeys (`event_policy_as`; `entry.earnings: allow`
   downgrades an earnings block to a warning), the minimum option credit,
   the liquidity floors, and non-positive EV.

Each result is a plain dict (one row of the recommender's sheet) carrying
`legs_json`, from which `position_from_row` rebuilds the Position.
"""
from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pandas as pd

from analytics import costs, margin, moves, sizing
from analytics.options_math import implied_vol
from analytics.strategies.base import Leg, Position
from core.market_calendar import trading_days_between
from core.paths import load_config


def _f(value) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if np.isfinite(value) else None


def pick_expirations(dates: list[dt.date], spec, today: dt.date,
                     quoted: dict[str, set] | None = None) -> dict[str, dt.date] | None:
    """Role -> expiration, in the spec's order, each later than the last.
    `quoted` maps option type -> expirations that type is quoted at; a role
    only takes an expiration where every leg type it needs is quoted (a
    listed but unquoted expiry, e.g. a holiday weekly, is skipped)."""
    chosen: dict[str, dt.date] = {}
    floor = None
    for role, exp in spec.expirations.items():
        target, tol = int(exp["dte_target"]), int(exp.get("tolerance", 14))
        kinds = {l.type for l in spec.legs if l.expiration == role and l.type != "stock"}
        options = [d for d in dates if abs((d - today).days - target) <= tol
                   and (floor is None or d > floor)
                   and (quoted is None or all(d in quoted.get(k, set()) for k in kinds))]
        if not options:
            return None
        best = min(options, key=lambda d: (abs((d - today).days - target), d))
        chosen[role] = best
        floor = best
    return chosen


def _quotes(frame: pd.DataFrame, kind: str, need_bid: bool) -> pd.DataFrame:
    rows = frame[frame[f"{kind}_ask"].fillna(0) > 0]
    if need_bid:
        rows = rows[rows[f"{kind}_bid"].fillna(0) > 0]
    return rows.sort_values("strike_price")


def select_strike(leg_spec, rows: pd.DataFrame, spot: float, em: float | None,
                  chosen: dict[str, Leg]) -> tuple[pd.Series | None, str]:
    """The chain row for one option leg, and a sentence saying why."""
    kind, sel = leg_spec.type, leg_spec.select
    if rows.empty:
        return None, "no quoted strikes"
    strikes = rows["strike_price"].astype(float)
    key = leg_spec.selector
    if key == "delta":
        deltas = rows[f"{kind}_delta"].astype(float)
        ok = deltas.notna()
        if not ok.any():
            return None, "no deltas in the chain"
        idx = (deltas[ok] - float(sel["delta"])).abs().idxmin()
        return rows.loc[idx], f"delta nearest {float(sel['delta']):+.2f} ({deltas[idx]:+.2f})"
    if key == "same_strike_as":
        ref = chosen[sel["same_strike_as"]].strike
        match = rows[strikes == ref]
        if match.empty:
            return None, f"strike {ref:g} not listed at this expiration"
        return match.iloc[0], f"same strike as {sel['same_strike_as']} (${ref:g})"
    if key == "moneyness":
        target, why = spot * (1.0 + float(sel["moneyness"])), f"{float(sel['moneyness']):+.1%} from spot"
    elif key == "em_multiple":
        if not em:
            return None, "no expected move for this expiration"
        k = float(sel["em_multiple"])
        target = spot - k * em if kind == "put" else spot + k * em
        why = f"{k:g} EM from spot (EM ${em:,.2f})"
    elif key == "atm":
        target, why = spot, "nearest the money"
    else:                                       # offset_from
        ref = chosen[sel["offset_from"]].strike
        if sel.get("width") is not None:
            width = float(sel["width"])
        elif sel.get("width_pct") is not None:
            width = float(sel["width_pct"]) / 100.0 * spot
        else:
            if not em:
                return None, "no expected move for the EM width"
            width = float(sel["width_em"]) * em
        target = ref - width if kind == "put" else ref + width
        beyond = rows[strikes < ref] if kind == "put" else rows[strikes > ref]
        if beyond.empty:
            return None, f"no strike beyond {sel['offset_from']} ${ref:g}"
        idx = (beyond["strike_price"].astype(float) - target).abs().idxmin()
        return beyond.loc[idx], f"${width:,.2f} past {sel['offset_from']} (${ref:g})"
    idx = (strikes - target).abs().idxmin()
    return rows.loc[idx], why


def _leg_from_row(leg_spec, row: pd.Series, expiration: dt.date, root) -> Leg:
    k = leg_spec.type
    bid, ask = _f(row.get(f"{k}_bid")), _f(row.get(f"{k}_ask"))
    mid = (bid + ask) / 2.0 if bid is not None and ask is not None else _f(row.get(f"{k}_mark"))
    return Leg(option_type=k, side=leg_spec.side, strike=float(row["strike_price"]),
               expiration=expiration, qty=leg_spec.qty, bid=bid, ask=ask, mid=mid,
               iv=_f(row.get(f"{k}_iv")), delta=_f(row.get(f"{k}_delta")),
               gamma=_f(row.get(f"{k}_gamma")), theta=_f(row.get(f"{k}_theta")),
               vega=_f(row.get(f"{k}_vega")), open_interest=_f(row.get(f"{k}_open_interest")),
               volume=_f(row.get(f"{k}_volume")), symbol=row.get(f"{k}_symbol"))


def legs_to_json(legs: list[Leg]) -> str:
    return json.dumps([l.to_dict() for l in legs])


def position_from_row(row: dict) -> Position:
    """Rebuild a resolved position from its sheet row."""
    legs = []
    for raw in json.loads(row["legs_json"]):
        raw = dict(raw)
        raw["expiration"] = pd.Timestamp(raw["expiration"]).date()
        legs.append(Leg(**raw))
    return Position(row["strategy"], row["ticker"], legs, float(row["modelled_fill"]),
                    collateral_per_contract=_f(row.get("bpr_per_contract")),
                    front_dte=_f(row.get("dte_calendar")))


def legs_text(legs: list[Leg]) -> str:
    out = []
    for leg in legs:
        if leg.is_stock:
            out.append(f"{'+' if leg.side == 'long' else '-'}{100 * leg.qty} sh")
            continue
        sign = "-" if leg.side == "short" else "+"
        qty = f"{leg.qty}" if leg.qty != 1 else ""
        out.append(f"{sign}{qty}{leg.strike:g}{leg.option_type[0].upper()} "
                   f"{pd.Timestamp(leg.expiration):%d%b}")
    return " / ".join(out)


def _expiry_fees(position: Position, s_t: np.ndarray, contracts: int) -> np.ndarray:
    """Per-sample exit fees at the front expiry: assignment/exercise for
    expiring legs in the money, a close commission for later legs."""
    fees = np.zeros(len(s_t))
    for leg, offset in zip(position.legs, position.expiry_offsets()):
        if leg.is_stock:
            continue
        if offset > 0:
            fees += costs.option_close(contracts * leg.qty,
                                       "buy" if leg.side == "short" else "sell").total
        else:
            fees += (leg.intrinsic(s_t) > 0) * costs.assignment(contracts * leg.qty).total
    return fees


def resolve(spec, ctx, request, account, cfg: dict | None = None, today: dt.date | None = None,
            daily: pd.DataFrame | None = None, adv_dollars: float | None = None,
            events_frame: pd.DataFrame | None = None, calendar_healthy: bool = True
            ) -> tuple[list[dict], list[str]]:
    """Every position `spec` resolves to on this ticker (one per option
    root), and the reasons when it resolves to none."""
    cfg = cfg or load_config()
    today = today or ctx.today
    chain = ctx.chain.copy()
    if chain.empty:
        return [], ["no chain"]
    chain["expiration"] = pd.to_datetime(chain["expiration"]).dt.date
    if "root_symbol" not in chain:
        chain["root_symbol"] = None
    profile = account.config
    allowed, why_not = margin.permitted(spec.margin_class, profile)
    rows, reasons = [], []
    for root, group in chain.groupby("root_symbol", dropna=False):
        root = None if pd.isna(root) else root
        quoted = {k: set(group.loc[group[f"{k}_ask"].fillna(0) > 0, "expiration"])
                  for k in ("put", "call") if f"{k}_ask" in group}
        exps = pick_expirations(sorted(group["expiration"].unique()), spec, today, quoted)
        if exps is None:
            reasons.append(f"no listed expiration fits {spec.expirations}")
            continue
        chosen: dict[str, Leg] = {}
        why: dict[str, str] = {}
        failed = None
        for leg_spec in spec.legs:
            if leg_spec.type == "stock":
                chosen[leg_spec.name] = Leg("stock", leg_spec.side, float(ctx.spot),
                                            exps[next(iter(exps))], leg_spec.qty,
                                            bid=ctx.spot, ask=ctx.spot, mid=ctx.spot)
                why[leg_spec.name] = "shares at spot"
                continue
            expiration = exps[leg_spec.expiration]
            at_exp = group[group["expiration"] == expiration]
            rows_q = _quotes(at_exp, leg_spec.type, need_bid=leg_spec.side == "short")
            em = ctx.expected_move(pd.Timestamp(expiration), root)
            em_value = em.em if em is not None and np.isfinite(em.em) else None
            row, reason = select_strike(leg_spec, rows_q, ctx.spot, em_value, chosen)
            if row is None:
                failed = f"{leg_spec.name}: {reason}"
                break
            leg = _leg_from_row(leg_spec, row, expiration, root)
            if any(l.option_type == leg.option_type and l.strike == leg.strike
                   and l.expiration == leg.expiration for l in chosen.values()):
                failed = f"{leg_spec.name}: resolves to a strike already used"
                break
            chosen[leg_spec.name] = leg
            why[leg_spec.name] = reason
        if failed:
            reasons.append(failed)
            continue
        row = _evaluate(spec, ctx, request, account, cfg, today, daily, adv_dollars,
                        events_frame, calendar_healthy, list(chosen.values()), why, exps,
                        root, allowed, why_not)
        if row is not None:
            rows.append(row)
        else:
            reasons.append("could not price the package")
    return rows, reasons


def _evaluate(spec, ctx, request, account, cfg, today, daily, adv, events_frame, healthy,
              legs, why, exps, root, allowed, why_not) -> dict | None:
    options = [l for l in legs if not l.is_stock]
    fill = costs.multi_leg_fill([{"side": l.side, "qty": l.qty, "bid": l.bid, "ask": l.ask}
                                 for l in options])
    if fill is None:
        return None
    stock_cost = sum((1 if l.side == "long" else -1) * l.qty * float(ctx.spot)
                     for l in legs if l.is_stock)
    credit = fill["modelled"] - stock_cost
    rate = float((cfg.get("analytics", {}) or {}).get("risk_free_rate", 0.045))
    front = min(exps.values())
    dte_cal = max((front - today).days, 0)
    # Model IVs implied from each leg's mid by OUR pricer (no dividend yield),
    # so the model reproduces the entry prices. The chain's IVs use the
    # vendor's dividend and rate assumptions; mixing them into our pricer
    # misprices a calendar's two legs against each other.
    chain_iv = {}
    for leg in options:
        chain_iv[id(leg)] = leg.iv
        days = max((pd.Timestamp(leg.expiration).date() - today).days, 1)
        if leg.mid and leg.mid > 0:
            model = implied_vol(leg.mid, float(ctx.spot), leg.strike, days, rate,
                                leg.option_type)
            if model:
                leg.iv = float(model)
    position = Position(spec.id, ctx.ticker, legs, credit, rate=rate, front_dte=dte_cal)
    bpr_one = margin.bpr_per_contract(position, spec.margin_class, float(ctx.spot))
    position.collateral_per_contract = bpr_one
    dte_trd = max(trading_days_between(today, front), 1)

    rejections, warnings = [], []
    if not allowed:
        rejections.append(f"account profile: {why_not}")
    min_credit = spec.entry.get("min_credit")
    if min_credit is not None and fill["modelled"] > 0 and fill["modelled"] < float(min_credit):
        rejections.append(f"option credit ${fill['modelled']:.2f} is below the "
                          f"${float(min_credit):.2f} minimum")

    sized = sizing.max_contracts_for_position(
        bpr_one, account=account,
        legs=[(l.open_interest, l.volume, f"{l.side} {l.strike:g}{l.option_type[0].upper()}")
              for l in options],
        adv_dollars=adv, notional_per_contract=float(ctx.spot) * 100.0)
    contracts = max(int(sized.contracts), 0)
    if sized.rejected:
        rejections.extend(sized.reasons or ("sizing: no contracts",))
    n = max(contracts, 1)

    overrides = dict(request.event_policy_overrides or {}) if request else {}
    if spec.entry.get("earnings", "block") == "allow":
        overrides["earnings"] = {**overrides.get("earnings", {}), "action": "warn"}
    event_hits = ()
    try:
        from data_sources import events
        check = events.check(ctx.ticker, today, max(exps.values()), spec.event_policy_as,
                             calendar_healthy=healthy, overrides=overrides, frame=events_frame)
        if check.blocks:
            rejections.extend(f"event: {t}" for t in check.texts("block"))
        warnings.extend(f"event: {t}" for t in check.texts("warn"))
        event_hits = tuple(h.text() for h in check.hits)
    except Exception as exc:
        warnings.append(f"event check unavailable: {exc}")

    entry_fees = costs.legs_open([("sell" if l.side == "short" else "buy", n * l.qty)
                                  for l in options]).total
    entry_fees += sum(costs.stock_buy(100 * n * l.qty).total
                      for l in legs if l.is_stock and l.side == "long")
    pop = p_max_loss = ev = None
    sample, eff_n = "no empirical sample", 0
    if daily is not None and not daily.empty:
        selected = moves.select_windows(daily, dte_trd, lookback_years=10,
                                        vol_conditioned=True, min_observations=40)
        if selected is not None:
            windows, sample = selected
            s_t = float(ctx.spot) * (1.0 + windows["terminal_return"].astype(float).to_numpy())
            pnl = np.asarray(position.payoff(s_t), dtype=float)
            pop = float(np.mean(pnl > 0))
            if position.max_loss > 0 and spec.margin_class != "naked":
                p_max_loss = float(np.mean(pnl <= -position.max_loss + 1e-6))
            ev = float(np.mean(pnl * 100.0 * n - _expiry_fees(position, s_t, n))) - entry_fees
            eff_n = moves._effective_n(len(windows), dte_trd)
    if ev is not None and ev <= 0:
        rejections.append(f"EV ${ev:,.0f} is not positive on the empirical sample")
    if position.multi_expiry:
        warnings.append("model risk: the later leg is valued at the front expiry by "
                        "Black-Scholes at its entry IV (term structure assumed)")

    greeks = position.net_greeks()
    bpr = bpr_one * n
    front_iv = next((chain_iv.get(id(l)) for l in options
                     if l.side == "short" and chain_iv.get(id(l))), None)
    liq = [l.open_interest for l in options if l.open_interest is not None]
    strikes = [l.strike for l in options]
    trade_id = (f"{spec.id}|{ctx.ticker}|{front}|"
                + "/".join(f"{l.strike:g}{l.option_type[0]}" for l in options)
                + (f"|{root}" if root and root != ctx.ticker else ""))
    return {
        "trade_id": trade_id, "ticker": ctx.ticker, "strategy": spec.id, "label": spec.label,
        "family": spec.family, "margin_class": spec.margin_class,
        "expiration": str(front), "last_expiration": str(max(exps.values())),
        "dte_calendar": dte_cal, "dte_trading": dte_trd, "root_symbol": root,
        "settlement": getattr(ctx, "settlement", "physical"), "spot": float(ctx.spot),
        "legs_json": legs_to_json(legs), "legs": legs_text(legs),
        "strike_reasons": "; ".join(f"{k}: {v}" for k, v in why.items()),
        "strike": max(strikes) if strikes else None, "long_strike": min(strikes) if strikes else None,
        "net_mid": fill["net_mid"] - stock_cost, "natural": fill["natural"] - stock_cost,
        "modelled_fill": credit, "option_credit": fill["modelled"],
        "max_profit": position.max_profit * 100.0 * n,
        "max_loss": position.max_loss * 100.0 * n,
        "breakevens": ", ".join(f"{b:,.2f}" for b in position.breakevens[:4]),
        "bpr_per_contract": bpr_one, "collateral": bpr, "contracts": contracts,
        "binding_constraint": sized.binding_constraint, "entry_fees": entry_fees,
        "prob_otm_empirical": pop, "prob_max_loss": p_max_loss, "expected_value": ev,
        "ev_annualised": (ev / bpr * 365.0 / max(dte_cal, 1)) if ev is not None and bpr else None,
        "return_on_risk": (position.max_profit / position.max_loss)
        if position.max_loss > 0 else None,
        "sample_label": sample, "effective_n": eff_n,
        "net_delta": greeks.get("delta"), "net_theta": greeks.get("theta"),
        "net_vega": greeks.get("vega"), "implied_vol": front_iv,
        "min_open_interest": min(liq) if liq else None,
        "multi_expiry": position.multi_expiry, "events": event_hits,
        "accepted": not rejections, "rejections": tuple(dict.fromkeys(rejections)),
        "warnings": tuple(dict.fromkeys(warnings)), "notes": tuple(spec.notes),
    }
