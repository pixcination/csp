"""
The multi-strategy recommender (Phase 16, roadmap C.8 task 3).

For each ticker with a stored chain:

1. **Conditions.** IV regime (low / mid / high from the TastyTrade IV rank,
   `recommender.iv_regime` thresholds), trend state (Phase 10, from the
   technicals cache), and whether an earnings date falls inside each spec's
   trade window.
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
   rows whose conditions are met first. That is the same "risk-adjusted EV"
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
                events_frame: pd.DataFrame | None, horizon_days: int) -> dict:
    ivr = metrics.get("ivr")
    trend = None
    if latest is not None and not latest.empty and "symbol" in latest:
        mine = latest[latest["symbol"] == ticker]
        if not mine.empty and "trend_state" in mine:
            value = mine["trend_state"].iloc[0]
            trend = value if isinstance(value, str) else None
    earnings = None
    if events_frame is not None and not events_frame.empty:
        rows = events_frame[(events_frame["symbol"] == ticker)
                            & (events_frame["type"] == "earnings")
                            & (events_frame["date"] >= today)]
        earnings = min(rows["date"]) if not rows.empty else None
    return {"ivr": ivr, "iv_regime": strategy_spec.iv_regime(ivr), "trend": trend,
            "next_earnings": earnings,
            "earnings_in_window": bool(earnings and (earnings - today).days <= horizon_days)}


def _horizon(spec) -> int:
    """Calendar days the spec's latest leg can run: its last role's target + tolerance."""
    return max(int(e["dte_target"]) + int(e.get("tolerance", 14))
               for e in spec.expirations.values())


def _headline(spec, policies: dict, dte: int, cfg: pe.EngineConfig) -> str:
    target = spec.exit.get("profit_target_pct")
    if target and f"close_{int(target)}" in policies:
        return f"close_{int(target)}"
    return pe.headline_policy(policies, dte, cfg)


def _probabilities(rows: list[dict], specs: dict, daily: pd.DataFrame, ticker: str,
                   cfg: pe.EngineConfig, today: dt.date, earnings: dt.date | None) -> None:
    """Run the engine on each row in place (blend columns + headline)."""
    from analytics.probabilities import _technical_frame
    tech, support_column = _technical_frame(ticker, daily)
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
                              event_day=event_day, rv=rv)
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
            chain, under = _chains.load_chain(t)
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
    latest = technical_study.load_latest()
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
                                   events_frame, _horizon(spec))
                met, why = strategy_spec.applies(spec, cond)
                conditions.append({"ticker": ticker, "strategy": sid, "iv_regime": cond["iv_regime"],
                                   "ivr": cond["ivr"], "trend": cond["trend"],
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
                                "trend_state": cond["trend"]})
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
        sheet["rank_key"] = np.where(sheet["accepted"] & sheet["conditions_met"], score,
                                     np.where(sheet["accepted"], score - 1e6, -1e9))
        sheet = sheet.sort_values("rank_key", ascending=False).reset_index(drop=True)
    return {"sheet": sheet, "conditions": pd.DataFrame(conditions),
            "seconds": time.perf_counter() - started, "n_paths": cfg.n_paths}
