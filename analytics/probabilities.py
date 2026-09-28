"""
Run the probability engine over a candidate sheet (Phase 13).

`run_sheet(sheet, request)` groups the rows by ticker, builds the H and T
paths (and G's normals) ONCE per ticker and horizon, runs all three models on
every row, and returns:

    sheet      the input with blended headline columns added and re-ranked:
               pop_blend, p_hit_<X>_blend, median_days_<X>_blend,
               p_touch_blend, p_assign_blend / p_max_loss_blend,
               p_roll_blend, headline_policy, headline_ev, headline_days,
               headline_annualised, ev_per_day_bpr (the default rank),
               below_min_gain_targets, per-model pop_G/H/T, effective_n_G/H/T,
               t_flag, prob_labels
    policies   long table: trade_id, model (G/H/T/blend), policy, ev,
               p_profit, days, annualised, ev_per_day_bpr, net_gain_when_hit,
               below_min_gain
    metrics    long table: trade_id, model, metric, value
    curves     long table: trade_id, model, target, day, prob -- P(reach X%
               of max profit by day d), for charts

RANKING: accepted rows sort by the blended `ev_per_day_bpr` of the headline
policy (EV per calendar day held, per dollar of buying power). Since Phase 17
the headline of a put spread is the rule set actually run
(`management.spread`: the target above the hold horizon, the loss stop, the
time stop -- `shipped_rules`), with loss stops triggered on intraday
extremes and H/T leg IVs moving with spot; CSP rows keep the auto policy. With the
request's risk mode `min_pop`, a row whose BLENDED P(profit at expiry) is
under `min_pop` is also rejected (the Phase 12 empirical gate still applies).
Rejected rows sort last. `SORTS` lists the alternative orderings the UI offers.
"""
from __future__ import annotations

import datetime as dt
import time
import zlib

import numpy as np
import pandas as pd

from analytics import prob_engine as pe
from analytics.strategies.base import Leg, Position
from core.progress import BaseReporter, NullReporter

SORTS = {
    "ev_per_day_bpr": "Blended EV per day on buying power (default)",
    "headline_annualised": "Blended annualised return (headline policy)",
    "pop_blend": "Blended P(profit at expiry)",
    "p_hit_50_blend": "Blended P(reach 50% by expiry)",
    "ev_annualised": "Empirical EV annualised (Phase 12)",
    "credit_width": "Credit / width (PCS)",
}


def position_from_row(row: dict) -> Position:
    exp = row["expiration"]
    if row.get("strategy") == "pcs":
        legs = [Leg("put", "short", float(row["strike"]), exp, iv=_f(row.get("implied_vol"))),
                Leg("put", "long", float(row["long_strike"]), exp,
                    iv=_f(row.get("long_iv")) or _f(row.get("implied_vol")))]
        return Position("pcs", row["ticker"], legs, float(row["modelled_fill"]))
    leg = Leg("put", "short", float(row["strike"]), exp, iv=_f(row.get("implied_vol")))
    return Position("csp", row["ticker"], [leg], float(row["modelled_fill"]),
                    collateral_per_contract=float(row["strike"]) * 100.0)


