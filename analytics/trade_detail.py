"""
Trade Detail data -- everything the deep-dive page shows, without Streamlit
(Phase 14, roadmap C.6).

The Screener page lists every candidate of a run; selecting one opens the
Trade Detail page with `?run=<run id>&trade=<trade id>`. That page reads the
PERSISTED run (`pipeline.results.load_run`) -- never a re-run -- so what it
shows is exactly what the ranking saw, and the URL stays bookmarkable for as
long as the run folder exists.

This module builds the page's tables from the sheet row plus data on disk:

    record(results, trade_id)       the row as a plain dict (numpy -> python)
    thesis(row)                     plain-English summary, verdict, risk
                                    flags and "why this strike"
    payoff_frame(position, ...)     P&L at expiry and at T+n days, any legs
    greeks_frame(position, ...)     net Greeks from entry to expiry
    scenario_grid(position, ...)    P&L over price x IV shifts at a given day
    terminal_distributions(...)     empirical (block-bootstrapped history,
                                    model H's sampler) vs lognormal at IV
    em_cone(...)                    IV and straddle expected-move cones
    chain_window(chain, ...)        the strikes around the trade, both legs
    management_plan(row)            target, time stop, roll trigger, and the
                                    config rules behind them
    screener_grid(sheet)            the Screener's results columns

Everything here is per share unless the name says dollars; `Position`
(strategies/base.py) carries the legs, so a future strategy with more legs
renders without changes.
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd

from analytics.strategies.base import Position
from core.paths import load_config

#: Probability columns of the results grid (blended engine output)
TARGET_COLUMNS = {25: "p_hit_25_blend", 30: "p_hit_30_blend", 50: "p_hit_50_blend",
                  100: "p_hit_100_blend"}


# --- Row handling -----------------------------------------------------------------

def _plain(value):
    """numpy scalars/arrays and NaN -> plain python, so a row survives
    `paper.accept`, JSON and f-strings alike."""
    if isinstance(value, np.ndarray):
        return [_plain(v) for v in value.tolist()]
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    if isinstance(value, str) and value in MISSING_TEXT:
        return None
    return value


#: What `pipeline.results._parquet_safe` leaves behind for a missing value in
#: a text column (it stores str(v), so None/NaN arrive as text)
MISSING_TEXT = {"nan", "None", "NaN", "<NA>", "NaT"}


def _clean_text(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for column in out.columns:
        if out[column].dtype == object:
            out[column] = out[column].map(
                lambda v: None if isinstance(v, str) and v in MISSING_TEXT else v)
    return out


def record(results, trade_id: str) -> dict | None:
    """One sheet row of a persisted run, as a plain dict."""
    sheet = getattr(results, "candidates", None)
    if sheet is None or sheet.empty or "trade_id" not in sheet:
        return None
    rows = sheet[sheet["trade_id"] == trade_id]
    if rows.empty:
        return None
    out = {k: _plain(v) for k, v in rows.iloc[0].to_dict().items()}
    for key in ("rejections", "warnings", "notes", "events"):
        value = out.get(key)
        if value is None:
            out[key] = []
        elif isinstance(value, str):
            out[key] = [value] if value else []
    out["expiration"] = str(pd.Timestamp(out["expiration"]).date())
    return out


def default_trade(results) -> str | None:
    """The trade a page shows when none is named: the top-ranked accepted row."""
    sheet = getattr(results, "candidates", None)
    if sheet is None or sheet.empty or "trade_id" not in sheet:
        return None
    ranked = sheet
    if "accepted" in sheet:
        accepted = sheet[sheet["accepted"].fillna(False).astype(bool)]
        ranked = accepted if not accepted.empty else sheet
    if "rank_key" in ranked:
        ranked = ranked.sort_values("rank_key", ascending=False)
    return str(ranked.iloc[0]["trade_id"])


def escape_md(text: str) -> str:
    """Streamlit markdown reads $...$ as LaTeX and ~~...~~ as strikethrough;
    prices carry $ and approximations carry ~."""
    return str(text).replace("$", r"\$").replace("~", r"\~")


def trade_label(row: dict) -> str:
    if row.get("strategy") == "pcs":
        body = f"${row['strike']:g}/${row['long_strike']:g} put spread"
    else:
        body = f"${row['strike']:g} put"
    return f"{row['ticker']} {row['expiration']} {body}"


def position(row: dict) -> Position:
    from analytics.probabilities import position_from_row
    return position_from_row(row)


def days_to_expiry(row: dict, today: dt.date | None = None) -> int:
    return max(int(row.get("dte_calendar") or 0), 0)


# --- Plain-English summary ---------------------------------------------------------

def _pct(x) -> str:
    return "--" if x is None else f"{x:.0%}"


def verdict(row: dict) -> tuple[str, str]:
    """(level, text) -- level is ok | warn | error, for the banner colour."""
    if not row.get("accepted"):
        reasons = "; ".join(row.get("rejections") or []) or "failed an entry gate"
        return "error", f"Rejected: {reasons}."
    ev = row.get("headline_ev")
    pop = row.get("pop_blend")
    if ev is not None and ev <= 0:
        return "warn", ("Passes every gate, but the blended models expect it to lose money "
                        "under the headline policy.")
    if row.get("proposed"):
        return "ok", "Proposed: passed every gate and the portfolio limits."
    if pop is not None and pop < 0.6:
        return "warn", "Passes the gates with a low blended probability of profit."
    return "ok", "Passes every entry gate (not proposed: a better row of the same ticker or " \
                 "the portfolio limits came first)."


def effective_basis(row: dict) -> float:
    """Share cost if a CSP is assigned: strike less the credit."""
    for key in ("breakeven", "effective_basis"):
        if row.get(key):
            return float(row[key])
    return float(row["strike"]) - float(row.get("modelled_fill") or 0.0)


def thesis(row: dict) -> dict:
    """The Summary tab: what the trade is betting on, in words."""
    strategy = row.get("strategy") or "csp"
    spot = row.get("spot")
    short = row["strike"]
    distance = (short / spot - 1.0) if spot else None
    parts = []
    if strategy == "pcs":
        parts.append(
            f"Sell the ${short:g} put and buy the ${row['long_strike']:g} put "
            f"({row.get('width') or 0:g} wide) for a ${row.get('modelled_fill') or 0:.2f} credit. "
            f"You keep the credit if {row['ticker']} closes above ${short:g} on "
            f"{row['expiration']}; the most you can lose is ${row.get('max_loss') or 0:,.0f}.")
    else:
        parts.append(
            f"Sell the ${short:g} put for ${row.get('modelled_fill') or 0:.2f}. You keep the "
            f"premium if {row['ticker']} closes above ${short:g} on {row['expiration']}; "
            f"below it you buy 100 shares per contract at an effective "
            f"${effective_basis(row):,.2f}.")
    if distance is not None:
        em = row.get("short_distance_em")
        parts.append(
            f"The short strike is {abs(distance):.1%} below spot ${spot:,.2f}"
            + (f", {abs(em):.2f} expected moves away" if em is not None else "") + ".")
    if row.get("pop_blend") is not None:
        parts.append(
            f"The blended models give a {_pct(row['pop_blend'])} chance of profit at expiry "
            f"(G {_pct(row.get('pop_G'))}, H {_pct(row.get('pop_H'))}, T {_pct(row.get('pop_T'))}) "
            f"and {_pct(row.get('p_hit_50_blend'))} of reaching 50% of max profit first.")
    if row.get("headline_ev") is not None:
        parts.append(
            f"Headline policy `{row.get('headline_policy')}`: expected P&L "
            f"${row['headline_ev']:,.0f} net of fees over ~{row.get('headline_days') or 0:.0f} days.")
    if row.get("iv_rv_ratio"):
        parts.append(f"Implied volatility is {row['iv_rv_ratio']:.2f}x realised"
                     + (" -- premium is rich." if row["iv_rv_ratio"] >= 1.2 else "."))

    why = [row.get("strike_rule_reason") or f"strike rule `{row.get('strike_rule') or 'n/a'}`"]
    if row.get("strong_support_id"):
        why.append(f"strong support {row['strong_support_id']} at "
                   f"${row['strong_support_level']:,.2f}"
                   + (f" ({row['strong_support_summary']})" if row.get("strong_support_summary")
                      else ""))
    elif row.get("support_summary"):
        why.append(f"nearest level: {row['support_summary']}")
    if row.get("oi_wall_strike"):
        why.append(f"open-interest wall at ${row['oi_wall_strike']:g} "
                   f"({row.get('oi_wall_multiple') or 0:.1f}x median OI)")

    level, text = verdict(row)
    return {"verdict": text, "level": level, "text": " ".join(parts), "why_strike": why,
            "flags": risk_flags(row)}


def risk_flags(row: dict) -> list[str]:
    flags = [f"event: {e}" for e in row.get("events") or []]
    flags += list(row.get("warnings") or [])
    if row.get("p_touch_blend") is not None and row["p_touch_blend"] >= 0.4:
        flags.append(f"{row['p_touch_blend']:.0%} chance the short strike is touched on a "
                     f"daily close -- expect to manage it")
    if row.get("scales") is False:
        flags.append(f"size capped by {row.get('binding_constraint') or 'liquidity'}")
    if row.get("fillability") is not None and row["fillability"] < 0.4:
        flags.append(f"weak fillability {row['fillability']:.2f} "
                     f"(weakest leg: {row.get('weakest_leg') or 'short'})")
    if row.get("below_min_gain_targets"):
        flags.append(f"closing at {row['below_min_gain_targets']} nets under the minimum gain")
    if row.get("t_flag"):
        flags.append(f"technical model: {row['t_flag']}")
    if row.get("cost_drag") and row["cost_drag"] > 0.08:
        flags.append(f"fees take {row['cost_drag']:.0%} of gross credit")
    # events are also copied into warnings by construction; show each once
    seen, out = set(), []
    for flag in flags:
        key = flag.removeprefix("event: ")
        if key not in seen:
            seen.add(key)
            out.append(flag)
    return out


# --- Payoff, Greeks, scenarios -------------------------------------------------------

def price_grid(position: Position, spot: float, em: float | None = None,
               n: int = 161) -> np.ndarray:
    strikes = [leg.strike for leg in position.legs]
    span = max(3.0 * (em or 0.0), 0.12 * spot, 1.5 * (max(strikes) - min(strikes)))
    lo = max(min(min(strikes), spot) - span, 0.01)
    hi = max(max(strikes), spot) + span
    return np.linspace(lo, hi, n)


def payoff_frame(position: Position, spot: float, dte: int, contracts: int = 1,
                 em: float | None = None, days: list[int] | None = None,
                 rate: float | None = None) -> pd.DataFrame:
    """Long frame: price, curve ("expiry" or "T+n"), pnl in dollars for
    `contracts` (before fees). T+n reprices every leg at its own IV."""
    rate = _rate() if rate is None else rate
    prices = price_grid(position, spot, em)
    scale = 100.0 * max(int(contracts), 1)
    frames = [pd.DataFrame({"price": prices, "curve": "expiry",
                            "pnl": position.payoff(prices) * scale})]
    if days is None:
        days = sorted({0, dte // 2} - {dte}) if dte > 1 else [0]
    for day in days:
        left = dte - day
        if left <= 0:
            continue
        values = np.array([position.value(p, left, rate=rate) for p in prices])
        frames.append(pd.DataFrame({"price": prices, "curve": f"T+{day}",
                                    "pnl": (position.credit + values) * scale}))
    return pd.concat(frames, ignore_index=True)


def _rate() -> float:
    return float((load_config().get("analytics", {}) or {}).get("risk_free_rate", 0.045))


def leg_greeks(position: Position, spot: float, days_left: float,
               rate: float | None = None, iv_shift: float = 0.0) -> dict:
    """Net position Greeks per contract (x100), signed, recomputed by
    Black-Scholes at each leg's IV (+ `iv_shift`)."""
    from analytics.options_math import bs_price_greeks
    rate = _rate() if rate is None else rate
    out = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "value": 0.0}
    for leg in position.legs:
        vol = max((leg.iv or 0.3) + iv_shift, 0.01)
        g = bs_price_greeks(spot, leg.strike, days_left, vol, rate, leg.option_type)
        k = leg.sign * leg.qty * 100.0
        out["delta"] += k * g.delta
        out["gamma"] += k * g.gamma
        out["theta"] += k * g.theta
        out["vega"] += k * g.vega
        out["value"] += k * g.price
    return out


