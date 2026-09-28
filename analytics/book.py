"""
The open book as risk (Phase 15): marks, Greeks, beta-weighted delta,
theta per day, buying power and the events inside each position.

Everything the Portfolio page and the pipeline's position review need about
the positions already on, computed without Streamlit so it can be tested.

Per position (all dollars are for the whole position, all contracts):

    mark            net debit to close now, per share (short marks minus
                    long marks); `natural` is the worst case (short asks
                    minus long bids)
    unrealized      (credit - mark) x 100 x contracts, before exit fees
    profit_pct      share of max profit captured (the credit)
    delta_shares    position delta in shares of the underlying
    bw_delta        beta-weighted delta in SPY shares:
                    delta_shares x beta x spot / SPY spot
    theta_day       dollars per calendar day of time decay (positive for a
                    credit position)
    vega            dollars per 1 vol point
    bpr             buying power held: strike x 100 for a CSP, max loss for
                    a spread

Greeks come from the latest chain snapshot where the leg is quoted, else
from Black-Scholes at the leg's entry IV (`greeks_source` says which). Beta
is the 1-year daily-return regression on SPY (price basis).
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from analytics import paper
from analytics.options_math import bs_price_greeks
from core.paths import load_config

BENCHMARK = "SPY"


def _f(value) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if np.isfinite(value) else None


def _rate() -> float:
    return float((load_config().get("analytics", {}) or {}).get("risk_free_rate", 0.045))


def leg_quote(chain: pd.DataFrame | None, leg: dict) -> dict | None:
    """The chain row for one leg (matching root symbol when the chain has
    several, e.g. SPX and SPXW), as {mark, bid, ask, iv, delta, gamma, theta,
    vega}. None when the leg is not in the snapshot."""
    if chain is None or chain.empty:
        return None
    kind = leg.get("option_type") or "put"
    frame = chain
    exp = pd.Timestamp(leg["expiration"]).date()
    dates = pd.to_datetime(frame["expiration"]).dt.date
    match = frame[(dates == exp) & (frame["strike_price"].astype(float) == float(leg["strike"]))]
    root = leg.get("root_symbol")
    if root and "root_symbol" in match.columns and len(match) > 1:
        rooted = match[match["root_symbol"] == root]
        match = rooted if not rooted.empty else match
    if match.empty:
        return None
    row = match.iloc[0]
    bid, ask = _f(row.get(f"{kind}_bid")), _f(row.get(f"{kind}_ask"))
    mark = _f(row.get(f"{kind}_mark"))
    if mark is None and bid is not None and ask is not None:
        mark = (bid + ask) / 2.0
    return {"mark": mark, "bid": bid, "ask": ask, "iv": _f(row.get(f"{kind}_iv")),
            **{g: _f(row.get(f"{kind}_{g}")) for g in ("delta", "gamma", "theta", "vega")}}


def mark_position(position: dict, legs: pd.DataFrame, chain: pd.DataFrame | None,
                  spot: float | None, today: dt.date | None = None) -> dict:
    """Mark one position and compute its Greeks (see the module docstring)."""
    today = today or dt.date.today()
    n = max(int(position.get("contracts") or 1), 1)
    rate = _rate()
    mark = natural = 0.0
    marked = True
    greeks = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    sources = set()
    for _, leg in legs.iterrows():
        leg = leg.to_dict()
        sign = -1 if leg["side"] == "short" else 1
        qty = int(leg.get("qty") or 1)
        days = max((pd.Timestamp(leg["expiration"]).date() - today).days, 0)
        quote = leg_quote(chain, leg)
        if quote is None or quote["mark"] is None:
            marked = False
        else:
            # Net debit to close: buy back shorts, sell longs.
            mark += -sign * qty * quote["mark"]
            worst = quote["ask"] if sign < 0 else quote["bid"]
            natural += -sign * qty * (worst if worst is not None else quote["mark"])
        use_chain = quote is not None and all(quote.get(g) is not None for g in greeks)
        if use_chain:
            values = {g: quote[g] for g in greeks}
            sources.add("chain")
        elif spot:
            vol = _f(leg.get("iv")) or (quote or {}).get("iv") or 0.3
            bs = bs_price_greeks(spot, float(leg["strike"]), days, vol, rate,
                                 leg.get("option_type") or "put")
            values = {"delta": bs.delta, "gamma": bs.gamma, "theta": bs.theta, "vega": bs.vega}
            sources.add("model")
        else:
            continue
        for g in greeks:
            greeks[g] += sign * qty * values[g] * 100.0 * n

    fill = _f(position.get("actual_fill"))
    fill = fill if fill is not None else _f(position.get("modelled_fill")) or 0.0
    out = {"mark": mark if marked else None, "natural": natural if marked else None,
           "unrealized": (fill - mark) * 100.0 * n if marked else None,
           "profit_pct": (fill - mark) / fill if marked and fill else None,
           "delta_shares": greeks["delta"], "gamma_shares": greeks["gamma"],
           "theta_day": greeks["theta"], "vega": greeks["vega"],
           "greeks_source": "/".join(sorted(sources)) or "none"}
    return out


def beta(ticker: str, benchmark: str = BENCHMARK, days: int = 252,
         loader=None) -> float | None:
    """Daily-return beta over the last `days` sessions (price basis)."""
    if ticker.upper() == benchmark.upper():
        return 1.0
    if loader is None:
        from data_sources.yfinance_sync import load_daily as loader
    try:
        a, b = loader(ticker, basis="price"), loader(benchmark, basis="price")
    except Exception:
        return None
    if a is None or b is None or a.empty or b.empty:
        return None
    ra = a.set_index(pd.to_datetime(a["date"]))["close"].astype(float).pct_change()
    rb = b.set_index(pd.to_datetime(b["date"]))["close"].astype(float).pct_change()
    joined = pd.concat([ra, rb], axis=1, keys=["a", "b"]).dropna().tail(days)
    if len(joined) < 60 or joined["b"].var() == 0:
        return None
    return float(joined["a"].cov(joined["b"]) / joined["b"].var())


def open_book(today: dt.date | None = None, chain_loader=None, daily_loader=None,
              positions: pd.DataFrame | None = None,
              legs: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per open position with marks, Greeks and buying power."""
    today = today or dt.date.today()
    if positions is None:
        positions = paper.list_positions(status="open")
    if positions.empty:
        return pd.DataFrame()
    if legs is None:
        legs = paper.list_legs(positions["id"].astype(int).tolist())
    if chain_loader is None:
        from data_sources import chains as _chains

        def chain_loader(ticker):
            chain, under = _chains.load_chain(ticker)
            return chain, _chains.spot_from_underlying(under)

    cache: dict[str, tuple] = {}

    def chain_for(ticker):
        if ticker not in cache:
            try:
                cache[ticker] = chain_loader(ticker)
            except Exception:
                cache[ticker] = (pd.DataFrame(), None)
        return cache[ticker]

    _, spy_spot = chain_for(BENCHMARK)
    rows = []
    for _, pos in positions.iterrows():
        pos = pos.to_dict()
        ticker = str(pos["ticker"]).upper()
        chain, spot = chain_for(ticker)
        mine = legs[legs["position_id"] == pos["id"]]
        marks = mark_position(pos, mine, chain, spot, today)
        b = beta(ticker, loader=daily_loader)
        bw = (marks["delta_shares"] * b * spot / spy_spot
              if b is not None and spot and spy_spot else None)
        exp = pd.Timestamp(pos["expiration"]).date()
        fill = _f(pos.get("actual_fill"))
        rows.append({
            "id": int(pos["id"]), "ticker": ticker, "strategy": pos.get("strategy") or "csp",
            "legs": paper.leg_text(pos), "expiration": exp, "dte": (exp - today).days,
            "contracts": int(pos["contracts"]),
            "credit": fill if fill is not None else _f(pos.get("modelled_fill")),
            "spot": spot, "bpr": _f(pos.get("collateral")) or 0.0,
            "max_loss": _f(pos.get("max_loss")), "beta": b, "bw_delta": bw, **marks,
            "short_strike": float(pos["strike"]), "long_strike": _f(pos.get("long_strike")),
            "rolls_used": int(pos.get("rolls_used") or 0)
            if pd.notna(pos.get("rolls_used")) else 0,
            "entry_date": pos.get("entry_date"),
            "settlement_type": pos.get("settlement_type"),
        })
    return pd.DataFrame(rows)