def _f(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if np.isfinite(value) and value > 0 else None


def _technical_frame(ticker: str, daily: pd.DataFrame) -> tuple[pd.DataFrame | None, str | None]:
    """Indicator frame for model T and the column of today's strongest
    respected support (weekly levels need the weekly indicators)."""
    from analytics import indicators, level_respect, technical_study
    column = None
    try:
        support = technical_study.load_support(ticker)
        strong = support[support["strong"].fillna(False).astype(bool)]
        if not strong.empty:
            level_id = strong.sort_values("edge_ci_lo", ascending=False).iloc[0]["level_id"]
            mapping = {lid: col for lid, _, col in
                       level_respect.level_columns(level_respect.Params.from_config())}
            column = mapping.get(level_id)
    except Exception:
        column = None
    try:
        frame = indicators.for_symbol(ticker) if column and column.startswith("w_") \
            else indicators.compute(daily)
        return frame, column
    except Exception:
        return None, None


def shipped_rules(row: dict) -> dict | None:
    """The management rules a sheet row is actually run under, for the
    engine's `managed` policy (Phase 17). Put spreads follow
    `exit_rules.spread_config()`: the profit target only above the hold
    horizon, the loss stop always, the time stop when entered above it. CSPs
    are held and rolled, not stopped: None (the auto headline)."""
    if row.get("strategy") != "pcs":
        return None
    from analytics.exit_rules import spread_config
    cfg = spread_config()
    dte = int(row.get("dte_calendar") or 0)
    target = cfg.get("profit_target_pct")
    return {"target": int(target) if target and dte > int(cfg["hold_max_dte"]) else None,
            "stop": cfg.get("loss_stop_multiple"),
            "time_stop": cfg.get("time_stop_dte")}


def _next_earnings(ticker: str, today: dt.date, events_frame: pd.DataFrame) -> dt.date | None:
    if events_frame is None or events_frame.empty:
        return None
    rows = events_frame[(events_frame["symbol"] == ticker) & (events_frame["type"] == "earnings")
                        & (events_frame["date"] > today)]
    return min(rows["date"]) if not rows.empty else None


def run_sheet(sheet: pd.DataFrame, request=None, cfg: pe.EngineConfig | None = None,
              today: dt.date | None = None,
              reporter: BaseReporter | None = None) -> dict:
    from core.market_calendar import ET
    from data_sources import events
    from data_sources.yfinance_sync import load_daily

    cfg = cfg or pe.EngineConfig.from_config()
    reporter = reporter or NullReporter()
    today = today or dt.datetime.now(ET).date()
    targets = sorted(set(int(t) for t in (request.profit_targets if request else [25, 50, 100])))
    empty = {"sheet": sheet, "policies": pd.DataFrame(), "metrics": pd.DataFrame(),
             "curves": pd.DataFrame(), "seconds": 0.0}
    if sheet is None or sheet.empty:
        return empty
    started = time.perf_counter()
    events_frame = events.load()
    rows = sheet.to_dict("records")
    blended_rows: list[dict] = []
    policy_rows, metric_rows, curve_rows = [], [], []

    tickers = list(dict.fromkeys(r["ticker"] for r in rows))
    spy = load_daily("SPY", basis="price") if cfg.spot_vol else None
    with reporter.stage("probabilities", "Probability engine", total=len(tickers)):
        for ticker in tickers:
            mine = [r for r in rows if r["ticker"] == ticker]
            daily = load_daily(ticker, basis="price")
            beta = pe.name_spot_vol_beta(daily, cfg, spy)
            tech, support_column = _technical_frame(ticker, daily)
            earnings = _next_earnings(ticker, today, events_frame)
            rv = None
            if len(daily) > 21:
                closes = daily["close"].astype(float).tail(21).to_numpy()
                rv = float(np.std(np.diff(np.log(closes)), ddof=1) * np.sqrt(252))
            cache: dict[int, pe.TickerPaths] = {}
            for row in mine:
                steps = max(int(row.get("dte_trading") or 1), 1)
                if steps not in cache:
                    seed = cfg.seed + zlib.crc32(f"{ticker}|{steps}".encode())
                    cache[steps] = pe.ticker_paths(daily, steps, cfg,
                                                   np.random.default_rng(seed), tech,
                                                   support_column)
                exp_date = pd.Timestamp(row["expiration"]).date()
                event_day = (earnings - today).days if earnings and earnings <= exp_date else None
                rules = shipped_rules(row)
                spec = pe.TradeSpec(
                    position=position_from_row(row), spot=float(row["spot"]),
                    dte_calendar=max(int(row["dte_calendar"]), 1), dte_trading=steps,
                    contracts=max(int(row.get("contracts") or 0), 1),
                    bpr=float(row["collateral"]),
                    cash_settled=row.get("settlement") == "cash",
                    event_day=event_day, rv=rv,
                    loss_stop_multiple=(rules or {}).get("stop"), managed=rules,
                    spot_vol_beta=beta)
                result = pe.run_trade(spec, cache[steps], targets, cfg)
                blended_rows.append(_summarise(row, result, spec, cfg, targets, request))
                _collect(row["trade_id"], result, policy_rows, metric_rows, curve_rows)
            reporter.advance(1, note=f"{ticker} {len(mine)} trade(s)")

    out = pd.DataFrame(blended_rows)
    out["rank_key"] = np.where(out["accepted"], out["ev_per_day_bpr"].fillna(-1e6), -1e9)
    out = out.sort_values("rank_key", ascending=False).reset_index(drop=True)
    out.attrs.update(sheet.attrs)
    seconds = time.perf_counter() - started
    return {"sheet": out, "policies": pd.DataFrame(policy_rows),
            "metrics": pd.DataFrame(metric_rows), "curves": pd.DataFrame(curve_rows),
            "seconds": seconds, "trades": len(rows), "tickers": len(tickers)}


def _summarise(row: dict, result: dict, spec: pe.TradeSpec, cfg: pe.EngineConfig,
               targets: list[int], request) -> dict:
    out = dict(row)
    blend = result["blend"]
    for key, value in blend.items():
        if key in ("weights", "policies", "managed_policy"):
            continue
        name = {"p_touch_short": "p_touch", "p_roll_trigger": "p_roll",
                "p_short_itm": "p_short_itm"}.get(key, key)
        out[f"{name}_blend"] = value
    for model, meta in result["meta"].items():
        out[f"effective_n_{model}"] = meta.get("effective_n")
        r = result["models"].get(model)
        out[f"pop_{model}"] = r.get("pop") if r else None
    out["t_flag"] = result["meta"]["T"].get("flag", "")
    out["prob_labels"] = " | ".join(f"{m}: {meta['label']}" for m, meta in result["meta"].items())
    policies = blend.get("policies", {})
    head = pe.headline_policy(policies, spec.dte_calendar, cfg,
                              pe.managed_policy_name(spec.managed, spec.dte_calendar))
    chosen = policies.get(head, {})
    out["spot_vol_beta"] = spec.spot_vol_beta
    out["headline_p_stopped"] = chosen.get("p_stopped")
    out.update({"headline_policy": head, "headline_ev": chosen.get("ev"),
                "headline_p_profit": chosen.get("p_profit"),
                "headline_days": chosen.get("days"),
                "headline_annualised": chosen.get("annualised"),
                "ev_per_day_bpr": chosen.get("ev_per_day_bpr")})
    below = [k.split("_")[1] + "%" for k, v in policies.items()
             if k.startswith("close_") and k.count("_") == 1 and v.get("below_min_gain")]
    out["below_min_gain_targets"] = ", ".join(below)
    if request is not None and request.risk_mode == "min_pop" and request.min_pop is not None:
        pop = out.get("pop_blend")
        if pop is not None and pop < request.min_pop:
            reasons = list(out.get("rejections") or ())
            reasons.append(f"blended P(profit) {pop:.0%} is below the requested "
                           f"{request.min_pop:.0%}")
            out["rejections"] = tuple(reasons)
            out["accepted"] = False
    return out


def _collect(trade_id: str, result: dict, policy_rows, metric_rows, curve_rows) -> None:
    for model, r in list(result["models"].items()) + [("blend", result["blend"])]:
        for name, policy in (r.get("policies") or {}).items():
            policy_rows.append({"trade_id": trade_id, "model": model, "policy": name,
                                **{k: policy.get(k) for k in (
                                    "ev", "p_profit", "days", "annualised", "ev_per_day_bpr",
                                    "net_gain_when_hit", "below_min_gain", "p_stopped")}})
        for key, value in r.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and key != "n_paths":
                metric_rows.append({"trade_id": trade_id, "model": model, "metric": key,
                                    "value": float(value)})
        if model == "blend":
            continue
        days = r.get("curve_days")
        for target, curve in (r.get("curves") or {}).items():
            # every 5th point plus the last keeps the table small
            idx = sorted(set(range(0, len(curve), 5)) | {len(curve) - 1})
            for i in idx:
                curve_rows.append({"trade_id": trade_id, "model": model, "target": int(target),
                                   "day": float(days[i]), "prob": float(curve[i])})