def greeks_frame(position: Position, spot: float, dte: int, contracts: int = 1,
                 rate: float | None = None) -> pd.DataFrame:
    """Net Greeks for the whole position, day 0 to expiry, spot and IV held."""
    n = max(int(contracts), 1)
    rows = []
    for day in range(0, max(dte, 1)):
        g = leg_greeks(position, spot, dte - day, rate)
        rows.append({"day": day, **{k: v * n for k, v in g.items() if k != "value"}})
    return pd.DataFrame(rows)


def scenario_grid(position: Position, spot: float, dte: int, day: int = 0,
                  contracts: int = 1, moves=None, iv_shifts=None,
                  rate: float | None = None) -> pd.DataFrame:
    """P&L in dollars (before fees) across spot moves (fractions) and IV
    shifts (vol points as fractions), `day` days after entry."""
    rate = _rate() if rate is None else rate
    moves = np.round(np.arange(-0.10, 0.1001, 0.02), 4) if moves is None else moves
    iv_shifts = [-0.10, -0.05, 0.0, 0.05, 0.10] if iv_shifts is None else iv_shifts
    left = max(dte - day, 0)
    scale = 100.0 * max(int(contracts), 1)
    rows = []
    for shift in iv_shifts:
        for move in moves:
            price = spot * (1.0 + move)
            value = position.value(price, left, rate=rate,
                                   iv_fn=lambda leg: max((leg.iv or 0.3) + shift, 0.01))
            rows.append({"iv_shift": shift, "move": move, "price": price,
                         "pnl": (position.credit + value) * scale})
    return pd.DataFrame(rows)


