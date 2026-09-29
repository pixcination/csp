"""
The multi-strategy recommender (Phase 16, roadmap C.8 task 3).

For each ticker with a stored chain:

1. **Conditions.** IV regime (low / mid / high from the TastyTrade IV
   percentile, clamped, with hysteresis against the previous snapshot --
   Phase 17, `strategy_spec.iv_regime`), the trend condition, and whether an
   earnings date falls inside each spec's trade window. Since Phase 20 the
   trend condition is read from the OUTLOOK's Direction dial at the spec's
   nearest expiration role (>= 6 uptrend, <= 4 downtrend, else range),
   skill-shrunk: where Direction has no walk-forward skill the dial sits at 5
   and the condition is `range`, so only specs that accept a range apply --
   no directional structure is chosen on a direction nobody can forecast.
   `outlook.recommender_source: trend_state` restores the Phase 10 technical
   trend state; a ticker without an Outlook row falls back to it.
2. **Applicable strategies.** Every spec whose `entry` conditions the ticker
   meets (the condition matrix is those conditions laid out -- see
   `strategy_spec.condition_matrix`). With explicit `spec_ids` the specs are
   resolved whatever the conditions, and a row says whether they were met:
   "pick one spec and scan the universe for its best instances".
3. **Resolve** each spec on the chain (`strategies.resolver`).
4. **Probability engine** (G / H / T and the blend) on every resolved
   position -- generic over `Position`, so calendars and stock legs go
   through the same repricing core. The headline policy is the spec's own
   profit target when it has one, else the engine's auto policy.
5. **Rank** by the headline blended EV per day on buying power, accepted
   rows whose conditions are met first and, within them, rows in a
   preferred IV regime before the rest (the regime is soft since Phase 17).
   Naked specs on a profile with `naked_research_only` are flagged
   `research_only`: shown for comparison, never auto-tracked. That is the same "risk-adjusted EV"
   the CSP/PCS sheet ranks on: dollars per day per dollar of BPR tied up.

Nothing here is calibrated per strategy yet. The engine was validated on
short puts (Phase 13) and put spreads (Phase 15); calendars and diagonals
carry an unvalidated term-structure assumption, flagged on their rows.
"""
from __future__ import annotations

import datetime as dt
import time
import zlib

import numpy as np
import pandas as pd

from analytics import prob_engine as pe
from analytics import sizing, strategy_spec
from analytics.strategies import resolver
from core.paths import load_config
from core.progress import BaseReporter, NullReporter

TARGETS = [25, 50, 100]


def _conditions(ticker: str, today: dt.date, metrics: dict, latest: pd.DataFrame,
                events_frame: pd.DataFrame | None, horizon_days: int,
                previous: dict | None = None, outlook_table: pd.DataFrame | None = None,
                outlook_days: int | None = None) -> dict:
    ivr = metrics.get("ivr")
    rcfg = strategy_spec.regime_config()
    value, measure = strategy_spec.regime_value(metrics, rcfg)
    prior_value, _ = strategy_spec.regime_value(previous, rcfg) if previous else (None, None)
    prior = strategy_spec.iv_regime(prior_value, cfg=rcfg)
    trend = None
    if latest is not None and not latest.empty and "symbol" in latest:
        mine = latest[latest["symbol"] == ticker]
        if not mine.empty and "trend_state" in mine:
            state = mine["trend_state"].iloc[0]
            trend = state if isinstance(state, str) else None
    trend_source, direction, direction_conf = "trend_state", None, None
    if outlook_table is not None and not outlook_table.empty and outlook_days:
        from analytics import outlook
        rec = outlook.at(outlook_table, ticker, outlook_days)
        if rec and rec.get("direction") is not None:
            direction, direction_conf = rec["direction"], rec.get("direction_conf")
            trend, trend_source = outlook.trend_class(direction), "outlook"
    earnings = None
    if events_frame is not None and not events_frame.empty:
        rows = events_frame[(events_frame["symbol"] == ticker)
                            & (events_frame["type"] == "earnings")
                            & (events_frame["date"] >= today)]
        earnings = min(rows["date"]) if not rows.empty else None
    return {"ivr": ivr, "ivp": metrics.get("ivp"), "iv_value": value, "iv_measure": measure,
            "iv_regime": strategy_spec.iv_regime(value, prior, rcfg),
            "iv_regime_previous": prior, "trend": trend, "trend_source": trend_source,
            "outlook_direction": direction, "outlook_direction_conf": direction_conf,
            "next_earnings": earnings,
            "earnings_in_window": bool(earnings and (earnings - today).days <= horizon_days)}


