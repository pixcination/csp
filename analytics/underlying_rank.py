"""
Underlying ranking -- which names deserve a chain pull (Phase 11).

Pulling 30-60 DTE chains for the whole registry does not fit the dxFeed
budget, and most of the universe is not worth pricing on any given day.
This scores every registry symbol from data already on disk -- no chains --
and the pipeline pulls chains for the top N only.

COMPONENTS (each scored 0-1; weights from the request's `ranking_weights`:
a preset in config.yaml -> underlying_rank.weight_presets or the Settings
page, or the request's own mapping)
----------------------------------------------------------------------
    iv_rank    mean of TastyTrade IV rank and IV percentile
    iv_rv      IV at the request's DTE (TastyTrade per-expiration IV, else
               the IV index) / realised vol over the window
               `vrp.match_rv_window_to_dte` pairs with that DTE -- the same
               horizon match the CSP entry gate uses. Linear between
               iv_rv_floor (0) and iv_rv_cap (1). TastyTrade's 30-day HV is
               the fallback when bars are missing.
    liquidity  half the TastyTrade liquidity rating / 5, half a fixed log
               scale of its liquidity-value (log10 between
               liquidity_value_log10 bounds). The rating alone puts 45 of 67
               names at 3 or 4; the value spans five orders of magnitude.
    trend      Phase 10 trend state: uptrend / range / downtrend
    support    nearest STRONG level below spot (Phase 10), in expected-move
               units at the request's DTE. 1 inside support_em_band, rising
               linearly to it from spot, decaying to 0 at twice its upper end.
               0 when no level is strong. Phase 10 found MA support mostly
               chance in this universe, hence the low weight.
    drawdown   max drawdown over stage1 drawdown_lookback_years; 0 at
               drawdown_floor

A component with no data is dropped and the rest renormalised, so a name is
not punished for a missing feed; `coverage` reports the share of total weight
that had data. Every row keeps its component breakdown. **Calibration (Phase
14, analytics/rank_calibration.py):** point-in-time over 8 years and 67 names,
only iv_rank (through an RV-percentile proxy) and weakly drawdown predicted the
share of premium a 1-EM short put kept; trend and support ICs were ~0; iv_rv
and liquidity cannot be tested. The `calibrated` preset follows that; the
default preset is unchanged pending Tom's decision.

GATES, NOT PENALTIES
--------------------
Two things exclude a name outright rather than lowering its score:

* **Events.** The request's event policy over the trade window. If a blocking
  event (earnings, for stocks) falls before the *shortest* requested
  expiration, every expiration is blocked -> excluded. If it falls inside the
  window, the name stays in with `event_status = partial` and the latest
  clear DTE noted, because the shorter expirations are still tradable.
* **Capital.** The account profile's permissions and per-position cap. A CSP
  needs roughly (spot - 1 EM) x 100 of collateral under the cap; a PCS needs
  the narrowest requested width x 100 under it, and spread approval. A name
  no requested strategy can trade is excluded.

Stage 1 stays advisory (Phase 9): its tier is carried as a column.
"""
from __future__ import annotations

import datetime as dt
import json
import math

import numpy as np
import pandas as pd

from analytics.scan_request import ScanRequest, resolve_universe, strategies_for
from core.market_calendar import ET
from core.paths import load_config

COMPONENTS = ("iv_rank", "iv_rv", "liquidity", "trend", "support", "drawdown")


# --- Component scores (pure) ---------------------------------------------------

def _clip01(x: float) -> float:
    return float(min(max(x, 0.0), 1.0))


def score_iv_rank(ivr: float | None, ivp: float | None) -> float | None:
    values = [v for v in (ivr, ivp) if v is not None and np.isfinite(v)]
    return _clip01(sum(values) / len(values)) if values else None


def score_iv_rv(ratio: float | None, floor: float, cap: float) -> float | None:
    if ratio is None or not np.isfinite(ratio):
        return None
    return _clip01((ratio - floor) / (cap - floor))