# --- Expected move and distributions ---------------------------------------------------

def em_cone(spot: float, start: dt.date, dte: int, em_iv: float | None,
            em_straddle: float | None) -> pd.DataFrame:
    """Daily cone to expiry: each EM scales with sqrt(time)."""
    days = np.arange(0, max(dte, 1) + 1)
    frac = np.sqrt(days / max(dte, 1))
    out = pd.DataFrame({"date": [pd.Timestamp(start) + pd.Timedelta(days=int(d)) for d in days]})
    for name, em in (("iv", em_iv), ("straddle", em_straddle)):
        if em and np.isfinite(em):
            out[f"{name}_upper"] = spot + em * frac
            out[f"{name}_lower"] = spot - em * frac
    return out


def terminal_distributions(daily: pd.DataFrame, spot: float, iv: float | None,
                           dte_trading: int, dte_calendar: int, n_paths: int = 20_000,
                           seed: int = 20260927, rate: float | None = None) -> dict:
    """Terminal prices at expiry under two views:

    empirical  model H's sampler: 5-day blocks of this ticker's own daily
               returns from days at a similar realised vol (unconditioned when
               too few match), last 10 years
    lognormal  GBM at the short leg's IV -- what the option price implies
    """
    from analytics import prob_engine as pe
    cfg = pe.EngineConfig.from_config()
    cfg.n_paths = n_paths
    rate = cfg.rate if rate is None else rate
    rng = np.random.default_rng(seed)
    steps = max(int(dte_trading), 1)
    out = {"label": {}}
    h, _, _ = pe.h_paths(daily, steps, cfg, rng)
    if h.log_returns is not None:
        out["empirical"] = spot * np.exp(h.log_returns[:, -1])
        out["label"]["empirical"] = h.label
    if iv and iv > 0:
        years = max(dte_calendar, 1) / 365.0
        z = rng.standard_normal(n_paths)
        out["lognormal"] = spot * np.exp((rate - 0.5 * iv * iv) * years + iv * math.sqrt(years) * z)
        out["label"]["lognormal"] = f"lognormal at IV {iv:.0%}"
    return out