def summary(frame: pd.DataFrame, nlv: float | None = None) -> dict:
    """Totals across the open book."""
    if nlv is None:
        nlv = float((load_config().get("account", {}) or {}).get("net_liquidating_value", 0))
    if frame is None or frame.empty:
        return {"positions": 0, "bpr": 0.0, "utilisation": 0.0 if nlv else None}
    bpr = float(frame["bpr"].sum())
    total = lambda c: float(frame[c].dropna().sum()) if c in frame and frame[c].notna().any() \
        else None  # noqa: E731
    return {
        "positions": int(len(frame)), "names": int(frame["ticker"].nunique()),
        "bpr": bpr, "utilisation": bpr / nlv if nlv else None, "nlv": nlv,
        "bw_delta": total("bw_delta"), "theta_day": total("theta_day"),
        "vega": total("vega"), "unrealized": total("unrealized"),
        "max_loss": total("max_loss"),
        "theta_on_bpr_annual": (total("theta_day") or 0.0) * 365.0 / bpr if bpr else None,
        "unpriced": int(frame["mark"].isna().sum()),
        "by_strategy": frame.groupby("strategy")["bpr"].sum().to_dict(),
    }


def event_calendar(frame: pd.DataFrame, events: pd.DataFrame | None = None,
                   today: dt.date | None = None) -> pd.DataFrame:
    """Every event between today and the last open expiration that falls
    inside a position: its own earnings/ex-dividend, and market-wide events
    (FOMC, CPI, OPEX...). One row per event, listing the positions it hits."""
    today = today or dt.date.today()
    if frame is None or frame.empty:
        return pd.DataFrame()
    if events is None:
        try:
            from data_sources import events as ev
            events = ev.load()
        except Exception:
            return pd.DataFrame()
    if events is None or events.empty:
        return pd.DataFrame()
    last = max(frame["expiration"])
    window = events[(pd.to_datetime(events["date"]).dt.date >= today)
                    & (pd.to_datetime(events["date"]).dt.date <= last)].copy()
    if window.empty:
        return pd.DataFrame()
    try:
        from data_sources.events import MARKET
    except Exception:
        MARKET = "*"
    rows = []
    for _, event in window.iterrows():
        day = pd.Timestamp(event["date"]).date()
        market = event["symbol"] == MARKET
        hit = frame[(frame["expiration"] >= day)
                    & (market | (frame["ticker"] == event["symbol"]))]
        if hit.empty:
            continue
        rows.append({"date": day, "days_away": (day - today).days,
                     "event": event.get("type"), "symbol": event["symbol"],
                     "detail": event.get("note") or "",
                     "positions": ", ".join(f"#{i} {t}" for i, t in
                                            zip(hit["id"], hit["ticker"])),
                     "bpr_exposed": float(hit["bpr"].sum())})
    return pd.DataFrame(rows).sort_values(["date", "symbol"]).reset_index(drop=True) \
        if rows else pd.DataFrame()
