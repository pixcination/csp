"""
Trade tracking (Phase 18, review Part C): log recommendations as forward
tests, re-mark them through the day, and settle them with two outcomes.

TWO BOOKS (`paper_positions.book`)
    tracked   a forward test at the modelled fill when logged. Never counted
              by capacity, exposure or correlation limits; never opens a wheel
              cycle or a share lot.
    taken     a real trade at your fill. `promote(tracked_id, fill)` records
              one from a tracked row and links them (`promoted_from`).

WHAT GETS LOGGED (C.3 -- measure the model, not the choices)
    `sample_rows(sheet)` picks, from one ranked sheet:
      top        the first K passing rows (default 5)
      control    M random passing rows further down (default M = 3)
      near_miss  M random rejected rows that failed exactly one gate
               (labelled `control` before 2026-09-29, relabelled then)
    Positions are deduplicated on `dedupe_key` (the sheet's trade_id:
    strategy, ticker, expiry, strikes). Seeing an open logged trade again
    appends a row to `tracking_observations` (price, rank, probabilities)
    instead of opening a second position.

UPDATE (C.4) -- `update()`
    Pulls fresh chains for the tickers with open positions only, then per
    position writes a `position_marks` row: time and session block, spot,
    each leg's bid/ask/mid/IV/Greeks, mark and natural, P&L in dollars and
    as a share of max profit, DTE left, best and worst so far, the
    probabilities recomputed FROM NOW (the engine on the current spot and leg
    IVs, the entry credit kept: P(reach the target), P(max loss), POP), the
    management verdict and its reason (exit_rules, the rules the book runs),
    and the P&L since the previous mark split into delta, gamma, theta, vega
    and a residual (the previous mark's Greeks times the change in spot,
    time and each leg's IV). The IV change uses each leg's IV implied from
    its own mid (`iv_mid`): dxFeed recomputes its Greeks events less often
    than quotes (SPY's stream IV and delta were identical three minutes
    apart while the mids moved), so the stream IV can lag the price.

ENTRY VS NOW
    The context at log time is stored on the position (`entry_context`):
    spot, expected move, leg IVs, IV rank/percentile, trend, RSI, days to
    earnings, the VIX term ratio and the probabilities. `entry_vs_now` lays
    it beside the latest mark.

OUTCOMES (C.4) -- `expire_due()`
    A position whose last expiration has a completed session is settled from
    that day's closing price (daily bars, price basis): expired worthless,
    settled, or assigned. Two outcomes are stored:
      hold      what holding to expiry made (`hold_pnl`, net of fees)
      managed   what the shipped rules did on the recorded marks: the first
                mark whose verdict was close or roll exits there
                (`managed_pnl`, `managed_rule`); with none, the hold outcome.
    Marks are sampled (the scheduled job makes them hourly in Phase 19), so a
    rule that would have fired between marks is caught at the next one.

PER CONTRACT (Phase 18 follow-up)
    Every mark stores `pnl_per_contract` (gross, like `pnl`) and every outcome
    `hold_pnl_per_contract` / `managed_pnl_per_contract` (fees split evenly),
    so tracked results compare across tickers and accounts whatever the size.
    The profile-sized count stays in `paper_positions.sized_contracts`.

AUTO-LOGGING (Phase 19) -- `auto_log()`
    The scheduler's once-a-day log of one auto preset's run: the C.3 sample,
    minus naked and research-only rows (never auto-tracked), trimmed to the
    preset's daily cap on NEW positions (default K + 2M = 11; rows already
    open still get their observation). `observe_only()` is what any other
    scan of an auto preset does: observations for open positions, nothing new.
"""
from __future__ import annotations

import datetime as dt
import json
import math

import numpy as np
import pandas as pd