def distribution_table(dists: dict, levels: dict[str, float]) -> pd.DataFrame:
    """P(terminal price below each level) under each distribution."""
    rows = []
    for name, value in levels.items():
        if value is None or not np.isfinite(value):
            continue
        row = {"level": name, "price": value}
        for model in ("empirical", "lognormal"):
            if model in dists:
                row[f"p_below_{model}"] = float(np.mean(dists[model] < value))
        rows.append(row)
    return pd.DataFrame(rows)


# --- Chain and liquidity -------------------------------------------------------------------

def chain_window(chain: pd.DataFrame, expiration, strikes: list[float],
                 n_each_side: int = 8) -> pd.DataFrame:
    """Puts of one expiration around the trade's strikes, with bid/ask width,
    OI and volume; the trade's legs flagged."""
    if chain is None or chain.empty:
        return pd.DataFrame()
    frame = chain[pd.to_datetime(chain["expiration"]).dt.date
                  == pd.Timestamp(expiration).date()].copy()
    if frame.empty:
        return frame
    frame = frame.sort_values("strike_price").reset_index(drop=True)
    lo, hi = min(strikes), max(strikes)
    idx_lo = int(frame["strike_price"].sub(lo).abs().idxmin())
    idx_hi = int(frame["strike_price"].sub(hi).abs().idxmin())
    view = frame.iloc[max(idx_lo - n_each_side, 0): idx_hi + n_each_side + 1].copy()
    if {"put_bid", "put_ask"} <= set(view.columns):     # strikes with no quote at all
        view = view[view["put_bid"].notna() | view["put_ask"].notna()]
    out = pd.DataFrame({
        "strike": view["strike_price"].astype(float),
        "bid": view.get("put_bid"), "ask": view.get("put_ask"),
        "mid": (view.get("put_bid") + view.get("put_ask")) / 2.0,
        "iv": view.get("put_iv"), "delta": view.get("put_delta"),
        "open_interest": view.get("put_open_interest"), "volume": view.get("put_volume"),
    })
    out["width"] = out["ask"] - out["bid"]
    out["width_pct"] = out["width"] / out["mid"].where(out["mid"] > 0)
    out["leg"] = out["strike"].map(lambda k: "short" if abs(k - strikes[0]) < 1e-9 else
                                   ("long" if any(abs(k - s) < 1e-9 for s in strikes[1:])
                                    else ""))
    return out.reset_index(drop=True)


