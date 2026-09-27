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


@dataclass
class Recommendation:
    ticker: str
    expiration: dt.date
    strike: float
    spot: float
    dte_calendar: int
    dte_trading: int

    # Pricing
    bid: float | None
    ask: float | None
    mid: float | None
    modelled_fill: float
    delta: float | None
    implied_vol: float | None

    # Sizing
    contracts: int
    collateral: float
    binding_constraint: str
    scales: bool

    # Economics
    gross_credit: float
    fees: float
    net_credit: float
    cost_drag: float
    expected_value: float
    ev_annualised: float
    net_annualised_if_expires: float

    # Probability
    prob_otm_empirical: float | None
    prob_touch: float | None
    expected_shortfall: float | None
    sample_label: str
    effective_n: int

    # Context
    iv_rv_ratio: float | None
    vrp_class: str
    effective_basis: float
    basis_quality: str
    regime_multiplier: float

    # Verdict
    accepted: bool
    rejections: tuple = ()
    warnings: tuple = ()
    rationale: str = ""
    open_interest: float | None = None
    option_volume: float | None = None

    # Phase 7 signals
    normalised_skew: float | None = None
    skew_class: str = ""
    gap_variance_share: float | None = None
    prob_undefendable_gap: float | None = None
    term_structure_ratio: float | None = None
    event_suspected: bool = False

    # Phase 9: TastyTrade market metrics and the events check
    ivr: float | None = None                 # tasty IV index rank (tos source), 0-1
    ivp: float | None = None                 # tasty IV percentile, 0-1
    iv_index: float | None = None
    liquidity_rating: int | None = None
    events: tuple = ()                       # every event hit, as text

    def to_dict(self) -> dict:
        out = asdict(self)
        out["expiration"] = str(self.expiration)
        return out

    @property
    def rank_key(self) -> float:
        """Sort key. Rejected candidates always sort last, whatever their EV."""
        if not self.accepted:
            return -1e9
        return self.ev_annualised


# --- Basis quality ---------------------------------------------------------

def basis_assessment(daily: pd.DataFrame, effective_basis: float) -> tuple[str, str]:
    """Would assignment leave you owning this at a price worth owning it at?

    The question the old score never asked. Probability-OTM treats assignment
    as pure downside; on a wheel it is entry, and entry at a good basis is a
    different outcome from entry at a bad one.
    """
    if daily.empty or len(daily) < 210:
        return "unknown", "not enough history to place the basis"
    frame = daily.copy()
    frame.columns = [str(c).lower() for c in frame.columns]
    close = frame["close"].astype(float)
    sma200 = float(close.rolling(200).mean().iloc[-1])
    window = close.tail(252)
    low52, high52 = float(window.min()), float(window.max())
    if high52 <= low52:
        return "unknown", "degenerate 52-week range"

    position = (effective_basis - low52) / (high52 - low52)
    vs_sma = effective_basis / sma200 - 1.0

    if effective_basis <= sma200 and position <= 0.35:
        return "good", (f"assignment basis ${effective_basis:,.2f} sits {abs(vs_sma):.0%} "
                        f"below the 200-day and in the bottom {position:.0%} of the "
                        f"52-week range -- a price worth owning it at")
    if effective_basis <= sma200:
        return "fair", (f"basis ${effective_basis:,.2f} is below the 200-day but "
                        f"{position:.0%} up the 52-week range")
    if position >= 0.85:
        return "poor", (f"basis ${effective_basis:,.2f} is {vs_sma:+.0%} versus the "
                        f"200-day and near the 52-week high -- assignment would leave "
                        f"you long near the top of the range")
    return "fair", (f"basis ${effective_basis:,.2f} is {vs_sma:+.0%} versus the 200-day")


# --- Evaluation ------------------------------------------------------------