def _front_days(spec) -> int:
    """The spec's nearest expiration role's target: the horizon its trend
    condition is read at."""
    return min(int(e["dte_target"]) for e in spec.expirations.values())


def _outlook_table() -> pd.DataFrame | None:
    from analytics import outlook
    if (outlook.cfg().get("recommender_source") or "outlook") != "outlook":
        return None
    try:
        return outlook.load_latest()
    except Exception:
        return None


def _horizon(spec) -> int:
    """Calendar days the spec's latest leg can run: its last role's target + tolerance."""
    return max(int(e["dte_target"]) + int(e.get("tolerance", 14))
               for e in spec.expirations.values())


def spec_rules(spec) -> dict | None:
    """The spec's own exit block as the engine's `managed` rules (Phase 17):
    target, loss stop (x credit, or x debit for a debit trade), time stop."""
    ex = spec.exit or {}
    rules = {"target": ex.get("profit_target_pct"), "stop": ex.get("loss_stop_multiple"),
             "time_stop": ex.get("time_stop_dte")}
    return rules if any(v for v in rules.values()) else None


def _headline(spec, policies: dict, dte: int, cfg: pe.EngineConfig) -> str:
    managed = pe.managed_policy_name(spec_rules(spec), dte)
    if cfg.headline_policy == "shipped" and managed in policies:
        return managed
    target = spec.exit.get("profit_target_pct")
    if target and f"close_{int(target)}" in policies:
        return f"close_{int(target)}"
    return pe.headline_policy(policies, dte, cfg)


def _probabilities(rows: list[dict], specs: dict, daily: pd.DataFrame, ticker: str,
                   cfg: pe.EngineConfig, today: dt.date, earnings: dt.date | None) -> None:
    """Run the engine on each row in place (blend columns + headline)."""
    from analytics.probabilities import _technical_frame
    tech, support_column = _technical_frame(ticker, daily)
    beta = pe.name_spot_vol_beta(daily, cfg)
    rv = None
    if len(daily) > 21:
        closes = daily["close"].astype(float).tail(21).to_numpy()
        rv = float(np.std(np.diff(np.log(closes)), ddof=1) * np.sqrt(252))
    cache: dict[int, pe.TickerPaths] = {}
    for row in rows:
        spec = specs[row["strategy"]]
        targets = sorted(set(TARGETS + [int(t) for t in [spec.exit.get("profit_target_pct")]
                                         if t]))
        steps = max(int(row["dte_trading"]), 1)
        if steps not in cache:
            seed = cfg.seed + zlib.crc32(f"{ticker}|{steps}".encode())
            cache[steps] = pe.ticker_paths(daily, steps, cfg, np.random.default_rng(seed),
                                           tech, support_column)
        front = pd.Timestamp(row["expiration"]).date()
        event_day = (earnings - today).days if earnings and earnings <= front else None
        position = resolver.position_from_row(row)
        spec_t = pe.TradeSpec(position=position, spot=float(row["spot"]),
                              dte_calendar=max(int(row["dte_calendar"]), 1), dte_trading=steps,
                              contracts=max(int(row["contracts"]), 1),
                              bpr=float(row["bpr_per_contract"]) * max(int(row["contracts"]), 1),
                              cash_settled=row.get("settlement") == "cash",
                              event_day=event_day, rv=rv,
                              loss_stop_multiple=spec.exit.get("loss_stop_multiple"),
                              managed=spec_rules(spec), spot_vol_beta=beta)
        result = pe.run_trade(spec_t, cache[steps], targets, cfg)
        blend = result["blend"]
        for key in ("pop", "p_touch_short", "p_short_itm", "p_max_loss"):
            if blend.get(key) is not None:
                row[f"{key}_blend"] = blend[key]
        for x in targets:
            row[f"p_hit_{x}_blend"] = blend.get(f"p_hit_{x}")
        policies = blend.get("policies", {})
        head = _headline(spec, policies, spec_t.dte_calendar, cfg)
        chosen = policies.get(head, {})
        row.update({"headline_policy": head, "headline_ev": chosen.get("ev"),
                    "headline_p_stopped": chosen.get("p_stopped"),
                    "headline_days": chosen.get("days"),
                    "headline_annualised": chosen.get("annualised"),
                    "ev_per_day_bpr": chosen.get("ev_per_day_bpr"),
                    "models": ", ".join(f"{m}: n~{result['meta'][m]['effective_n']}"
                                        + (f" ({result['meta'][m]['flag']})"
                                           if result['meta'][m].get('flag') else "")
                                        for m in result["meta"])})