def load_chain_for(row: dict, block: str | None = None) -> pd.DataFrame:
    from data_sources import chains
    try:
        chain, _ = chains.load_chain(row["ticker"], block)
    except Exception:
        chain = pd.DataFrame()
    if chain.empty and block is not None:
        try:
            chain, _ = chains.load_chain(row["ticker"])
        except Exception:
            chain = pd.DataFrame()
    return chain


# --- Management ---------------------------------------------------------------------------

def management_plan(row: dict) -> list[dict]:
    """The rules that will manage this trade, each with the number it implies."""
    cfg = load_config()
    mgmt = cfg.get("management", {}) or {}
    engine = cfg.get("prob_engine", {}) or {}
    dte = int(row.get("dte_calendar") or 0)
    credit = float(row.get("modelled_fill") or 0.0)
    contracts = max(int(row.get("contracts") or 1), 1)
    policy = row.get("headline_policy") or ""
    plan = []
    if policy.startswith("close_"):
        target = int(policy.split("_")[1])
        buyback = credit * (1 - target / 100.0)
        plan.append({"rule": "Profit target",
                     "detail": f"close at {target}% of max profit: buy back at about "
                               f"${buyback:.2f} (P {_pct(row.get(f'p_hit_{target}_blend'))}, "
                               f"median {row.get(f'median_days_{target}_blend') or 0:.0f} days)"})
    else:
        plan.append({"rule": "Profit target",
                     "detail": f"hold to expiry -- at {dte} DTE the fees of an early close "
                               f"eat most of the remaining credit (P(expire worthless) "
                               f"{_pct(row.get('p_hit_100_blend'))})"})
    stop = int(engine.get("time_stop_dte", 21))
    if dte > stop:
        plan.append({"rule": "Time stop",
                     "detail": f"reassess at {stop} DTE (day {dte - stop}); reported as a "
                               f"policy, not the headline"})
    else:
        plan.append({"rule": "Time stop", "detail": f"none: the trade starts inside "
                                                    f"{stop} DTE"})
    defense = mgmt.get("defense", {}) or {}
    plan.append({"rule": "Roll trigger",
                 "detail": f"short delta beyond {defense.get('roll_when_delta_beyond', -0.45)}"
                           + (" or price below the short strike"
                              if defense.get("roll_when_price_below_strike") else "")
                           + f" (P {_pct(row.get('p_roll_blend'))}); roll only for a net "
                             f"credit, at most {defense.get('max_rolls_per_cycle', 2)} times, "
                             f"not under {defense.get('min_dte_to_roll', 2)} DTE"})
    exit_cfg = mgmt.get("exit", {}) or {}
    plan.append({"rule": "Minimum gain",
                 "detail": f"never close early for under "
                           f"${exit_cfg.get('min_net_gain_to_close_early', 5):.2f} net of fees"
                           + (f" -- applies to {row['below_min_gain_targets']}"
                              if row.get("below_min_gain_targets") else "")})
    if row.get("strategy") == "pcs":
        plan.append({"rule": "Max loss",
                     "detail": f"${row.get('max_loss') or 0:,.0f} for {contracts} contract(s) "
                               f"if {row['ticker']} expires below ${row['long_strike']:g} "
                               f"(P {_pct(row.get('p_max_loss_blend'))})"})
    else:
        plan.append({"rule": "Assignment",
                     "detail": f"accept assignment when no roll pays a credit: "
                               f"{100 * contracts} shares at an effective "
                               f"${effective_basis(row):,.2f} "
                               f"(P {_pct(row.get('p_assign_blend') or row.get('p_short_itm_blend'))})"})
    return plan