def _candidate_strikes(chain: pd.DataFrame, spot: float, today: dt.date,
                        cfg: dict) -> pd.DataFrame:
    entry = cfg.get("management", {}).get("entry", {})
    lo, hi = entry.get("delta_band", [-0.30, -0.12])
    dte_min = entry.get("dte_min", 5)
    dte_max = entry.get("dte_max", 10)

    frame = chain.copy()
    if "put_delta" not in frame.columns:
        return pd.DataFrame()
    frame["expiration"] = pd.to_datetime(frame["expiration"])
    frame["dte_calendar"] = (frame["expiration"].dt.date - today).apply(lambda d: d.days)
    frame = frame[(frame["dte_calendar"] >= dte_min) & (frame["dte_calendar"] <= dte_max)]
    frame = frame[frame["put_delta"].notna()]
    frame = frame[(frame["put_delta"] >= lo) & (frame["put_delta"] <= hi)]
    # Only strikes below spot: a put above spot is already in the money and is
    # not the trade this tool proposes.
    return frame[frame["strike_price"].astype(float) < spot]


def _ticker_context(ticker: str, chain: pd.DataFrame, spot: float) -> dict:
    """Phase 7 signals that depend on the ticker, not the strike.

    Computed once per ticker because each involves either a chain-wide fit or a
    pass over the 1-minute archive, and doing that per candidate strike would
    dominate the run.
    """
    from analytics import gaps, skew

    context: dict = {}
    try:
        reading = skew.measure(chain, ticker, spot)
        if reading:
            context["normalised_skew"] = reading.normalised_skew
            context["skew_class"] = reading.classification
            context["skew_favourable"] = reading.favourable
            context["skew_note"] = reading.note
    except Exception:
        pass
    try:
        structure = skew.term_structure(chain, ticker, spot)
        if structure:
            context["term_structure_ratio"] = structure.ratio
            context["event_suspected"] = structure.event_suspected
            context["term_note"] = structure.note
    except Exception:
        pass
    try:
        gap = gaps.profile(ticker)
        if gap:
            context["gap_variance_share"] = gap.overnight_variance_share
            context["gap_note"] = gap.note
            context["gap_profile"] = gap
    except Exception:
        pass
    return context