from core.paths import load_config

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS tracking_observations (
        dedupe_key VARCHAR, position_id INTEGER, run_id VARCHAR, observed_at TIMESTAMP,
        rank INTEGER, sample VARCHAR, preset VARCHAR, accepted BOOLEAN,
        modelled_fill DOUBLE, spot DOUBLE, pop_blend DOUBLE, p_hit_50_blend DOUBLE,
        p_max_loss_blend DOUBLE, ev_per_day_bpr DOUBLE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS position_marks (
        position_id INTEGER, marked_at TIMESTAMP, session_block VARCHAR, source VARCHAR,
        spot DOUBLE, mark DOUBLE, natural_mark DOUBLE, pnl DOUBLE, profit_pct DOUBLE,
        dte_left INTEGER, best_pct DOUBLE, worst_pct DOUBLE, legs_json VARCHAR,
        delta_shares DOUBLE, gamma_shares DOUBLE, theta_day DOUBLE, vega DOUBLE,
        target_pct INTEGER, p_target_now DOUBLE, p_max_loss_now DOUBLE, pop_now DOUBLE,
        verdict VARCHAR, verdict_urgency VARCHAR, verdict_reason VARCHAR,
        iv_rank DOUBLE, iv_pct DOUBLE, vix_ratio DOUBLE, trend VARCHAR, rsi DOUBLE,
        pnl_change DOUBLE, attr_delta DOUBLE, attr_gamma DOUBLE, attr_theta DOUBLE,
        attr_vega DOUBLE, attr_residual DOUBLE
    )
    """,
]
# Columns added after a table first shipped (run on every connect).
MIGRATIONS = [
    "ALTER TABLE position_marks ADD COLUMN IF NOT EXISTS pnl_per_contract DOUBLE",
    # Backfill: fees split evenly makes per contract exactly the total / n.
    "UPDATE position_marks SET pnl_per_contract = pnl / (SELECT greatest(p.contracts, 1) "
    "FROM paper_positions p WHERE p.id = position_marks.position_id) "
    "WHERE pnl_per_contract IS NULL AND pnl IS NOT NULL",
    "UPDATE paper_positions SET hold_pnl_per_contract = hold_pnl / greatest(contracts, 1) "
    "WHERE hold_pnl_per_contract IS NULL AND hold_pnl IS NOT NULL",
    "UPDATE paper_positions SET managed_pnl_per_contract = managed_pnl / greatest(contracts, 1) "
    "WHERE managed_pnl_per_contract IS NULL AND managed_pnl IS NOT NULL",
]

CLOSE_VERDICTS = {"close", "roll"}


def _f(value) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _cfg() -> dict:
    return dict(load_config().get("tracking", {}) or {})


def _con():
    from analytics import paper
    return paper._connect()


def _json(value) -> str:
    def default(obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, (np.ndarray, tuple, set)):
            return list(obj)
        return str(obj)
    return json.dumps(value, default=default)


# --- Context -----------------------------------------------------------------------------

def market_context(ticker: str, today: dt.date | None = None) -> dict:
    """IV rank/percentile, trend, RSI, days to earnings and the VIX term
    ratio for one ticker now (each None when unavailable)."""
    today = today or dt.date.today()
    out = {"iv_rank": None, "iv_pct": None, "trend": None, "rsi": None,
           "days_to_earnings": None, "vix_ratio": None}
    try:
        from data_sources import tasty_metrics
        m = tasty_metrics.for_symbol(ticker) or {}
        out["iv_rank"], out["iv_pct"] = _f(m.get("ivr")), _f(m.get("ivp"))
    except Exception:
        pass
    try:
        from analytics import technical_study
        latest = technical_study.load_latest()
        mine = latest[latest["symbol"] == ticker] if not latest.empty else latest
        if not mine.empty:
            trend = mine["trend_state"].iloc[0]
            out["trend"] = trend if isinstance(trend, str) else None
            out["rsi"] = _f(mine["rsi"].iloc[0]) if "rsi" in mine else None
    except Exception:
        pass
    try:
        from data_sources import events
        frame = events.load()
        rows = frame[(frame["symbol"] == ticker) & (frame["type"] == "earnings")
                     & (frame["date"] >= today)]
        if not rows.empty:
            out["days_to_earnings"] = (min(rows["date"]) - today).days
    except Exception:
        pass
    try:
        from analytics import regime
        out["vix_ratio"] = _f(regime.current().term_ratio)
    except Exception:
        pass
    return out


def entry_context(row: dict) -> dict:
    """What the world looked like when a row was logged."""
    ctx = market_context(str(row["ticker"]).upper())
    legs = []
    try:
        from analytics import paper
        legs = [{"side": l["side"], "type": l["option_type"], "strike": l["strike"],
                 "iv": l.get("iv")} for l in paper.legs_from_recommendation(row)]
    except Exception:
        legs = []
    return {**ctx, "spot": _f(row.get("spot")), "em": _f(row.get("em")),
            "short_distance_em": _f(row.get("short_distance_em")), "legs": legs,
            "iv_rank": _f(row.get("ivr")) if _f(row.get("ivr")) is not None else ctx["iv_rank"],
            "iv_pct": _f(row.get("ivp")) if _f(row.get("ivp")) is not None else ctx["iv_pct"],
            "pop": _f(row.get("pop_blend")), "p_hit_50": _f(row.get("p_hit_50_blend")),
            "p_max_loss": _f(row.get("p_max_loss_blend")),
            "ev_per_day_bpr": _f(row.get("ev_per_day_bpr")),
            "headline_policy": row.get("headline_policy"),
            "logged_at": dt.datetime.now().isoformat(timespec="seconds")}


# --- Logging -----------------------------------------------------------------------------

def dedupe_key(row: dict) -> str:
    if row.get("trade_id"):
        return str(row["trade_id"])
    strikes = "/".join(f"{float(v):g}" for v in (row.get("strike"), row.get("long_strike"))
                       if _f(v) is not None)
    return f"{row.get('strategy') or 'csp'}|{row['ticker']}|" \
           f"{pd.Timestamp(row['expiration']).date()}|{strikes}"


def _ranked(sheet: pd.DataFrame) -> pd.DataFrame:
    frame = sheet.copy()
    if "rank_key" in frame:
        frame = frame.sort_values("rank_key", ascending=False, kind="stable")
    elif "ev_per_day_bpr" in frame:
        frame = frame.sort_values("ev_per_day_bpr", ascending=False, kind="stable")
    frame = frame.reset_index(drop=True)
    frame["rank"] = np.arange(1, len(frame) + 1)
    return frame


def _n_rejections(value) -> int:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return 0
    return len(list(value))


def sample_rows(sheet: pd.DataFrame, k: int | None = None, m: int | None = None,
                seed: int | str = 0) -> pd.DataFrame:
    """The C.3 sample of one ranked sheet: top K passing rows, M random
    passing rows below them, M random near-miss rejects (one failed gate).
    Adds `sample` and `rank` (1 = best in the whole sheet)."""
    cfg = _cfg()
    k = int(k if k is not None else cfg.get("top_k", 5))
    m = int(m if m is not None else cfg.get("control_m", 3))
    if sheet is None or sheet.empty:
        return pd.DataFrame()
    ranked = _ranked(sheet)
    import zlib
    rng = np.random.default_rng(seed if isinstance(seed, int) else zlib.crc32(str(seed).encode()))
    passing = ranked[ranked["accepted"].astype(bool)]
    top = passing.head(k).assign(sample="top")
    rest = passing.iloc[k:]
    control = rest.iloc[sorted(rng.choice(len(rest), min(m, len(rest)), replace=False))] \
        if len(rest) else rest
    rejected = ranked[~ranked["accepted"].astype(bool)]
    near = rejected[rejected["rejections"].apply(_n_rejections) == 1] \
        if "rejections" in rejected else rejected.iloc[0:0]
    near = near.iloc[sorted(rng.choice(len(near), min(m, len(near)), replace=False))] \
        if len(near) else near
    return pd.concat([top, control.assign(sample="control"), near.assign(sample="near_miss")],
                     ignore_index=True)


def _open_by_key(con, key: str, book: str) -> int | None:
    row = con.execute("SELECT id FROM paper_positions WHERE dedupe_key = ? AND status = 'open' "
                      "AND coalesce(book, 'taken') = ? ORDER BY id DESC LIMIT 1",
                      [key, book]).fetchone()
    return int(row[0]) if row else None


def _observe(con, key: str, position_id: int, row: dict, run_id, sample, preset) -> None:
    con.execute("INSERT INTO tracking_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?)",
                [key, position_id, run_id, dt.datetime.now(),
                 int(row["rank"]) if _f(row.get("rank")) is not None else None, sample, preset,
                 bool(row.get("accepted")) if row.get("accepted") is not None else None,
                 _f(row.get("modelled_fill")), _f(row.get("spot")), _f(row.get("pop_blend")),
                 _f(row.get("p_hit_50_blend")), _f(row.get("p_max_loss_blend")),
                 _f(row.get("ev_per_day_bpr"))])


def log(rows, book: str = "tracked", sample: str = "manual", run_id: str | None = None,
        preset: str | None = None, account_profile: str | None = None,
        observe_only: bool = False, flags: str | None = None) -> list[dict]:
    """Log sheet rows (dicts or a frame). Each returns {key, action, position_id,
    message}: `opened` (a new position), `observed` (already open: an
    observation appended), `not_open` (observe_only and not tracked) or
    `skipped` (not recordable, e.g. a stock leg).

    `account_profile` is the profile the rows were sized against; unset, the
    run's request says (paper.run_profile)."""
    from analytics import paper
    if isinstance(rows, pd.DataFrame):
        rows = rows.to_dict("records")
    profile = account_profile or paper.run_profile(run_id)
    lookup = paper.run_kind(run_id) == "lookup"
    out = []
    for row in rows:
        row = dict(row)
        key = dedupe_key(row)
        row_sample = "lookup" if lookup else (row.get("sample") or sample)
        con = _con()
        try:
            existing = _open_by_key(con, key, book)
            if existing is not None:
                _observe(con, key, existing, row, run_id, row_sample, preset)
                out.append({"key": key, "action": "observed", "position_id": existing,
                            "message": f"{key}: already tracked (#{existing}); observation added"})
                continue
        finally:
            con.close()
        if observe_only:
            out.append({"key": key, "action": "not_open", "position_id": None,
                        "message": f"{key}: not tracked; observe-only scans open nothing"})
            continue
        try:
            source = {k: v for k, v in row.items() if not isinstance(v, (bytes,))}
            result = paper.accept(
                row, contracts=max(int(_f(row.get("contracts")) or 0), 1),
                run_id=run_id, book=book, sample=row_sample,
                notes=f"logged ({row_sample})",
                tracking={"dedupe_key": key, "preset": preset, "account_profile": profile,
                          "sized_contracts": int(_f(row.get("contracts")) or 0),
                          "flags": flags,
                          "rank_at_log": int(row["rank"]) if _f(row.get("rank")) else None,
                          "trade_id": row.get("trade_id"), "entry_spot": _f(row.get("spot")),
                          "entry_context": _json(entry_context(row)),
                          "source_row": _json(source)})
        except (ValueError, KeyError, TypeError) as exc:
            out.append({"key": key, "action": "skipped", "position_id": None,
                        "message": f"{key}: not recorded ({exc})"})
            continue
        con = _con()
        try:
            _observe(con, key, result.position_id, row, run_id, row_sample, preset)
        finally:
            con.close()
        out.append({"key": key, "action": "opened", "position_id": result.position_id,
                    "message": result.message})
    return out


def log_all(sheet: pd.DataFrame, run_id: str | None = None, preset: str | None = None,
            k: int | None = None, m: int | None = None,
            account_profile: str | None = None) -> list[dict]:
    """C.3: log the top K and the control sample of one sheet as tracked."""
    picked = sample_rows(sheet, k, m, seed=run_id or 0)
    return log(picked, book="tracked", run_id=run_id, preset=preset,
               account_profile=account_profile)


def auto_eligible(sheet: pd.DataFrame) -> pd.DataFrame:
    """Rows an automatic log may open: never a naked spec, never a row the
    profile marks research-only (Tom, Phase 17/19)."""
    if sheet is None or sheet.empty:
        return pd.DataFrame() if sheet is None else sheet
    keep = pd.Series(True, index=sheet.index)
    if "research_only" in sheet:
        keep &= ~sheet["research_only"].map(lambda v: bool(v) if pd.notna(v) else False)
    if "margin_class" in sheet:
        keep &= sheet["margin_class"].fillna("") != "naked"
    return sheet[keep]


def opened_today(preset: str, day: dt.date | None = None) -> int:
    """Tracked positions a preset opened on `day` (its daily cap counts these)."""
    day = day or dt.date.today()
    con = _con()
    try:
        row = con.execute("SELECT count(*) FROM paper_positions WHERE preset = ? "
                          "AND coalesce(book, 'taken') = 'tracked' "
                          "AND CAST(logged_at AS DATE) = ?", [preset, day]).fetchone()
    finally:
        con.close()
    return int(row[0]) if row else 0


def auto_log(sheet: pd.DataFrame, run_id: str, preset: str, account_profile: str,
             k: int | None = None, m: int | None = None, daily_cap: int | None = None,
             today: dt.date | None = None) -> dict:
    """The scheduled log of one auto preset's run (Phase 19). Returns
    {results, opened, observed, capped, excluded}."""
    cfg = _cfg()
    k = int(k if k is not None else cfg.get("top_k", 5))
    m = int(m if m is not None else cfg.get("control_m", 3))
    cap = int(daily_cap if daily_cap is not None else k + 2 * m)
    eligible = auto_eligible(sheet)
    excluded = 0 if sheet is None else len(sheet) - len(eligible)
    picked = sample_rows(eligible, k, m, seed=run_id or 0)
    if picked.empty:
        return {"results": [], "opened": 0, "observed": 0, "capped": 0, "excluded": excluded}
    room = max(cap - opened_today(preset, today), 0)
    con = _con()
    try:
        is_open = [(_open_by_key(con, dedupe_key(r), "tracked") is not None)
                   for r in picked.to_dict("records")]
    finally:
        con.close()
    # Already-open rows only add observations; new ones fill the room in
    # sample order (top first, then controls, then near-misses).
    new_rank = np.cumsum([not o for o in is_open])
    allowed = [o or n <= room for o, n in zip(is_open, new_rank)]
    kept = picked[allowed]
    capped = int(len(picked) - len(kept))
    results = log(kept, book="tracked", run_id=run_id, preset=preset,
                  account_profile=account_profile)
    return {"results": results,
            "opened": sum(r["action"] == "opened" for r in results),
            "observed": sum(r["action"] == "observed" for r in results),
            "capped": capped, "excluded": excluded}


def observe_only(sheet: pd.DataFrame, run_id: str, preset: str) -> dict:
    """A non-logging scan of an auto preset: observations for the rows that
    are already tracked, nothing opened."""
    if sheet is None or sheet.empty:
        return {"results": [], "observed": 0}
    results = log(_ranked(auto_eligible(sheet)), book="tracked", run_id=run_id,
                  preset=preset, observe_only=True)
    return {"results": results, "observed": sum(r["action"] == "observed" for r in results)}


def promote(position_id: int, actual_fill: float | None = None, contracts: int | None = None,
            leg_fills: list[float] | None = None, entry_date: dt.date | None = None,
            notes: str = ""):
    """Record a real trade from a tracked one (the tracked row stays open as
    the forward test); the new `taken` position links back via promoted_from."""
    from analytics import paper
    row = paper._query("SELECT * FROM paper_positions WHERE id = ?", [position_id])
    if row.empty:
        raise ValueError(f"no position {position_id}")
    row = row.iloc[0].to_dict()
    if not row.get("source_row"):
        raise ValueError(f"position {position_id} was not logged from a sheet row")
    source = json.loads(row["source_row"])
    return paper.accept(source, contracts=contracts or int(row["contracts"]),
                        actual_fill=actual_fill, leg_fills=leg_fills, entry_date=entry_date,
                        run_id=row.get("run_id"), notes=notes or f"promoted from #{position_id}",
                        book="taken", sample=row.get("sample") or "manual",
                        tracking={"dedupe_key": row.get("dedupe_key"),
                                  "trade_id": row.get("trade_id"),
                                  "entry_spot": _f(row.get("entry_spot")),
                                  "entry_context": row.get("entry_context"),
                                  "source_row": row.get("source_row"),
                                  "account_profile": row.get("account_profile"),
                                  "sized_contracts": row.get("sized_contracts"),
                                  "promoted_from": int(position_id)})


# --- Verdicts (the rules the book runs) ---------------------------------------------------

def verdict(row: dict, legs: pd.DataFrame, chain: pd.DataFrame | None, spot: float | None,
            mark: float | None, today: dt.date, daily: pd.DataFrame | None = None) -> dict | None:
    """The management decision for one open position right now (CSP:
    evaluate_short_put; PCS: evaluate_put_spread + roll candidates; spec
    positions: their exit block). None when it cannot be priced."""
    from analytics import book, strategy_spec
    from analytics.exit_rules import (OpenPut, OpenSpecPosition, OpenSpread,
                                      evaluate_put_spread, evaluate_short_put,
                                      evaluate_spec_position, spread_roll_candidates)
    from core.market_calendar import trading_days_between
    if spot is None:
        return None
    strategy = row.get("strategy") or "csp"
    ticker = str(row["ticker"]).upper()
    credit = float(row["actual_fill"] if pd.notna(row.get("actual_fill")) else row["modelled_fill"])
    expiration = pd.Timestamp(row["expiration"]).date()
    extra: dict = {}
    if strategy not in ("csp", "put", "pcs"):
        if mark is None:
            return None
        try:
            spec = strategy_spec.load_all().get(strategy)
        except Exception:
            spec = None
        front = min(pd.Timestamp(e).date() for e in legs["expiration"])
        best = row.get("max_profit_share")
        position = OpenSpecPosition(
            ticker=ticker, label=spec.label if spec else strategy,
            contracts=int(row["contracts"]), entry_credit=credit, current_mark=float(mark),
            max_profit=float(best) if best is not None and pd.notna(best) else abs(credit),
            calendar_days_left=max((front - today).days, 0),
            entry_dte_calendar=(front - pd.Timestamp(row["entry_date"]).date()).days,
            legs=tuple((l["side"], int(l["qty"] or 1)) for _, l in legs.iterrows()))
        decision = evaluate_spec_position(position, spec.exit if spec else {})
    elif strategy == "pcs":
        if daily is None:
            from data_sources.yfinance_sync import load_daily
            daily = load_daily(ticker, basis="price")
        short_leg = legs[legs["side"] == "short"]
        quote = book.leg_quote(chain, short_leg.iloc[0].to_dict()) if not short_leg.empty else None
        position = OpenSpread(
            ticker=ticker, short_strike=float(row["strike"]),
            long_strike=float(row["long_strike"]), contracts=int(row["contracts"]),
            entry_credit=credit, spot=spot, current_mark=mark if mark is not None else credit,
            calendar_days_left=max((expiration - today).days, 0),
            trading_days_left=max(trading_days_between(today, expiration), 0),
            entry_dte_calendar=(expiration - pd.Timestamp(row["entry_date"]).date()).days,
            short_delta=(quote or {}).get("delta"),
            rolls_used=int(row["rolls_used"]) if pd.notna(row.get("rolls_used")) else 0,
            cash_settled=row.get("settlement_type") == "cash")
        decision = evaluate_put_spread(position, daily)
        if decision.action.value == "roll" and chain is not None and not chain.empty:
            rolls = spread_roll_candidates(chain, position, expiration, today)
            extra["roll_candidates"] = (json.loads(rolls.to_json(orient="records",
                                                                 date_format="iso"))
                                        if not rolls.empty else [])
    else:
        if daily is None:
            from data_sources.yfinance_sync import load_daily
            daily = load_daily(ticker, basis="price")
        position = OpenPut(
            ticker=ticker, strike=float(row["strike"]), contracts=int(row["contracts"]),
            entry_credit=credit, spot=spot, current_mark=mark if mark is not None else 0.0,
            trading_days_left=max(trading_days_between(today, expiration), 0))
        decision = evaluate_short_put(position, daily)
    return {"position_id": int(row["id"]), "strategy": strategy, **decision.to_dict(), **extra}


# --- Forward probabilities ------------------------------------------------------------------

def _target_for(row: dict) -> int | None:
    strategy = row.get("strategy") or "csp"
    if strategy == "pcs":
        from analytics.exit_rules import spread_config
        return spread_config().get("profit_target_pct") or 50
    if strategy in ("csp", "put"):
        return 50
    try:
        from analytics import strategy_spec
        spec = strategy_spec.load_all().get(strategy)
        return int(spec.exit.get("profit_target_pct") or 50) if spec else 50
    except Exception:
        return 50


def forward_probabilities(row: dict, legs: pd.DataFrame, quotes: list[dict | None],
                          spot: float, today: dt.date, n_paths: int | None = None,
                          daily: pd.DataFrame | None = None) -> dict:
    """The engine from NOW: current spot, each leg at its current IV (entry IV
    if unquoted), the entry credit kept -- so P(reach X%) is of the ORIGINAL
    max profit. Returns {target_pct, p_target, p_max_loss, pop} (blend)."""
    import zlib

    from analytics import prob_engine as pe
    from analytics.probabilities import _technical_frame
    from analytics.strategies.base import Leg, Position
    from core.market_calendar import trading_days_between
    ticker = str(row["ticker"]).upper()
    target = _target_for(row)
    option_legs = []
    for (_, leg), quote in zip(legs.iterrows(), quotes):
        iv = (quote or {}).get("iv") or _f(leg.get("iv")) or 0.3
        option_legs.append(Leg(leg["option_type"], leg["side"], float(leg["strike"]),
                               pd.Timestamp(leg["expiration"]).date(), int(leg["qty"] or 1),
                               iv=iv))
    credit = float(row["actual_fill"] if pd.notna(row.get("actual_fill")) else row["modelled_fill"])
    front = min(l.expiration for l in option_legs)
    dte_cal = max((front - today).days, 0)
    if dte_cal < 1:
        return {"target_pct": target, "p_target": None, "p_max_loss": None, "pop": None}
    position = Position(row.get("strategy") or "csp", ticker, option_legs, credit,
                        front_dte=dte_cal)
    steps = max(trading_days_between(today, front), 1)
    cfg = pe.EngineConfig.from_config()
    cfg.n_paths = int(n_paths or _cfg().get("n_paths", 4000))
    if daily is None:
        from data_sources.yfinance_sync import load_daily
        daily = load_daily(ticker, basis="price")
    tech, support = _technical_frame(ticker, daily)
    seed = cfg.seed + zlib.crc32(f"{ticker}|{steps}|now".encode())
    paths = pe.ticker_paths(daily, steps, cfg, np.random.default_rng(seed), tech, support)
    spec = pe.TradeSpec(position=position, spot=float(spot), dte_calendar=dte_cal,
                        dte_trading=steps, contracts=max(int(row["contracts"]), 1),
                        bpr=float(row.get("collateral") or 1.0),
                        spot_vol_beta=pe.name_spot_vol_beta(daily, cfg))
    result = pe.run_trade(spec, paths, [int(target)], cfg)
    blend = result["blend"]
    return {"target_pct": int(target), "p_target": blend.get(f"p_hit_{int(target)}"),
            "p_max_loss": blend.get("p_max_loss") if "p_max_loss" in blend
            else blend.get("p_assign"), "pop": blend.get("pop")}


# --- Update ----------------------------------------------------------------------------------

def _leg_detail(leg: dict, quote: dict | None, spot: float | None, today: dt.date) -> dict:
    """One leg's quote with Greeks (chain, else Black-Scholes at its IV)."""
    from analytics.options_math import bs_price_greeks, implied_vol
    days = max((pd.Timestamp(leg["expiration"]).date() - today).days, 0)
    q = dict(quote or {})
    iv = q.get("iv") or _f(leg.get("iv"))
    rate = float((load_config().get("analytics", {}) or {}).get("risk_free_rate", 0.045))
    iv_mid = None
    if spot and q.get("mark") and days > 0:
        try:
            iv_mid = implied_vol(float(q["mark"]), float(spot), float(leg["strike"]), days,
                                 rate, leg["option_type"])
        except Exception:
            iv_mid = None
    if spot and iv and any(q.get(g) is None for g in ("delta", "gamma", "theta", "vega")):
        g = bs_price_greeks(spot, float(leg["strike"]), days, iv, rate, leg["option_type"])
        for name in ("delta", "gamma", "theta", "vega"):
            if q.get(name) is None:
                q[name] = getattr(g, name)
    return {"side": leg["side"], "type": leg["option_type"], "strike": float(leg["strike"]),
            "expiration": str(pd.Timestamp(leg["expiration"]).date()),
            "qty": int(leg.get("qty") or 1), "bid": q.get("bid"), "ask": q.get("ask"),
            "mark": q.get("mark"), "iv": iv, "iv_mid": iv_mid,
            "delta": q.get("delta"), "gamma": q.get("gamma"),
            "theta": q.get("theta"), "vega": q.get("vega")}


def attribution(prev: dict, now: dict, contracts: int) -> dict:
    """P&L between two marks split into delta, gamma, theta, vega and a
    residual, from the PREVIOUS mark's per-leg Greeks: per leg,
    d(price) ~ delta dS + 1/2 gamma dS^2 + theta dt + vega d(IV points);
    position P&L = sum over legs of +/-qty x d(price) x 100 x contracts
    (long +, short -)."""
    out = {"pnl_change": None, "attr_delta": None, "attr_gamma": None, "attr_theta": None,
           "attr_vega": None, "attr_residual": None}
    if prev is None or prev.get("mark") is None or now.get("mark") is None:
        return out
    n = max(int(contracts), 1)
    total = -(float(now["mark"]) - float(prev["mark"])) * 100.0 * n
    d_s = (now["spot"] or 0.0) - (prev["spot"] or 0.0)
    d_t = (pd.Timestamp(now["marked_at"]) - pd.Timestamp(prev["marked_at"])).total_seconds() \
        / 86400.0
    parts = {"attr_delta": 0.0, "attr_gamma": 0.0, "attr_theta": 0.0, "attr_vega": 0.0}
    prev_legs = json.loads(prev["legs_json"]) if prev.get("legs_json") else []
    now_legs = now["legs"]
    for a, b in zip(prev_legs, now_legs):
        sign = -1.0 if a["side"] == "short" else 1.0
        scale = sign * int(a.get("qty") or 1) * 100.0 * n
        if a.get("delta") is not None:
            parts["attr_delta"] += scale * a["delta"] * d_s
        if a.get("gamma") is not None:
            parts["attr_gamma"] += scale * 0.5 * a["gamma"] * d_s * d_s
        if a.get("theta") is not None:
            parts["attr_theta"] += scale * a["theta"] * d_t
        iv_a = a.get("iv_mid") if a.get("iv_mid") is not None else a.get("iv")
        iv_b = b.get("iv_mid") if b.get("iv_mid") is not None and a.get("iv_mid") is not None \
            else b.get("iv")
        if a.get("vega") is not None and iv_a is not None and iv_b is not None:
            parts["attr_vega"] += scale * a["vega"] * (iv_b - iv_a) * 100.0
    out.update(parts)
    out["pnl_change"] = total
    out["attr_residual"] = total - sum(parts.values())
    return out


def _last_mark(con, position_id: int) -> dict | None:
    frame = con.execute("SELECT * FROM position_marks WHERE position_id = ? "
                        "ORDER BY marked_at DESC LIMIT 1", [position_id]).fetchdf()
    return None if frame.empty else frame.iloc[0].to_dict()


def _extremes(con, position_id: int) -> tuple[float | None, float | None]:
    row = con.execute("SELECT max(profit_pct), min(profit_pct) FROM position_marks "
                      "WHERE position_id = ?", [position_id]).fetchone()
    return (_f(row[0]), _f(row[1])) if row else (None, None)


def reuse_since(now: dt.datetime | None = None) -> dt.datetime:
    """The oldest capture a mark reuses instead of pulling again (Tom
    2026-09-29): anything from the last `tracking.reuse_chain_minutes` (20),
    and when an archive finished inside that window, anything it captured --
    the 15:45 mark reads the whole 15:30 archive, however long it took. An
    archive run by hand hours earlier widens nothing."""
    from data_sources import chain_archive
    now = now or dt.datetime.now()
    window = dt.timedelta(minutes=float(_cfg().get("reuse_chain_minutes", 20)))
    since = now - window
    try:
        latest = next(iter(chain_archive.list_archives()), None)
        started = dt.datetime.fromisoformat(latest["captured_at"]) if latest else None
        finished = started + dt.timedelta(seconds=float(latest.get("seconds") or 0)) \
            if started else None
    except (OSError, ValueError, TypeError):
        started = finished = None
    if started and since <= finished <= now:
        since = min(since, started)
    return since


def fresh_snapshot(ticker: str, legs: pd.DataFrame, since: dt.datetime,
                   today: dt.date) -> str | None:
    """The block of `ticker`'s newest snapshot when it prices every unexpired
    leg from a capture at or after `since`; None when the mark must pull.
    Only the newest block counts, because that is the one the marks read."""
    from analytics import book as book_mod
    from data_sources import chains

    block = chains.latest_block_for(ticker)
    if block is None:
        return None
    chain, _ = chains.load_chain(ticker, block)
    if chain.empty or "captured_at" not in chain:
        return None
    for _, leg in legs.iterrows():
        leg = leg.to_dict()
        exp = pd.Timestamp(leg["expiration"]).date()
        if exp < today:
            continue
        quote = book_mod.leg_quote(chain, leg)
        if quote is None or quote["mark"] is None:
            return None
        rows = chain[pd.to_datetime(chain["expiration"]).dt.date == exp]
        if pd.to_datetime(rows["captured_at"]).min() < pd.Timestamp(since):
            return None
    return block


def update(position_ids: list[int] | None = None, pull: bool = True, book: str | None = None,
           n_paths: int | None = None, source: str = "update", reporter=None,
           now: dt.datetime | None = None, reuse: bool = True) -> pd.DataFrame:
    """Re-mark open positions (C.4), then one `position_marks` row per
    position. Returns the new marks; `.attrs["chains"]` = {"pulled",
    "reused", "legs", "rest", "unquoted"}.

    Quotes (Tom 2026-09-29): a ticker whose newest snapshot prices every leg
    from a capture since `reuse_since()` reads that snapshot; every other
    ticker's held legs and underlying are quoted together in one DXLink
    session (`chains.quote_legs`), kept in memory -- no chain is pulled or
    written. Full chains come from the scans and the 15:30 archive."""
    from analytics import book as book_mod
    from analytics import paper
    from core.market_calendar import session_block
    from core.progress import NullReporter
    from data_sources import chains
    from data_sources.yfinance_sync import load_daily

    reporter = reporter or NullReporter()
    now = now or dt.datetime.now()
    today = now.date()
    positions = paper.list_positions(status="open", book=book)
    if position_ids is not None:
        positions = positions[positions["id"].isin([int(i) for i in position_ids])]
    if positions.empty:
        return pd.DataFrame()
    legs = paper.list_legs(positions["id"].astype(int).tolist())
    tickers = sorted(set(positions["ticker"].str.upper()))
    counts = {"pulled": 0, "reused": 0, "legs": 0, "rest": 0, "unquoted": 0}
    quoted: dict = {}
    if pull:
        since = reuse_since(now) if reuse else None
        to_quote = []
        with reporter.stage("chains", "Leg quotes for open positions", total=len(tickers)):
            for ticker in tickers:
                mine = legs[legs["position_id"].isin(
                    positions.loc[positions["ticker"].str.upper() == ticker, "id"])]
                live = mine[[pd.Timestamp(e).date() >= today for e in mine["expiration"]]]
                if live.empty:
                    reporter.advance(1, note=f"{ticker}: expired, nothing to quote")
                    continue
                block = fresh_snapshot(ticker, mine, since, today) if since else None
                if block:
                    counts["reused"] += 1
                    reporter.advance(1, note=f"{ticker}: reused {block}")
                    continue
                to_quote.append(live.assign(ticker=ticker))
            if to_quote:
                res = chains.quote_legs(pd.concat(to_quote, ignore_index=True),
                                        limiter=chains.SubscriptionLimiter(
                                            load_config().get("stage3_subs_per_minute_budget",
                                                              8000)),
                                        reporter=reporter, now=now)
                if res.symbols and len(res.missing) == res.symbols:
                    # Fail the slot (red on Schedule) rather than record a book
                    # of unpriced marks as a successful run.
                    raise RuntimeError(f"no leg quotes: stream {res.stream_error or 'empty'}, "
                                       f"REST returned none of {res.symbols}")
                quoted = res.frames
                counts.update(pulled=len(res.frames), legs=res.symbols, rest=res.rest_fallback,
                              unquoted=len(res.missing))
                reporter.advance(len(res.frames), note=(
                    f"{res.symbols} legs of {len(res.frames)} ticker(s) in one session, "
                    f"{res.streamed} streamed, {res.rest_fallback} by REST"
                    + (f", {len(res.missing)} unquoted" if res.missing else "")))
    block = session_block(now)
    out = []
    with reporter.stage("marks", "Marks and probabilities", total=len(positions)):
        for _, pos in positions.iterrows():
            row = pos.to_dict()
            ticker = str(row["ticker"]).upper()
            chain, under = quoted.get(ticker) or chains.load_chain(ticker)
            spot = chains.spot_from_underlying(under)
            mine = legs[legs["position_id"] == row["id"]].sort_values("leg_index")
            marked = book_mod.mark_position(row, mine, chain, spot, today)
            quotes = [book_mod.leg_quote(chain, l.to_dict()) for _, l in mine.iterrows()]
            detail = [_leg_detail(l.to_dict(), q, spot, today)
                      for (_, l), q in zip(mine.iterrows(), quotes)]
            daily = load_daily(ticker, basis="price")
            decision = verdict(row, mine, chain, spot, marked["mark"], today, daily)
            try:
                probs = forward_probabilities(row, mine, quotes, spot, today, n_paths, daily) \
                    if spot else {}
            except Exception as exc:
                reporter.log(f"#{row['id']}: probabilities failed ({exc})")
                probs = {}
            ctx = market_context(ticker, today)
            front = min(pd.Timestamp(e).date() for e in mine["expiration"])
            n = max(int(row["contracts"]), 1)
            record = {"position_id": int(row["id"]), "marked_at": now, "session_block": block,
                      "source": source, "spot": spot, "mark": marked["mark"],
                      "natural": marked["natural"], "pnl": marked["unrealized"],
                      "pnl_per_contract": (marked["unrealized"] / n
                                           if marked["unrealized"] is not None else None),
                      "profit_pct": marked["profit_pct"], "dte_left": (front - today).days,
                      "legs": detail, "delta_shares": marked["delta_shares"],
                      "gamma_shares": marked["gamma_shares"], "theta_day": marked["theta_day"],
                      "vega": marked["vega"], "target_pct": probs.get("target_pct"),
                      "p_target_now": probs.get("p_target"),
                      "p_max_loss_now": probs.get("p_max_loss"), "pop_now": probs.get("pop"),
                      "verdict": (decision or {}).get("action"),
                      "verdict_urgency": (decision or {}).get("urgency"),
                      "verdict_reason": (decision or {}).get("headline"),
                      "iv_rank": ctx["iv_rank"], "iv_pct": ctx["iv_pct"],
                      "vix_ratio": ctx["vix_ratio"], "trend": ctx["trend"], "rsi": ctx["rsi"]}
            con = _con()
            try:
                prev = _last_mark(con, int(row["id"]))
                attr = attribution(prev, record, n)
                best, worst = _extremes(con, int(row["id"]))
                pct = record["profit_pct"]
                if pct is not None:
                    best = pct if best is None else max(best, pct)
                    worst = pct if worst is None else min(worst, pct)
                values = {
                    "position_id": record["position_id"], "marked_at": now,
                    "session_block": block, "source": source, "spot": spot,
                    "mark": record["mark"], "natural_mark": record["natural"],
                    "pnl": record["pnl"], "profit_pct": pct, "dte_left": record["dte_left"],
                    "best_pct": best, "worst_pct": worst, "legs_json": _json(detail),
                    "delta_shares": record["delta_shares"],
                    "gamma_shares": record["gamma_shares"], "theta_day": record["theta_day"],
                    "vega": record["vega"], "target_pct": record["target_pct"],
                    "p_target_now": record["p_target_now"],
                    "p_max_loss_now": record["p_max_loss_now"], "pop_now": record["pop_now"],
                    "verdict": record["verdict"], "verdict_urgency": record["verdict_urgency"],
                    "verdict_reason": record["verdict_reason"], "iv_rank": record["iv_rank"],
                    "iv_pct": record["iv_pct"], "vix_ratio": record["vix_ratio"],
                    "trend": record["trend"], "rsi": record["rsi"],
                    "pnl_per_contract": record["pnl_per_contract"], **attr}
                con.execute(f"INSERT INTO position_marks ({', '.join(values)}) "
                            f"VALUES ({', '.join('?' * len(values))})", list(values.values()))
            finally:
                con.close()
            if record["mark"] is not None:
                paper.record_mark(int(row["id"]), float(record["mark"]), spot, today,
                                  source="tracking")
            out.append({**{k: v for k, v in record.items() if k != "legs"}, **attr,
                        "best_pct": best, "worst_pct": worst, "ticker": ticker})
            reporter.advance(1, note=f"#{row['id']} {ticker}: "
                                     + (f"{pct:+.0%} of max" if pct is not None else "unpriced")
                                     + f", {record['verdict'] or 'no verdict'}")
    frame = pd.DataFrame(out)
    frame.attrs["chains"] = counts
    return frame


# --- Reading --------------------------------------------------------------------------------

def marks(position_id: int | None = None) -> pd.DataFrame:
    from analytics import paper
    if position_id is None:
        return paper._query("SELECT * FROM position_marks ORDER BY marked_at")
    return paper._query("SELECT * FROM position_marks WHERE position_id = ? ORDER BY marked_at",
                        [int(position_id)])


def observations(position_id: int | None = None) -> pd.DataFrame:
    from analytics import paper
    if position_id is None:
        return paper._query("SELECT * FROM tracking_observations ORDER BY observed_at")
    return paper._query("SELECT * FROM tracking_observations WHERE position_id = ? "
                        "ORDER BY observed_at", [int(position_id)])


def entry_vs_now(position: dict, last_mark: dict | None) -> pd.DataFrame:
    """Rows: measure, at entry, now (and the change)."""
    entry = json.loads(position["entry_context"]) if position.get("entry_context") else {}
    now = last_mark or {}
    em = _f(entry.get("em"))
    short = _f(position.get("strike"))

    def dist(spot):
        return (spot - short) / em if spot and em and short else None

    now_legs = json.loads(now["legs_json"]) if now.get("legs_json") else []
    rows = [("spot", _f(entry.get("spot")) or _f(position.get("entry_spot")), _f(now.get("spot"))),
            ("short strike distance (entry EM)", dist(_f(entry.get("spot"))),
             dist(_f(now.get("spot")))),
            ("IV rank", _f(entry.get("iv_rank")), _f(now.get("iv_rank"))),
            ("IV percentile", _f(entry.get("iv_pct")), _f(now.get("iv_pct"))),
            ("RSI", _f(entry.get("rsi")), _f(now.get("rsi"))),
            ("VIX9D / VIX3M", _f(entry.get("vix_ratio")), _f(now.get("vix_ratio"))),
            ("POP", _f(entry.get("pop")), _f(now.get("pop_now"))),
            (f"P(reach {int(now.get('target_pct') or 50)}%)", _f(entry.get("p_hit_50")),
             _f(now.get("p_target_now"))),
            ("P(max loss / assignment)", _f(entry.get("p_max_loss")), _f(now.get("p_max_loss_now")))]
    for i, leg in enumerate(entry.get("legs") or []):
        now_iv = now_legs[i].get("iv") if i < len(now_legs) else None
        rows.append((f"IV {leg['side']} {leg['strike']:g}{leg['type'][0].upper()}",
                     _f(leg.get("iv")), _f(now_iv)))
    frame = pd.DataFrame(rows, columns=["measure", "entry", "now"])
    frame["change"] = frame["now"] - frame["entry"]
    trend_row = pd.DataFrame([{"measure": "trend", "entry": entry.get("trend"),
                               "now": now.get("trend"), "change": None}])
    return pd.concat([frame.astype(object), trend_row], ignore_index=True)


# --- Expiry and outcomes -------------------------------------------------------------------

def _close_on(ticker: str, day: dt.date) -> float | None:
    from data_sources.yfinance_sync import load_daily
    bars = load_daily(ticker, basis="price", start=day - dt.timedelta(days=7))
    if bars is None or bars.empty:
        return None
    bars = bars[pd.to_datetime(bars["date"]).dt.date == day]
    return _f(bars["close"].iloc[-1]) if not bars.empty else None


def managed_outcome(position: dict, marks_frame: pd.DataFrame) -> dict | None:
    """What the shipped rules did on the recorded marks: the first mark whose
    verdict was close or roll, exited at that mark with close fees. None when
    no mark said to act."""
    from analytics import costs, paper
    if marks_frame is None or marks_frame.empty:
        return None
    acted = marks_frame[marks_frame["verdict"].isin(CLOSE_VERDICTS)
                        & marks_frame["mark"].notna()].sort_values("marked_at")
    if acted.empty:
        return None
    first = acted.iloc[0]
    n = max(int(position["contracts"]), 1)
    legs = paper.list_legs([int(position["id"])])
    close_fees = costs.legs_close(paper._fee_sides(legs.to_dict("records"), n,
                                                   opening=False)).total
    fill = float(position["actual_fill"] if pd.notna(position.get("actual_fill"))
                 else position["modelled_fill"])
    fees = float(position.get("entry_fees") or 0.0) + close_fees
    gross = (fill - float(first["mark"])) * 100.0
    return {"managed_pnl": gross * n - fees, "managed_pnl_per_contract": gross - fees / n,
            "managed_exit_date": pd.Timestamp(first["marked_at"]).date(),
            "managed_rule": f"{first['verdict']}: {first.get('verdict_reason') or ''}"[:200]}


def expire_due(today: dt.date | None = None, book: str | None = "tracked",
               reporter=None) -> list[dict]:
    """Settle every open position whose last expiration has a completed
    session, from that day's close; store hold and managed outcomes."""
    from analytics import paper
    from core.freshness import last_completed_session
    last_done = last_completed_session() if today is None else today
    positions = paper.list_positions(status="open", book=book)
    if positions.empty:
        return []
    legs = paper.list_legs(positions["id"].astype(int).tolist())
    results = []
    for _, pos in positions.iterrows():
        row = pos.to_dict()
        mine = legs[legs["position_id"] == row["id"]]
        expirations = sorted({pd.Timestamp(e).date() for e in mine["expiration"]})
        if not expirations or expirations[0] > last_done:
            continue
        ticker = str(row["ticker"]).upper()
        if len(expirations) > 1:
            results.append({"position_id": int(row["id"]), "action": "manual",
                            "message": f"#{row['id']} {ticker}: the front leg expired but a "
                                       f"later leg lives on -- close it by hand"})
            continue
        day = expirations[0]
        price = _close_on(ticker, day)
        if price is None:
            results.append({"position_id": int(row["id"]), "action": "waiting",
                            "message": f"#{row['id']} {ticker}: no close for {day} yet"})
            continue
        debit, values, itm = paper.settlement_value(mine, price)
        strategy = row.get("strategy") or "csp"
        status = "expired_otm" if itm == 0 else ("assigned" if strategy in ("csp", "put")
                                                  else "settled")
        paper.close_position(int(row["id"]), status, exit_date=day,
                             settlement_price=price if status == "settled" else None,
                             notes=f"auto-expired at close ${price:,.2f}")
        closed = paper._query("SELECT * FROM paper_positions WHERE id = ?",
                              [int(row["id"])]).iloc[0].to_dict()
        n = max(int(row["contracts"]), 1)
        fill = float(row["actual_fill"] if pd.notna(row.get("actual_fill"))
                     else row["modelled_fill"])
        exit_fees = float(closed.get("exit_fees") or 0.0)
        # Hold: the package's value at the close (for an assigned CSP, the
        # shares marked at the close -- the loss is real whether or not sold).
        fees = float(row.get("entry_fees") or 0.0) + exit_fees
        hold = (fill - debit) * 100.0 * n - fees
        hold_one = (fill - debit) * 100.0 - fees / n
        managed = managed_outcome(row, marks(int(row["id"]))) or {
            "managed_pnl": hold, "managed_pnl_per_contract": hold_one,
            "managed_exit_date": day, "managed_rule": "held: no rule fired"}
        con = _con()
        try:
            con.execute("UPDATE paper_positions SET hold_status = ?, hold_pnl = ?, "
                        "hold_pnl_per_contract = ?, managed_pnl = ?, "
                        "managed_pnl_per_contract = ?, managed_exit_date = ?, "
                        "managed_rule = ? WHERE id = ?",
                        [status, hold, hold_one, managed["managed_pnl"],
                         managed["managed_pnl_per_contract"], managed["managed_exit_date"],
                         managed["managed_rule"], int(row["id"])])
        finally:
            con.close()
        results.append({"position_id": int(row["id"]), "action": status, "close": price,
                        "hold_pnl": hold, "hold_pnl_per_contract": hold_one, **managed,
                        "message": f"#{row['id']} {ticker} {day}: {status} at ${price:,.2f}; "
                                   f"hold ${hold:,.0f}, managed ${managed['managed_pnl']:,.0f}"})
        if reporter:
            reporter.log(results[-1]["message"])
    return results


def closed_outcomes(book: str | None = None) -> pd.DataFrame:
    """Closed positions with both outcomes (dollars, per contract, and whether
    the dollars belong in a dollar report)."""
    from analytics import paper
    frame = paper.list_positions(book=book)
    if frame.empty:
        return frame
    frame = frame[frame["status"] != "open"]
    cols = ["id", "book", "sample", "account_profile", "ticker", "strategy", "legs_label",
            "strike", "long_strike", "expiration", "contracts", "sized_contracts", "status",
            "exit_date", "settlement_price", "hold_pnl", "hold_pnl_per_contract",
            "managed_pnl", "managed_pnl_per_contract", "managed_exit_date", "managed_rule",
            "rec_pop", "dollar_pnl_valid", "flags"]
    return frame[[c for c in cols if c in frame.columns]].reset_index(drop=True)