# --- Screener grid ----------------------------------------------------------------------------

GRID_COLUMNS = [
    "trade_id", "ticker", "strategy", "expiration", "dte_calendar", "strike", "long_strike",
    "width", "modelled_fill", "net_mid", "max_loss", "collateral", "return_on_risk",
    "headline_annualised", "headline_ev", "ev_per_day_bpr", "pop_blend",
    "p_hit_25_blend", "p_hit_30_blend", "p_hit_50_blend", "p_hit_100_blend",
    "median_days_50_blend", "ivr", "iv_rv_ratio", "short_distance_em", "support",
    "fillability", "event_flags", "accepted", "proposed", "best_per_ticker", "why_not",
]


def screener_grid(sheet: pd.DataFrame) -> pd.DataFrame:
    """The Screener's columns, derived where the sheet lacks them (CSP rows
    have no long leg; mid is the short leg's mid for a CSP, the net mid for a
    spread)."""
    if sheet is None or sheet.empty:
        return pd.DataFrame(columns=GRID_COLUMNS)
    out = _clean_text(sheet)
    for column in ("long_strike", "width", "net_mid", "return_on_risk", "max_loss",
                   "fillability", "ivr", "iv_rv_ratio", "short_distance_em",
                   "strong_support_id", "strong_support_level", "support_level_id",
                   "support_level", "headline_annualised", "headline_ev", "ev_per_day_bpr",
                   "median_days_50_blend", "pop_blend", *TARGET_COLUMNS.values()):
        if column not in out:
            out[column] = np.nan
    if "mid" in out:
        out["net_mid"] = out["net_mid"].fillna(out["mid"])
    strike = out["strike"].astype(float)
    csp = out["strategy"].fillna("csp") != "pcs"
    credit = out["modelled_fill"].astype(float)
    contracts = out["contracts"].fillna(0).clip(lower=1)
    out.loc[csp, "max_loss"] = out.loc[csp, "max_loss"].fillna(
        ((strike - credit) * 100.0 * contracts).loc[csp])
    ror = out["return_on_risk"]
    risk = out["max_loss"] / contracts / 100.0
    out["return_on_risk"] = ror.fillna(credit / risk.where(risk > 0))

    def support(r) -> str:
        if isinstance(r.get("strong_support_id"), str) and r.get("strong_support_id"):
            return f"{r['strong_support_id']} ${r['strong_support_level']:,.2f} (strong)"
        if isinstance(r.get("support_level_id"), str) and r.get("support_level_id"):
            level = r.get("support_level")
            status = r.get("support_status") or ""
            return (f"{r['support_level_id']} ${level:,.2f}" if level == level and level
                    else r["support_level_id"]) + (f" ({status})" if status else "")
        return ""

    def events(r) -> str:
        value = r.get("events")
        if value is None or (isinstance(value, float) and value != value):
            return ""
        if isinstance(value, str):
            return value
        return "; ".join(str(v) for v in list(value))

    def why_not(r) -> str:
        value = r.get("rejections")
        if value is None or (isinstance(value, float) and value != value):
            return ""
        return "; ".join(str(v) for v in list(value)) if not isinstance(value, str) else value

    records = out.to_dict("records")
    out["support"] = [support(r) for r in records]
    out["event_flags"] = [events(r) for r in records]
    out["why_not"] = [why_not(r) for r in records]
    for flag in ("accepted", "proposed", "best_per_ticker"):
        out[flag] = out[flag].fillna(False).astype(bool) if flag in out else False
    out["strategy"] = out["strategy"].fillna("csp")
    out["expiration"] = pd.to_datetime(out["expiration"]).dt.date
    text = {"trade_id", "ticker", "strategy", "expiration", "support", "event_flags",
            "why_not", "accepted", "proposed", "best_per_ticker"}
    for column in GRID_COLUMNS:
        if column not in text:          # object dtype renders NaN as "None"
            out[column] = pd.to_numeric(out[column], errors="coerce").astype(float)
    return out[GRID_COLUMNS]