def evaluate_strike(ticker: str, row: pd.Series, spot: float, daily: pd.DataFrame,
                     adv_dollars: float | None, account, cfg: dict,
                     regime_reading, ticker_committed: float = 0.0,
                     earnings_blocks: bool = False,
                     earnings_note: str = "",
                     context: dict | None = None,
                     event_rejections: tuple = (),
                     event_warnings: tuple = (),
                     event_hits: tuple = (),
                     metrics: dict | None = None) -> Recommendation | None:
    strike = float(row["strike_price"])
    expiration = pd.Timestamp(row["expiration"]).date()
    today = dt.datetime.now(ET).date()
    dte_cal = max((expiration - today).days, 0)
    dte_trd = max(trading_days_between(today, expiration), 1)

    bid = _f(row.get("put_bid"))
    ask = _f(row.get("put_ask"))
    mid = _f(row.get("put_mark"))
    fill = costs.realistic_fill(bid, ask, mid, side="sell")
    if fill is None or fill <= 0:
        return None

    oi = _f(row.get("put_open_interest"))
    volume = _f(row.get("put_volume"))
    iv = _f(row.get("put_iv"))
    delta = _f(row.get("put_delta"))

    size = sizing.max_contracts_for_strike(
        strike, account, ticker_committed=ticker_committed,
        open_interest=oi, option_volume=volume, adv_dollars=adv_dollars)

    contracts = size.contracts
    mult, _ = regime.apply_to_sizing(contracts, regime_reading) if contracts else (0, "")
    contracts = mult

    econ = costs.csp_economics(strike, fill, max(contracts, 1), max(dte_cal, 1),
                                outcome="expire")

    breach = moves.breach_probabilities(
        daily, ticker, spot, strike, dte_trd, lookback_years=10,
        vol_conditioned=True, min_observations=40) if not daily.empty else None

    if breach is not None:
        p_assign = breach.prob_terminal
        shortfall_per_share = breach.expected_loss_if_breached * spot
        expected_loss = p_assign * shortfall_per_share * 100 * max(contracts, 1)
        expected_loss += p_assign * costs.assignment(max(contracts, 1)).total
        prob_otm = breach.prob_otm_empirical
        sample, eff_n = breach.sample_label, breach.effective_n
        touch = breach.prob_touch
    else:
        p_assign = shortfall = None
        expected_loss = float("nan")
        prob_otm = touch = None
        sample, eff_n = "no empirical sample", 0
        shortfall_per_share = None

    net = econ.net_credit
    ev = net - expected_loss if expected_loss == expected_loss else float("nan")
    collateral = strike * 100.0 * max(contracts, 1)
    ev_ann = (ev / collateral) * (365.0 / max(dte_cal, 1)) if collateral else float("nan")

    reading = vrp.reading(ticker, iv, daily, vrp.match_rv_window_to_dte(dte_cal)) \
        if iv and not daily.empty else None
    ratio = reading.ratio if reading else None

    effective_basis = strike - fill
    basis_class, basis_note = basis_assessment(daily, effective_basis)

    verdict = screen_entry(credit=fill, strike=strike, contracts=max(contracts, 1),
                            dte=dte_cal, iv_rv_ratio=ratio,
                            earnings_before_expiry=earnings_blocks,
                            prob_otm_empirical=prob_otm)

    rejections = list(verdict.reasons)
    warnings = list(verdict.warnings)
    if earnings_blocks and earnings_note:
        rejections = [r if "earnings" not in r else f"earnings: {earnings_note}"
                      for r in rejections]
    # Non-earnings events the policy blocks or warns on (FOMC, CPI...).
    rejections.extend(event_rejections)
    warnings.extend(event_warnings)
    if size.rejected:
        rejections.extend(size.reasons or ("no tradable size",))
    if ev == ev and ev <= 0:
        rejections.append(
            f"expected value is ${ev:,.0f} -- the empirical loss tail is larger than "
            f"the credit, so this is a negative-expectancy trade at this strike")
    if basis_class == "poor":
        warnings.append(basis_note)

    context = context or {}
    skew_cfg = cfg.get("signals", {})

    # Inverted skew: the market is charging more for the upside tail than the
    # downside. Selling puts into that is selling the cheap side.
    if context.get("skew_class") == "inverted" and skew_cfg.get(
            "block_inverted_skew", True):
        rejections.append(context.get("skew_note", "put skew is inverted"))
    elif context.get("skew_class") == "flat":
        warnings.append(context.get("skew_note", "put skew is flat"))
    elif context.get("skew_class") == "unmeasurable":
        # Deliberately a warning and never a rejection. On a weekend capture
        # most names land here, and a gate that blocks on "we could not tell"
        # would reproduce the empty-candidate cascade the earnings guard caused
        # in Phase 6: every individual refusal reasonable, the aggregate useless.
        warnings.append(context.get("skew_note", "put skew not measurable from these quotes"))

    # Single-name backwardation means something dateable is expected. If the
    # earnings gate did not already catch it, this is the only warning there is.
    if context.get("event_suspected") and not earnings_blocks:
        warnings.append(context.get(
            "term_note", "near-dated IV is well above further-dated -- an event "
                          "the earnings calendar may not list"))

    # Gap risk specific to THIS strike, not just the ticker's general profile.
    gap_profile = context.get("gap_profile")
    prob_gap = None
    if gap_profile is not None:
        try:
            from analytics import gaps as gaps_mod
            trade_gap = gaps_mod.trade_gap_risk(ticker, spot, strike, dte_trd)
            if trade_gap:
                prob_gap = trade_gap.prob_any_gap_through
                limit = skew_cfg.get("max_prob_undefendable_gap", 0.05)
                if prob_gap >= limit:
                    warnings.append(trade_gap.verdict)
        except Exception:
            pass

    accepted = not rejections and contracts >= 1

    return Recommendation(
        ticker=ticker, expiration=expiration, strike=strike, spot=spot,
        dte_calendar=dte_cal, dte_trading=dte_trd,
        bid=bid, ask=ask, mid=mid, modelled_fill=fill, delta=delta, implied_vol=iv,
        contracts=contracts, collateral=collateral,
        binding_constraint=size.binding_constraint, scales=size.scales,
        gross_credit=econ.gross_credit, fees=econ.entry_fees + econ.exit_fees,
        net_credit=net, cost_drag=econ.cost_drag_pct,
        expected_value=ev, ev_annualised=ev_ann,
        net_annualised_if_expires=econ.net_annualized,
        prob_otm_empirical=prob_otm, prob_touch=touch,
        expected_shortfall=shortfall_per_share, sample_label=sample, effective_n=eff_n,
        iv_rv_ratio=ratio, vrp_class=reading.classification if reading else "unknown",
        effective_basis=effective_basis, basis_quality=basis_class,
        regime_multiplier=regime_reading.size_multiplier if regime_reading else 1.0,
        normalised_skew=context.get("normalised_skew"),
        skew_class=context.get("skew_class", ""),
        gap_variance_share=context.get("gap_variance_share"),
        prob_undefendable_gap=prob_gap,
        term_structure_ratio=context.get("term_structure_ratio"),
        event_suspected=bool(context.get("event_suspected")),
        accepted=accepted, rejections=tuple(rejections), warnings=tuple(warnings),
        rationale=_rationale(ticker, strike, expiration, fill, contracts, net,
                              prob_otm, ev_ann, ratio, basis_note, size, sample),
        open_interest=oi, option_volume=volume,
        ivr=(metrics or {}).get("ivr"), ivp=(metrics or {}).get("ivp"),
        iv_index=(metrics or {}).get("iv_index"),
        liquidity_rating=(metrics or {}).get("liquidity_rating"),
        events=tuple(event_hits),
    )