def run(tickers: list[str], request=None, spec_ids: list[str] | None = None,
        recommend: bool = True, n_paths: int | None = None, today: dt.date | None = None,
        reporter: BaseReporter | None = None, chain_loader=None, specs: dict | None = None
        ) -> dict:
    """Recommend (or scan for) strategies across `tickers`.

    `spec_ids`: resolve these specs on every ticker whatever the conditions
    (rows flag `conditions_met`). None with `recommend`: every spec whose
    conditions a ticker meets. Returns {"sheet", "conditions", "seconds"}.
    """
    from analytics.scan_request import ScanRequest
    from analytics.strategies.context import TickerContext
    from analytics import technical_study
    from core.market_calendar import ET
    from data_sources import events, tasty_metrics
    from data_sources.yfinance_sync import load_daily

    request = request or ScanRequest.default()
    reporter = reporter or NullReporter()
    today = today or dt.datetime.now(ET).date()
    specs = specs or strategy_spec.load_all()
    wanted = {k: v for k, v in specs.items() if not spec_ids or k in spec_ids}
    unknown = sorted(set(spec_ids or []) - set(specs))
    if unknown:
        raise ValueError(f"unknown strategy spec(s): {', '.join(unknown)}")
    rcfg = load_config().get("recommender", {}) or {}
    cfg = pe.EngineConfig.from_config()
    cfg.n_paths = int(n_paths or rcfg.get("n_paths", cfg.n_paths))
    if chain_loader is None:
        from data_sources import chains as _chains

        def chain_loader(t):
            chain, under = _chains.load_chain(t, complete=True)
            return chain, _chains.spot_from_underlying(under)

    account = sizing.account_from_config(request.account_profile,
                                         position_pct_override=request.max_pct_capital)
    events_frame = events.load()
    try:
        healthy = events.earnings_health()["healthy"] if not events_frame.empty else False
    except Exception:
        healthy = False
    metrics = {r["symbol"]: r for r in tasty_metrics.latest(tickers).to_dict("records")} \
        if tickers else {}
    try:
        previous = {r["symbol"]: r for r in
                    tasty_metrics.previous(tickers).to_dict("records")} if tickers else {}
    except Exception:
        previous = {}
    profile_cfg = sizing.account_config(request.account_profile)
    naked_research_only = bool(profile_cfg.get("naked_research_only", False))
    latest = technical_study.load_latest()
    outlook_table = _outlook_table()
    started = time.perf_counter()
    rows, conditions = [], []
    with reporter.stage("strategies", "Strategy recommender", total=len(tickers)):
        for ticker in tickers:
            chain, spot = chain_loader(ticker)
            if chain is None or chain.empty or not spot:
                conditions.append({"ticker": ticker, "note": "no stored chain"})
                reporter.advance(1, note=f"{ticker}: no chain")
                continue
            ctx = TickerContext.build(ticker, chain, spot, today, metrics.get(ticker))
            daily = load_daily(ticker, basis="price")
            adv = None
            if len(daily) > 20:
                tail = daily.tail(20)
                adv = float((tail["close"].astype(float) * tail["volume"].astype(float)).mean())
            mine = []
            for sid, spec in wanted.items():
                cond = _conditions(ticker, today, metrics.get(ticker) or {}, latest,
                                   events_frame, _horizon(spec), previous.get(ticker),
                                   outlook_table, _front_days(spec))
                met, why = strategy_spec.applies(spec, cond)
                fit = strategy_spec.regime_fit(spec, cond["iv_regime"])
                conditions.append({"ticker": ticker, "strategy": sid, "iv_regime": cond["iv_regime"],
                                   "iv_value": cond["iv_value"], "iv_measure": cond["iv_measure"],
                                   "iv_regime_previous": cond["iv_regime_previous"],
                                   "regime_fit": fit,
                                   "ivr": cond["ivr"], "ivp": cond["ivp"], "trend": cond["trend"],
                                   "trend_source": cond["trend_source"],
                                   "outlook_direction": cond["outlook_direction"],
                                   "outlook_direction_conf": cond["outlook_direction_conf"],
                                   "earnings_in_window": cond["earnings_in_window"],
                                   "applies": met, "why_not": "; ".join(why)})
                if not met and not spec_ids:
                    continue
                resolved, reasons = resolver.resolve(spec, ctx, request, account,
                                                     today=today, daily=daily, adv_dollars=adv,
                                                     events_frame=events_frame,
                                                     calendar_healthy=healthy)
                if not resolved:
                    conditions[-1]["unresolved"] = "; ".join(reasons)
                for row in resolved:
                    row.update({"conditions_met": met, "conditions": "; ".join(why) or "met",
                                "iv_regime": cond["iv_regime"], "ivr": cond["ivr"],
                                "ivp": cond["ivp"], "iv_value": cond["iv_value"],
                                "regime_fit": fit is not False,
                                "research_only": bool(spec.margin_class == "naked"
                                                      and naked_research_only),
                                "trend_state": cond["trend"],
                                "trend_source": cond["trend_source"],
                                "outlook_direction": cond["outlook_direction"]})
                mine.extend(resolved)
            if mine:
                earnings = next((c.get("next_earnings") for c in [
                    _conditions(ticker, today, {}, None, events_frame, 0)]), None)
                _probabilities(mine, specs, daily, ticker, cfg, today, earnings)
            rows.extend(mine)
            reporter.advance(1, note=f"{ticker}: {len(mine)} position(s)")
    sheet = pd.DataFrame(rows)
    if not sheet.empty:
        score = sheet["ev_per_day_bpr"].astype(float).fillna(-1e6)
        fit = sheet["regime_fit"].astype(bool) if "regime_fit" in sheet \
            else pd.Series(True, index=sheet.index)
        met = sheet["accepted"] & sheet["conditions_met"]
        # Tiers: conditions met in a preferred regime, then met outside it
        # (soft regime), then accepted without the conditions, then rejected.
        sheet["rank_key"] = np.where(met & fit, score,
                                     np.where(met, score - 1e5,
                                              np.where(sheet["accepted"], score - 1e6, -1e9)))
        sheet = sheet.sort_values("rank_key", ascending=False).reset_index(drop=True)
    return {"sheet": sheet, "conditions": pd.DataFrame(conditions),
            "seconds": time.perf_counter() - started, "n_paths": cfg.n_paths}