def score_liquidity(rating: float | None, value: float | None = None,
                    log_bounds: tuple[float, float] = (2.0, 5.5)) -> float | None:
    parts = []
    if rating is not None and np.isfinite(rating):
        parts.append(_clip01(float(rating) / 5.0))
    if value is not None and np.isfinite(value) and value > 0:
        lo, hi = log_bounds
        parts.append(_clip01((math.log10(value) - lo) / (hi - lo)))
    return sum(parts) / len(parts) if parts else None


def score_trend(state: str | None, table: dict) -> float | None:
    if not state or state not in table:
        return None
    return float(table[state])


def score_support(distance_em: float | None, band: tuple[float, float],
                  studied: bool) -> float | None:
    """distance_em of the nearest strong level (None = no strong level)."""
    if not studied:
        return None
    if distance_em is None or not np.isfinite(distance_em):
        return 0.0
    lo, hi = band
    if distance_em < lo:
        return _clip01(distance_em / lo) if lo > 0 else 1.0
    if distance_em <= hi:
        return 1.0
    return _clip01(1.0 - (distance_em - hi) / hi)


def score_drawdown(max_drawdown: float | None, floor: float) -> float | None:
    if max_drawdown is None or not np.isfinite(max_drawdown):
        return None
    return _clip01(1.0 - max_drawdown / floor)


def composite(scores: dict[str, float | None], weights: dict[str, float]) -> tuple[float | None, float]:
    """(weighted score over the components with data, coverage of total weight)."""
    total = sum(weights.get(k, 0.0) for k in COMPONENTS)
    have = {k: v for k, v in scores.items() if v is not None and weights.get(k, 0.0) > 0}
    used = sum(weights[k] for k in have)
    if not used:
        return None, 0.0
    return sum(weights[k] * v for k, v in have.items()) / used, used / total if total else 0.0


# --- Inputs --------------------------------------------------------------------

def expected_move(spot: float | None, iv: float | None, dte: float) -> float | None:
    if not spot or not iv or not np.isfinite(iv) or iv <= 0:
        return None
    return float(spot) * float(iv) * math.sqrt(max(dte, 1.0) / 365.0)


def iv_near_dte(metrics: dict, dte: float, today: dt.date) -> float | None:
    """TastyTrade per-expiration IV nearest the reference DTE, else the IV index."""
    try:
        exps = json.loads(metrics.get("expirations_json") or "[]")
    except (TypeError, ValueError):
        exps = []
    best, best_gap = None, None
    for e in exps:
        try:
            days = (dt.date.fromisoformat(e["expiration"]) - today).days
            iv = float(e["iv"]) if e.get("iv") is not None else None
        except (KeyError, TypeError, ValueError):
            continue
        if iv is None or not np.isfinite(iv) or iv <= 0 or days < 1:
            continue
        gap = abs(days - dte)
        if best_gap is None or gap < best_gap:
            best, best_gap = iv, gap
    if best is not None:
        return best
    value = metrics.get("iv_index")
    return float(value) if value is not None and np.isfinite(value) else None