def _f(value) -> float | None:
    try:
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return None
        out = float(value)
        return out if np.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _rationale(ticker, strike, expiration, fill, contracts, net, prob_otm,
                ev_ann, ratio, basis_note, size, sample) -> str:
    bits = [f"Sell {contracts} {ticker} {expiration} ${strike:g} put at ~${fill:.2f} "
            f"for ${net:,.0f} net after fees."]
    if prob_otm is not None:
        bits.append(f"Empirically {prob_otm:.0%} of comparable windows finished above "
                    f"this strike ({sample}).")
    if ev_ann == ev_ann:
        bits.append(f"Expected value annualises to {ev_ann:.1%} on collateral, after "
                    f"charging the full empirical loss tail.")
    if ratio:
        bits.append(f"Implied vol is {ratio:.2f}x realized.")
    bits.append(size.explain() + ".")
    if basis_note:
        bits.append(basis_note.capitalize() + ".")
    return " ".join(bits)


# --- Sheet -----------------------------------------------------------------

def build_decision_sheet(tickers: list[str] | None = None,
                          reporter: BaseReporter | None = None,
                          include_rejected: bool = False) -> pd.DataFrame:
    """Rank every candidate strike across the universe by annualised EV."""
    return select_sheet(evaluate_universe(tickers, reporter=reporter),
                        include_rejected=include_rejected)


