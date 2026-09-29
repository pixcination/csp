"""
Candidate generation -- the decision sheet.

Replaces the composite score with expected value. The old score weighted
annualised yield at 30% and probability-OTM at 15%, which is close to
self-cancelling: further out of the money means a higher POP and a lower
yield, so the two components pull against each other by construction. And
probability-OTM was used raw over a 0.75-0.93 range, contributing about two
points out of a hundred.

Expected value collapses both into the number the trade is actually judged on:

    EV = net credit after fees
         - P(assign) x E[shortfall below strike] x 100
         - P(assign) x assignment fee

with `P(assign)` and `E[shortfall]` taken from `analytics/moves.py` -- what
this stock has actually done at a comparable volatility -- rather than from a
lognormal assumption. Divided by collateral and annualised, that is directly
comparable across strikes, expirations and tickers.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
It does not treat assignment as free. On a wheel, being assigned is entry at
a discount you already agreed to, and the covered-call side recovers part of
the drawdown -- so the EV above is *conservative*, scoring every trade as if
it were a naked put held to expiration. That is the right bias for a ranking
engine: it will never talk you into a trade by assuming a favourable second
leg that has not happened yet. `basis_quality` reports separately on whether
assignment would leave you owning the stock at a price worth owning it at.

Gates are rejections, not penalties. A trade that fails one is not a worse
trade, it is a trade not to take, so no amount of yield can buy past an
earnings print or a 12% cost drag.

SCAN REQUESTS (Phase 11)
------------------------
`evaluate_universe(request=...)` takes its DTE window or targets, delta
range, account profile and event overrides from an
`analytics.scan_request.ScanRequest`, plus the risk-mode gates: `min_pop`
rejects a strike whose empirical P(finish OTM) is below it (or unknown);
`max_loss_per_trade` caps contracts so (strike - fill) x 100 x contracts
stays under it; `max_pct_capital` replaces the profile's per-position cap.
Without a request, `ScanRequest.default()` reproduces the config window, so
the numbers do not move. The sheet is no longer cut to one row per ticker:
every accepted strike is kept, `best_per_ticker` marks the top one, and
portfolio construction proposes from those.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from analytics import costs, moves, regime, sizing, vrp
from analytics.exit_rules import screen_entry
from core.market_calendar import ET, trading_days_between
from core.paths import load_config
from core.progress import BaseReporter, NullReporter


from analytics.strategies.csp import (  # noqa: E402,F401  (moved in Phase 12)
    Recommendation, _candidate_strikes, _f, _rationale, _ticker_context, basis_assessment,
    evaluate_strike)


def build_decision_sheet(tickers: list[str] | None = None,
                          reporter: BaseReporter | None = None,
                          include_rejected: bool = False,
                          request=None) -> pd.DataFrame:
    """Rank every candidate strike across the universe by annualised EV."""
    return select_sheet(evaluate_universe(tickers, reporter=reporter, request=request),
                        include_rejected=include_rejected)


def evaluate_universe(tickers: list[str] | None = None,
                      reporter: BaseReporter | None = None,
                      request=None) -> pd.DataFrame:
    """Every candidate strike across the universe, accepted AND rejected.

    Sorted by rank key (rejected last), with the rejection census on
    `frame.attrs["census"]`. This is what a run persists as
    `candidates.parquet` -- the full sheet, so a later page can show why a
    strike was turned down, not only what survived. `select_sheet` turns it
    into the proposal list.
    """
    from analytics.scan_request import ScanRequest
    from core.paths import load_universe
    from data_sources import chains
    from data_sources import events, tasty_metrics
    from data_sources.yfinance_sync import load_daily

    cfg = load_config()
    # None = the whole CSP universe; an EMPTY list means nothing to analyse
    # (it used to fall through to the whole universe -- Phase 12 fix).
    tickers = load_universe() if tickers is None else list(tickers)
    reporter = reporter or NullReporter()
    request = request or ScanRequest.default()
    account = sizing.account_from_config(request.account_profile, request.max_pct_capital)
    regime_reading = regime.current()
    today = dt.datetime.now(ET).date()

    # Checked once, not per ticker: an empty calendar would otherwise block
    # every candidate in the universe, each rejection looking individually
    # reasonable while the run silently produced nothing. Phase 9: measured
    # over STOCKS only, from the merged yfinance + TastyTrade events table.
    if events.load().empty:
        events.build()
    calendar = events.earnings_health(tickers)
    if not calendar["healthy"]:
        reporter.log(f"EARNINGS GATE DEGRADED: {calendar['note']}")
    metrics_by_symbol = {r["symbol"]: r for r in
                         tasty_metrics.latest(tickers, max_age_days=5).to_dict("records")}

    from analytics.strategies import pcs as pcs_mod
    from analytics.strategies.context import TickerContext, csp_context

    rows: list = []                  # Recommendation | SpreadRecommendation
    annotated: list[dict] = []
    notes: dict[str, str] = {}       # per ticker, kept in the census (Phase 19 follow-up)
    wants_csp, wants_pcs = "csp" in request.strategies, "pcs" in request.strategies
    with reporter.stage("candidates", "Ranking candidates", total=len(tickers)):
        for ticker in tickers:
            try:
                chain, under = chains.load_chain(ticker, complete=True)
                spot = chains.spot_from_underlying(under)
                if chain.empty or not spot:
                    notes[ticker] = ("no chain snapshot" if chain.empty
                                     else "no spot in the snapshot")
                    reporter.advance(1, note=f"{ticker} {notes[ticker]}")
                    continue
                metrics = metrics_by_symbol.get(ticker)
                ctx = TickerContext.build(ticker, chain, spot, today, metrics)
                csp_ok = wants_csp and not ctx.cash_settled
                strikes = _candidate_strikes(chain, spot, today, cfg, request) \
                    if csp_ok else pd.DataFrame()
                if strikes.empty and not wants_pcs:
                    notes[ticker] = "no strikes in band"
                    reporter.advance(1, note=f"{ticker} no strikes in band")
                    continue

                daily = load_daily(ticker, basis="price")
                adv = _adv_dollars(daily)

                # Per-ticker signals, computed once rather than per strike.
                context = _ticker_context(ticker, chain, spot)

                def event_checks(strategy, expirations):
                    return {e: events.check(ticker, today, e, strategy,
                                            calendar_healthy=calendar["healthy"],
                                            overrides=request.event_policy_overrides)
                            for e in expirations}

                before = len(rows)
                if not strikes.empty:
                    checks = event_checks("csp", strikes["expiration"].dt.date.unique())
                    for _, row in strikes.iterrows():
                        expiration = pd.Timestamp(row["expiration"]).date()
                        check = checks[expiration]
                        earnings_hit = check.earnings_block
                        others_block = tuple(h.text() for h in check.hits
                                             if h.action == "block" and h.type != "earnings")
                        warn = tuple(h.text() for h in check.hits if h.action == "warn")
                        rec = evaluate_strike(ticker, row, spot, daily, adv, account,
                                               cfg, regime_reading,
                                               earnings_blocks=earnings_hit is not None,
                                               earnings_note=(earnings_hit.note
                                                              if earnings_hit else ""),
                                               context=context,
                                               event_rejections=others_block,
                                               event_warnings=warn,
                                               event_hits=tuple(h.text() for h in check.hits),
                                               metrics=metrics,
                                               min_pop=request.min_pop,
                                               max_loss_per_trade=request.max_loss_per_trade,
                                               dte_window=request.dte_window("csp"),
                                               today=today)
                        if rec is not None:
                            rows.append(rec)
                            record = rec.to_dict()
                            record["root_symbol"] = row.get("root_symbol")
                            record["_delta_range"] = tuple(request.delta_range)
                            annotated.append(csp_context(ctx, record, row))

                if wants_pcs:
                    frame_exp = pd.to_datetime(chain["expiration"]).dt.date
                    expirations = sorted({e for e in frame_exp
                                          if request.accepts_dte((e - today).days, "pcs")})
                    checks = event_checks("pcs", expirations)
                    for rec in pcs_mod.build_candidates(
                            ticker, ctx, daily, adv, account, cfg, regime_reading, request,
                            checks, metrics=metrics, skew=context, today=today):
                        rows.append(rec)
                        annotated.append(ctx.annotate(rec.to_dict()))

                new = rows[before:]
                accepted = sum(1 for r in new if r.accepted)
                exps = sorted(pd.to_datetime(chain["expiration"]).dt.date.unique())
                notes[ticker] = (f"{accepted} of {len(new)} passed; chain {len(chain)} rows, "
                                 f"{len(exps)} expiration(s) to {exps[-1] if exps else '-'}")
                reporter.advance(1, note=f"{ticker} {accepted} of {len(new)} passed")
            except Exception as exc:
                notes[ticker] = f"error: {type(exc).__name__}: {exc}"
                reporter.advance(1, note=f"{ticker} error")
                reporter.log(f"{ticker}: {type(exc).__name__}: {exc}")

    if not rows:
        empty = pd.DataFrame()
        empty.attrs["census"] = {"strikes_examined": 0, "tickers": 0, "accepted": 0,
                                 "by_reason": [], "headline": "no candidate built",
                                 "ticker_notes": notes}
        return empty

    census = rejection_census(rows)
    census["ticker_notes"] = notes
    dominant = (census["by_reason"][0][1] / max(census["strikes_examined"], 1)
                if census["by_reason"] else 0.0)
    if census["accepted"] == 0 or dominant > 0.6:
        prefix = "NO CANDIDATES PASSED. " if census["accepted"] == 0 else "GATE SKEW: "
        reporter.log(prefix + census["headline"])
        for reason, count in census["by_reason"][:5]:
            reporter.log(f"  {count:>4} x {reason}")

    frame = pd.DataFrame(annotated)
    frame["rank_key"] = [r.rank_key for r in rows]
    frame["trade_id"] = trade_ids(frame)
    frame = frame.sort_values("rank_key", ascending=False).reset_index(drop=True)
    frame = pcs_mod.mark_default_choice(frame)
    frame.attrs["census"] = census
    return frame


def trade_ids(frame: pd.DataFrame) -> list[str]:
    """A stable id per trade row: strategy|ticker|expiration|short[|long][|root].
    (ticker, expiration, strike) stopped being unique once PCS rows share a
    short strike across widths and SPX lists the same strike on two roots.)"""
    out = []
    for r in frame.to_dict("records"):
        long = r.get("long_strike")
        root = r.get("root_symbol")
        parts = [r.get("strategy", "csp"), str(r["ticker"]), str(r["expiration"]),
                 f"{float(r['strike']):g}"]
        if long is not None and long == long:
            parts.append(f"{float(long):g}")
        if root and root == root and root != r["ticker"]:
            parts.append(str(root))
        out.append("|".join(parts))
    return out


def select_sheet(frame: pd.DataFrame, include_rejected: bool = False,
                 best_per_ticker_only: bool = False) -> pd.DataFrame:
    """The ranked sheet from `evaluate_universe`: accepted rows (unless
    `include_rejected`), EVERY strike kept, `best_per_ticker` marking the top
    row of each ticker (Phase 11 -- the forced one-per-ticker cut is gone;
    `best_per_ticker_only` restores that view)."""
    census = frame.attrs.get("census")
    if not frame.empty and not include_rejected:
        frame = frame[frame["accepted"]]
    if frame.empty:
        empty = pd.DataFrame()
        if census:
            empty.attrs["census"] = census
        return empty
    out = mark_best_per_ticker(frame.reset_index(drop=True))
    if best_per_ticker_only:
        out = out[out["best_per_ticker"]].reset_index(drop=True)
    out.attrs["census"] = census
    return out


REASON_BUCKETS = [
    ("earnings", "earnings before expiration"),
    ("IV/RV", "implied vol not priced above realized"),
    ("below the $", "credit below the minimum"),
    ("fees are", "fees too large a share of the credit"),
    ("expected value", "negative expected value"),
    ("open interest", "open interest below the floor"),
    ("contracts traded", "option volume below the floor"),
    ("per-position cap", "position too large for the account"),
    ("deployable", "insufficient deployable cash"),
    ("position limit", "at the open-position limit"),
    ("skew is INVERTED", "put skew inverted -- selling the cheap tail"),
]


def rejection_census(rows: list) -> dict:
    """Why did candidates fail? Counted by gate, not just totalled.

    A run that proposes nothing is not self-explanatory, and every individual
    rejection can look reasonable while the aggregate points at one broken
    input. Counting them turns "no candidates passed the entry gates" into
    "58 of 61 were blocked on earnings", which is a different problem with a
    different fix.
    """
    from collections import Counter

    counter: Counter = Counter()
    strikes_examined = len(rows)
    tickers = {r.ticker for r in rows}
    accepted = [r for r in rows if r.accepted]

    for row in rows:
        if row.accepted:
            continue
        matched = False
        for needle, label in REASON_BUCKETS:
            if any(needle in reason for reason in row.rejections):
                counter[label] += 1
                matched = True
        if not matched and row.rejections:
            counter[row.rejections[0][:60]] += 1

    by_reason = counter.most_common()
    dominant_share = (by_reason[0][1] / max(strikes_examined, 1)) if by_reason else 0.0

    if accepted and dominant_share <= 0.6:
        headline = (f"{len(accepted)} of {strikes_examined} strikes across "
                    f"{len(tickers)} tickers passed every gate.")
    elif accepted:
        # Something passed, but one gate is eliminating almost everything. That
        # is nearly as misleading as returning nothing: the survivors look like
        # a shortlist when they are really the residue of a broken input.
        top, count = by_reason[0]
        headline = (f"{len(accepted)} of {strikes_examined} strikes passed, but "
                    f"{count} ({dominant_share:.0%}) were blocked on a single gate: "
                    f"{top}. Check the input behind it before treating the "
                    f"survivors as a shortlist.")
    elif by_reason:
        top, count = by_reason[0]
        share = count / max(strikes_examined, 1)
        headline = (f"{count} of {strikes_examined} strikes ({share:.0%}) were "
                    f"blocked on: {top}."
                    + ("  That single gate accounts for most of the universe -- "
                       "check the input behind it before changing thresholds."
                       if share > 0.6 else ""))
    else:
        headline = (f"No strikes were evaluated at all across {len(tickers)} "
                    f"tickers -- most likely no expirations fell inside the "
                    f"configured DTE window, or no chain snapshots exist.")

    return {"strikes_examined": strikes_examined, "tickers": len(tickers),
            "accepted": len(accepted), "by_reason": by_reason,
            "headline": headline}


def mark_best_per_ticker(frame: pd.DataFrame) -> pd.DataFrame:
    """Flag the best-ranked row of each ticker (the frame is in rank order).

    Six adjacent strikes on one name read as six opportunities and are one,
    so proposals are drawn from these rows only; the others stay visible.
    """
    out = frame.copy()
    if out.empty:
        out["best_per_ticker"] = pd.Series(dtype=bool)
        return out
    out["best_per_ticker"] = ~out["ticker"].duplicated()
    return out


def _adv_dollars(daily: pd.DataFrame, window: int = 90) -> float | None:
    if daily.empty or "volume" not in daily.columns or len(daily) < 10:
        return None
    frame = daily.tail(window)
    dollars = (frame["close"].astype(float) * frame["volume"].astype(float)).mean()
    return float(dollars) if np.isfinite(dollars) else None