def _event_status(symbol: str, strategies: list[str], asset_class: str,
                  request: ScanRequest, today: dt.date, events_frame, healthy: bool) -> dict:
    """Blocked for every expiration / partial / warn / ok, per the request's policy."""
    from data_sources import events

    lo, hi = request.dte_window()
    per_strategy = {}
    notes: list[str] = []
    for strategy in strategies:
        short = events.check(symbol, today, today + dt.timedelta(days=lo), strategy,
                             asset_class=asset_class, calendar_healthy=healthy,
                             overrides=request.event_policy_overrides, frame=events_frame)
        full = events.check(symbol, today, today + dt.timedelta(days=hi), strategy,
                            asset_class=asset_class, calendar_healthy=healthy,
                            overrides=request.event_policy_overrides, frame=events_frame)
        if short.blocks:
            status = "blocked"
            notes.extend(short.texts("block"))
        elif full.blocks:
            status = "partial"
            first = min(h.date for h in full.hits if h.action == "block")
            clear = max((first - today).days - 1, 0)
            notes.append(f"{strategy}: expirations after {first} blocked "
                         f"(clear to ~{clear} DTE): " + "; ".join(full.texts("block"))[:160])
        elif full.action == "warn":
            status = "warn"
            notes.extend(full.texts("warn"))
        else:
            status = "ok"
        per_strategy[strategy] = status
    order = ["ok", "warn", "partial", "blocked"]
    # A name is only as blocked as its LEAST blocked requested strategy.
    best = min(per_strategy.values(), key=order.index) if per_strategy else "ok"
    return {"event_status": best, "event_by_strategy": per_strategy,
            "event_notes": "; ".join(dict.fromkeys(notes))[:400]}


def _capital(strategies: list[str], spot: float | None, em: float | None,
             request: ScanRequest, account_cfg: dict) -> tuple[list[str], list[str]]:
    """(feasible strategies, reasons for the infeasible ones)."""
    nlv = float(account_cfg.get("net_liquidating_value", 0.0))
    pct = (request.max_pct_capital if request.max_pct_capital is not None
           else float(account_cfg.get("max_collateral_per_position_pct", 0.04)))
    cap = nlv * pct
    if request.max_loss_per_trade is not None:
        cap = min(cap, float(request.max_loss_per_trade))
    allowed = set(account_cfg.get("allowed_strategies") or ["csp", "pcs"])
    feasible, reasons = [], []
    for strategy in strategies:
        if strategy not in allowed:
            reasons.append(f"{strategy} not allowed in profile '{request.account_profile}'")
            continue
        if strategy == "csp":
            if spot is None:
                reasons.append("csp: no spot price")
                continue
            strike = max(spot - (em or 0.0), spot * 0.5)
            if strike * 100 > cap:
                reasons.append(f"csp: one contract ~${strike * 100:,.0f} collateral "
                               f"exceeds the ${cap:,.0f} per-position limit")
                continue
        if strategy == "pcs":
            if not account_cfg.get("spread_approval", True):
                reasons.append(f"pcs: profile '{request.account_profile}' has no "
                               f"spread approval")
                continue
            width = min(request.spread_widths) if request.spread_widths else 1.0
            if width * 100 > cap:
                reasons.append(f"pcs: ${width:g} width max loss exceeds the "
                               f"${cap:,.0f} per-position limit")
                continue
        feasible.append(strategy)
    return feasible, reasons


# --- Ranking -------------------------------------------------------------------