def evaluate_universe(tickers: list[str] | None = None,
                      reporter: BaseReporter | None = None) -> pd.DataFrame:
    """Every candidate strike across the universe, accepted AND rejected.

    Sorted by rank key (rejected last), with the rejection census on
    `frame.attrs["census"]`. This is what a run persists as
    `candidates.parquet` -- the full sheet, so a later page can show why a
    strike was turned down, not only what survived. `select_sheet` turns it
    into the proposal list.
    """
    from core.paths import load_universe
    from data_sources import chains
    from data_sources import events, tasty_metrics
    from data_sources.yfinance_sync import load_daily

    cfg = load_config()
    tickers = tickers or load_universe()
    reporter = reporter or NullReporter()
    account = sizing.account_from_config()
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

    rows: list[Recommendation] = []
    with reporter.stage("candidates", "Ranking candidates", total=len(tickers)):
        for ticker in tickers:
            try:
                chain, under = chains.load_chain(ticker)
                spot = chains.spot_from_underlying(under)
                if chain.empty or not spot:
                    reporter.advance(1, note=f"{ticker} no snapshot")
                    continue

                strikes = _candidate_strikes(chain, spot, today, cfg)
                if strikes.empty:
                    reporter.advance(1, note=f"{ticker} no strikes in band")
                    continue

                daily = load_daily(ticker, basis="price")
                adv = _adv_dollars(daily)

                # Per-ticker signals, computed once rather than per strike.
                context = _ticker_context(ticker, chain, spot)
                checks = {}
                for expiration in strikes["expiration"].dt.date.unique():
                    checks[expiration] = events.check(
                        ticker, today, expiration, "csp",
                        calendar_healthy=calendar["healthy"])

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
                                           metrics=metrics_by_symbol.get(ticker))
                    if rec is not None:
                        rows.append(rec)
                accepted = sum(1 for r in rows if r.ticker == ticker and r.accepted)
                reporter.advance(1, note=f"{ticker} {accepted} candidate(s)")
            except Exception as exc:
                reporter.advance(1, note=f"{ticker} error")
                reporter.log(f"{ticker}: {type(exc).__name__}: {exc}")

    if not rows:
        return pd.DataFrame()

    census = rejection_census(rows)
    dominant = (census["by_reason"][0][1] / max(census["strikes_examined"], 1)
                if census["by_reason"] else 0.0)
    if census["accepted"] == 0 or dominant > 0.6:
        prefix = "NO CANDIDATES PASSED. " if census["accepted"] == 0 else "GATE SKEW: "
        reporter.log(prefix + census["headline"])
        for reason, count in census["by_reason"][:5]:
            reporter.log(f"  {count:>4} x {reason}")

    frame = pd.DataFrame([r.to_dict() for r in rows])
    frame["rank_key"] = [r.rank_key for r in rows]
    frame = frame.sort_values("rank_key", ascending=False).reset_index(drop=True)
    frame.attrs["census"] = census
    return frame


def select_sheet(frame: pd.DataFrame, include_rejected: bool = False) -> pd.DataFrame:
    """The proposal list from `evaluate_universe`'s full sheet: accepted rows
    (unless `include_rejected`), best strike per ticker, run cap applied."""
    census = frame.attrs.get("census")
    if not frame.empty and not include_rejected:
        frame = frame[frame["accepted"]]
    if frame.empty:
        empty = pd.DataFrame()
        if census:
            empty.attrs["census"] = census
        return empty
    out = _one_per_ticker(frame.reset_index(drop=True), load_config())
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


def _one_per_ticker(frame: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Keep the best strike per ticker, then cap how many new trades a single
    run may propose.

    Without this the sheet fills with six adjacent strikes on the same name,
    which reads as six opportunities and is one.
    """
    if frame.empty:
        return frame
    best = frame.drop_duplicates(subset=["ticker"], keep="first")
    cap = cfg.get("management", {}).get("entry", {}).get("max_new_positions_per_run", 3)
    best = best.copy()
    best["proposed"] = [i < cap for i in range(len(best))]
    return best.reset_index(drop=True)


def _adv_dollars(daily: pd.DataFrame, window: int = 90) -> float | None:
    if daily.empty or "volume" not in daily.columns or len(daily) < 10:
        return None
    frame = daily.tail(window)
    dollars = (frame["close"].astype(float) * frame["volume"].astype(float)).mean()
    return float(dollars) if np.isfinite(dollars) else None