def filter_grid(grid: pd.DataFrame, strategies: list[str] | None = None,
                tickers: list[str] | None = None, accepted_only: bool = True,
                proposed_only: bool = False, best_per_ticker: bool = False,
                min_pop: float | None = None, max_dte: int | None = None,
                sort: str = "ev_per_day_bpr", group_by_ticker: bool = False) -> pd.DataFrame:
    view = grid
    if strategies:
        view = view[view["strategy"].isin(strategies)]
    if tickers:
        view = view[view["ticker"].isin(tickers)]
    if accepted_only:
        view = view[view["accepted"]]
    if proposed_only:
        view = view[view["proposed"]]
    if best_per_ticker:
        ranked = view.sort_values(sort, ascending=False, na_position="last")
        view = ranked.drop_duplicates("ticker", keep="first")
    if min_pop:
        view = view[view["pop_blend"].fillna(0) >= min_pop]
    if max_dte:
        view = view[view["dte_calendar"] <= max_dte]
    view = view.assign(_rej=~view["accepted"])
    keys, ascending = ["_rej", sort], [True, False]
    if group_by_ticker:
        keys, ascending = ["ticker", "_rej", sort], [True, True, False]
    return view.sort_values(keys, ascending=ascending,
                            na_position="last").drop(columns="_rej").reset_index(drop=True)


def export_excel(frame: pd.DataFrame) -> bytes:
    import io
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="screener")
    return buffer.getvalue()


# --- Chart context ------------------------------------------------------------------------------

#: Event types drawn on the price chart: the ticker's own and market-wide ones
CHART_EVENTS = {"earnings", "ex_dividend", "split", "fomc", "cpi"}


def trade_events(ticker: str, start, end, frame: pd.DataFrame | None = None,
                 market_from=None) -> list[tuple]:
    """[(date, label)] for the chart window: the ticker's earnings, ex-dividend
    dates and splits, plus FOMC and CPI (stored with symbol "*") from
    `market_from` on -- past macro dates on every chart are noise."""
    if frame is None:
        from data_sources import events
        try:
            frame = events.load()
        except Exception:
            return []
    if frame is None or frame.empty:
        return []
    dates = pd.to_datetime(frame["date"])
    mask = ((frame["symbol"].isin([ticker, "*"])) & frame["type"].isin(CHART_EVENTS)
            & (dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end)))
    if market_from is not None:
        mask &= (frame["symbol"] != "*") | (dates >= pd.Timestamp(market_from))
    rows = frame[mask].assign(_d=dates[mask]).sort_values("_d")
    labels = {"earnings": "Earnings", "ex_dividend": "Ex-div", "split": "Split",
              "fomc": "FOMC", "cpi": "CPI"}
    return [(r["_d"], labels.get(r["type"], r["type"])) for _, r in rows.iterrows()]


def nearby_levels(support: pd.DataFrame, spot: float, strikes: list[float],
                  band: float = 0.15, limit: int = 5, min_gap: float = 0.005) -> pd.DataFrame:
    """Respected levels worth drawing: every strong one inside the band, then
    the nearest others below spot, `limit` in all."""
    if support is None or support.empty:
        return pd.DataFrame()
    frame = support.copy()
    frame["strong"] = frame["strong"].fillna(False).astype(bool)
    lo = min(min(strikes), spot) * (1 - band)
    frame = frame[(frame["level"] >= lo) & (frame["level"] <= spot * (1 + band))]
    strong = frame[frame["strong"]]
    others = frame[~frame["strong"] & (frame["level"] <= spot)].sort_values(
        "level", ascending=False)
    picked = []
    for _, lv in pd.concat([strong, others]).iterrows():
        # levels within `min_gap` of one already drawn would overprint its label
        if all(abs(lv["level"] - p["level"]) > min_gap * spot for p in picked):
            picked.append(lv)
        if len(picked) >= limit:
            break
    return pd.DataFrame(picked).reset_index(drop=True)