def rank(request: ScanRequest | None = None, symbols: list[str] | None = None,
         held: set[str] | None = None, today: dt.date | None = None) -> pd.DataFrame:
    """Score every symbol in the request's universe. One row per symbol, all
    components kept, sorted by rank (excluded names last). `selected` marks
    the top `request.top_n_underlyings` eligible names."""
    from analytics import sizing, technical_study, universe_screen
    from data_sources import events, tasty_metrics, universe
    from data_sources.yfinance_sync import load_daily

    request = request or ScanRequest.default()
    today = today or dt.datetime.now(ET).date()
    cfg = load_config()
    rcfg = cfg.get("underlying_rank", {}) or {}
    weights = request.weights()
    trend_table = rcfg.get("trend_scores") or {"uptrend": 1.0, "range": 0.6, "downtrend": 0.1}
    band = tuple(rcfg.get("support_em_band", [0.5, 2.0]))
    dd_years = (cfg.get("stage1_thresholds") or {}).get("drawdown_lookback_years")
    held = {s.upper() for s in (held or set())}

    symbols = symbols if symbols is not None else resolve_universe(request)
    registry = universe.load()
    registry = registry.set_index("symbol") if not registry.empty else registry
    metrics = {r["symbol"]: r for r in tasty_metrics.latest(
        symbols, max_age_days=rcfg.get("metrics_max_age_days", 5)).to_dict("records")}
    latest = technical_study.load_latest()
    latest = latest.set_index("symbol") if not latest.empty else latest
    support = technical_study.load_support()
    studied = technical_study._support_symbols()
    events_frame = events.load()
    healthy = events.earnings_health()["healthy"] if not events_frame.empty else False
    account_cfg = sizing.account_config(request.account_profile)
    ref_dte = request.reference_dte()
    from analytics.vrp import match_rv_window_to_dte
    rv_window = match_rv_window_to_dte(int(round(ref_dte)))
    liq_bounds = tuple(rcfg.get("liquidity_value_log10", [2.0, 5.5]))

    rows = []
    for symbol in symbols:
        reg = registry.loc[symbol].to_dict() if symbol in getattr(registry, "index", []) else {}
        asset_class = reg.get("asset_class") or "stock"
        applicable = strategies_for(reg.get("settlement"), request)
        m = metrics.get(symbol, {})
        tech = latest.loc[symbol].to_dict() if symbol in getattr(latest, "index", []) else {}

        spot = tech.get("close")
        if spot is None or not np.isfinite(spot):
            bars = load_daily(symbol, basis="price", start=today - dt.timedelta(days=10))
            spot = float(bars["close"].iloc[-1]) if not bars.empty else None
        iv = iv_near_dte(m, ref_dte, today) if m else None
        em = expected_move(spot, iv, ref_dte)

        # Nearest strong level below spot, re-expressed in EM at the request DTE.
        sup_rows = support[(support["symbol"] == symbol) & support["strong"].fillna(False)] \
            if not support.empty else support
        sup = {}
        if spot and not sup_rows.empty:
            best = sup_rows.sort_values("level", ascending=False).iloc[0]
            sup = {"support_level_id": best["level_id"], "support_level": float(best["level"]),
                   "support_distance_pct": float(best["level"]) / spot - 1.0,
                   "support_distance_em": ((spot - float(best["level"])) / em) if em else None,
                   "support_strength": best["edge_ci_lo"], "support_summary": best["summary"]}

        try:
            daily = load_daily(symbol, basis="price")
            screen = universe_screen.metrics(daily, today, drawdown_years=dd_years)
            max_dd = screen["max_drawdown"] if screen else None
        except Exception:
            daily, max_dd = pd.DataFrame(), None

        # IV/RV at the request's horizon: IV nearest the reference DTE over
        # RV on the window the CSP gate pairs with that DTE. TastyTrade's
        # 30-day HV only when the bars cannot supply it.
        iv_index = m.get("iv_index")
        hv, hv_source = None, None
        if len(daily) > rv_window + 1:
            closes = daily["close"].astype(float).tail(rv_window + 1).to_numpy()
            hv, hv_source = float(np.std(np.diff(np.log(closes)), ddof=1) * math.sqrt(252)),                 f"bars {rv_window}d"
        elif m.get("hv_30d") is not None and np.isfinite(m.get("hv_30d")):
            hv, hv_source = float(m["hv_30d"]), "tastytrade 30d"
        ratio = (float(iv) / hv if iv is not None and hv and hv > 0 else None)

        scores = {
            "iv_rank": score_iv_rank(m.get("ivr"), m.get("ivp")),
            "iv_rv": score_iv_rv(ratio, rcfg.get("iv_rv_floor", 0.8), rcfg.get("iv_rv_cap", 1.5)),
            "liquidity": score_liquidity(m.get("liquidity_rating"),
                                         m.get("liquidity_value"), liq_bounds),
            "trend": score_trend(tech.get("trend_state"), trend_table),
            "support": score_support(sup.get("support_distance_em"), band,
                                     studied=symbol in studied and em is not None),
            "drawdown": score_drawdown(max_dd, rcfg.get("drawdown_floor", -0.65)),
        }
        score, coverage = composite(scores, weights)

        event = _event_status(symbol, applicable, asset_class, request, today,
                              events_frame, healthy) if applicable else {
            "event_status": "n/a", "event_by_strategy": {}, "event_notes": ""}
        feasible, capital_notes = _capital(applicable, spot, em, request, account_cfg)
        # Strategies still open after the event gate.
        open_strategies = [s for s in feasible
                           if event["event_by_strategy"].get(s, "ok") != "blocked"]

        exclusion = ""
        if not applicable:
            exclusion = "no requested strategy applies (cash-settled index needs pcs)"
        elif not feasible:
            exclusion = "capital: " + "; ".join(capital_notes)
        elif not open_strategies:
            exclusion = "events: " + event["event_notes"][:200]
        elif score is None:
            exclusion = "no ranking data (no metrics, technicals or bars)"

        rows.append({
            "symbol": symbol, "asset_class": asset_class,
            "settlement": reg.get("settlement") or "physical",
            "strategies": "+".join(open_strategies),
            "spot": spot, "iv_used": iv, "em": em,
            "em_pct": em / spot if em and spot else None, "reference_dte": ref_dte,
            "ivr": m.get("ivr"), "ivp": m.get("ivp"), "iv_index": iv_index,
            "rv": hv, "rv_source": hv_source, "iv_rv": ratio,
            "liquidity_rating": m.get("liquidity_rating"),
            "liquidity_value": m.get("liquidity_value"),
            "trend_state": tech.get("trend_state"), "max_drawdown": max_dd,
            **{k: sup.get(k) for k in ("support_level_id", "support_level",
                                       "support_distance_pct", "support_distance_em",
                                       "support_strength", "support_summary")},
            "event_status": event["event_status"], "event_notes": event["event_notes"],
            "capital_notes": "; ".join(capital_notes),
            "stage1_tier": reg.get("stage1_tier"),
            **{f"score_{k}": v for k, v in scores.items()},
            "score": score, "coverage": coverage,
            "eligible": not exclusion, "exclusion": exclusion,
            "held": symbol in held,
        })

    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["_key"] = np.where(frame["eligible"], frame["score"].fillna(-1.0), -2.0)
    frame = frame.sort_values(["_key", "symbol"], ascending=[False, True]) \
        .drop(columns="_key").reset_index(drop=True)
    eligible = frame["eligible"].to_numpy()
    frame["rank"] = np.where(eligible, np.cumsum(eligible), np.nan)
    top_n = request.top_n
    frame["selected"] = eligible & ((frame["rank"] <= top_n) if top_n else True)
    frame.attrs["request"] = request.to_dict()
    frame.attrs["weights"] = weights
    return frame


def chain_targets(ranked: pd.DataFrame, held: set[str] | None = None) -> list[str]:
    """Symbols to pull chains for: the selected top N, plus anything with an
    open position (its management and rolls need a current chain)."""
    picked = ranked.loc[ranked["selected"], "symbol"].tolist() if not ranked.empty else []
    extra = sorted(s for s in (held or set()) if s not in picked)
    return picked + extra


def summary(ranked: pd.DataFrame, top: int = 10) -> dict:
    """Compact manifest record: counts, exclusions by kind, the top rows."""
    if ranked.empty:
        return {"ranked": 0}
    excluded = ranked[~ranked["eligible"]]
    kinds = excluded["exclusion"].str.split(":").str[0].value_counts().to_dict()
    cols = ["rank", "symbol", "score", "coverage", "strategies", "event_status",
            "score_iv_rank", "score_iv_rv", "score_liquidity", "score_trend",
            "score_support", "score_drawdown"]
    return {"ranked": int(len(ranked)), "eligible": int(ranked["eligible"].sum()),
            "selected": ranked.loc[ranked["selected"], "symbol"].tolist(),
            "excluded_by": kinds,
            "top": ranked[ranked["eligible"]].head(top)[cols].to_dict("records")}
