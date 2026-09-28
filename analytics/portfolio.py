"""
Portfolio construction -- stop ranking trades in isolation.

Candidates are scored independently and the top few proposed. That is correct
for one trade and wrong for a book. Eight positions that fall together are one
position, and Phase 5 sharpened why it matters here specifically: the wheel's
worst outcome is not a loss, it is capital immobilised. AAPL's worst assigned
cycle in the 2008 regime ran 462 trading days. Survivable as one position.
Structural as twenty-five correlated ones, because the wheel simply stops
turning -- there is no cash left to sell puts with.

FOUR THINGS THIS ADDS
---------------------
**Correlation-aware selection.** Rank on marginal contribution to portfolio
risk, not standalone expected value. The second-best energy name, after you
already hold two, is not the third-best trade.

**Cluster exposure.** Sector tags cover 34 of 61 names; the rest are blank. So
grouping falls back to correlation clustering, which is arguably the better
primitive anyway -- it groups things that *move* together rather than things
that share an industry label.

**Simultaneous-assignment simulation.** Replays history and asks: over any
7-day window, how many of these positions would have finished in the money at
once, and how much capital would have converted to stock? The answer is a
distribution, and its tail is the number that decides position count.

**Capital recycling.** When does collateral actually free up? Proposals sized
against nominally-uncommitted cash are proposals you cannot fund.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from core.paths import load_config

MIN_OVERLAP = 250          # trading days two names must share to trust a correlation
DEFAULT_CLUSTER_THRESHOLD = 0.65


# --- Returns and correlation ----------------------------------------------

def return_matrix(tickers: list[str], years: int = 3) -> pd.DataFrame:
    """Aligned daily log returns, one column per ticker."""
    from data_sources.yfinance_sync import load_daily

    series = {}
    for ticker in tickers:
        frame = load_daily(ticker, basis="price")
        if frame.empty or len(frame) < MIN_OVERLAP:
            continue
        cutoff = frame["date"].max() - pd.DateOffset(years=years)
        frame = frame[frame["date"] >= cutoff]
        closes = frame.set_index("date")["close"].astype(float)
        series[ticker] = np.log(closes).diff()

    if not series:
        return pd.DataFrame()
    return pd.DataFrame(series).dropna(how="all")


def correlation_matrix(tickers: list[str], years: int = 3) -> pd.DataFrame:
    """Pairwise correlation, with thin pairs blanked rather than guessed.

    `min_periods` matters: two names that only overlap for forty days will
    produce a confident-looking correlation built on nothing, and a
    concentration limit enforced on that number is worse than no limit.
    """
    returns = return_matrix(tickers, years)
    if returns.empty:
        return pd.DataFrame()
    return returns.corr(min_periods=MIN_OVERLAP)


def cluster(tickers: list[str], threshold: float | None = None,
             years: int = 3) -> dict[str, int]:
    """Group tickers that move together, by single-linkage on correlation.

    Deliberately simple. The purpose is to stop the book quietly filling with
    six versions of the same bet, and single-linkage above a threshold does
    that. Anything more elaborate would imply a precision the input does not
    have.
    """
    threshold = threshold if threshold is not None else DEFAULT_CLUSTER_THRESHOLD
    corr = correlation_matrix(tickers, years)
    if corr.empty:
        return {t: i for i, t in enumerate(tickers)}

    names = list(corr.columns)
    parent = {n: n for n in names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, a in enumerate(names):
        for b in names[i + 1:]:
            value = corr.loc[a, b]
            if pd.notna(value) and value >= threshold:
                union(a, b)

    roots = {}
    out = {}
    for name in names:
        root = find(name)
        out[name] = roots.setdefault(root, len(roots))
    # Anything with too little history to correlate gets its own cluster rather
    # than being silently lumped in with everything else.
    for ticker in tickers:
        out.setdefault(ticker, len(roots) + len(out))
    return out


def sector_of(ticker: str) -> str:
    """Stage 2 category, falling back to 'untagged'."""
    import csv
    from core.paths import project_root

    path = project_root() / "output" / "stage2_quality_tags_master.csv"
    if not path.exists():
        return "untagged"
    try:
        with open(path, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("ticker", "").upper() == ticker.upper():
                    return (row.get("category") or "untagged").strip() or "untagged"
    except Exception:
        pass
    return "untagged"


# --- Marginal risk ---------------------------------------------------------

@dataclass
class MarginalRisk:
    ticker: str
    standalone_dollar_vol: float      # annualised $ risk of this position alone
    portfolio_dollar_vol_before: float
    portfolio_dollar_vol_after: float
    marginal_contribution: float      # $ risk the book actually gains
    diversification_benefit: float    # 1 - marginal/standalone; 1.0 = free lunch
    max_correlation: float
    most_correlated_with: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def marginal_risk(candidate: str, held: list[str],
                   collateral: dict[str, float] | None = None,
                   candidate_collateral: float | None = None,
                   years: int = 3) -> MarginalRisk | None:
    """How much dollar risk does adding `candidate` actually add?

    MEASURED IN DOLLARS, NOT NORMALISED WEIGHTS. That distinction is the whole
    point. A normalised-weight portfolio volatility *falls* every time you add
    a position, because the new name dilutes the existing ones -- which would
    make every candidate look risk-reducing and the metric worthless.

    In a cash-secured book, adding a position deploys additional capital; it
    does not reallocate what is already deployed. So the right quantity is the
    annualised dollar standard deviation of the whole book, and the marginal
    contribution is how much that grows. It is always positive. What varies --
    and what is worth ranking on -- is how much *less* than the position's
    standalone risk it adds, which is exactly the diversification benefit.
    """
    universe = list(dict.fromkeys(held + [candidate]))
    returns = return_matrix(universe, years)
    if returns.empty or candidate not in returns.columns:
        return None

    collateral = dict(collateral or {})
    # Exclude the candidate from the held list. It appears there whenever a
    # ticker you already own comes back up as a candidate, and a duplicate
    # label makes the covariance lookup return an oversized matrix.
    held_present = [h for h in dict.fromkeys(held)
                    if h in returns.columns and h != candidate]
    default = (float(np.median([collateral[h] for h in held_present]))
               if held_present and all(h in collateral for h in held_present)
               else 100_000.0)
    exposure = {t: float(collateral.get(t, default)) for t in held_present}
    exposure[candidate] = float(
        candidate_collateral if candidate_collateral is not None
        else collateral.get(candidate, default))

    cov = returns[held_present + [candidate]].cov() * 252.0

    def dollar_vol(names: list[str]) -> float:
        if not names:
            return 0.0
        weights = np.array([exposure[n] for n in names], dtype=float)
        variance = float(weights @ cov.loc[names, names].values @ weights)
        return float(np.sqrt(max(variance, 0.0)))

    standalone = dollar_vol([candidate])
    before = dollar_vol(held_present)
    after = dollar_vol(held_present + [candidate])
    marginal = after - before

    if held_present:
        corr = returns[held_present + [candidate]].corr()
        pair = corr.loc[candidate, held_present].dropna()
        max_corr = float(pair.max()) if len(pair) else 0.0
        partner = str(pair.idxmax()) if len(pair) else None
    else:
        max_corr, partner = 0.0, None

    benefit = 1.0 - (marginal / standalone) if standalone > 0 else 0.0
    return MarginalRisk(candidate, standalone, before, after, marginal,
                         float(np.clip(benefit, 0.0, 1.0)), max_corr, partner)


# --- Selection -------------------------------------------------------------

@dataclass
class Selection:
    accepted: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    clusters: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def select(candidates: pd.DataFrame, held: list[str] | None = None,
            max_new: int | None = None, years: int = 3) -> Selection:
    """Greedy correlation-aware selection over a ranked candidate sheet.

    Walks the list in EV order and accepts a candidate only if it clears the
    cluster and sector limits given everything already accepted. The rejected
    list keeps the reason, so a good trade that lost on concentration is
    visible rather than silently absent -- you may still want it, and the tool
    should not pretend it was never there.
    """
    result = Selection()
    if candidates.empty:
        return result

    cfg = load_config().get("portfolio", {})
    max_per_cluster = int(cfg.get("max_positions_per_cluster", 2))
    max_cluster_pct = float(cfg.get("max_cluster_collateral_pct", 0.25))
    max_sector_pct = float(cfg.get("max_sector_collateral_pct", 0.25))
    max_corr = float(cfg.get("max_pairwise_correlation", 0.80))
    nlv = float(load_config().get("account", {}).get("net_liquidating_value", 0.0))
    max_new = max_new if max_new is not None else int(
        load_config().get("management", {}).get("entry", {}).get(
            "max_new_positions_per_run", 3))

    held = held or []
    tickers = list(dict.fromkeys(list(candidates["ticker"]) + held))
    assignment = cluster(tickers, years=years)
    result.clusters = assignment

    corr = correlation_matrix(tickers, years)
    accepted_tickers = list(held)
    held_collateral: dict[str, float] = {}
    cluster_count: dict[int, int] = {}
    cluster_dollars: dict[int, float] = {}
    sector_dollars: dict[str, float] = {}

    for ticker in held:
        key = assignment.get(ticker, -1)
        cluster_count[key] = cluster_count.get(key, 0) + 1

    for _, row in candidates.iterrows():
        ticker = str(row["ticker"]).upper()
        collateral = float(row.get("collateral", 0.0))
        key = assignment.get(ticker, -1)
        sector = sector_of(ticker)
        reasons = []

        if cluster_count.get(key, 0) >= max_per_cluster:
            peers = [t for t, c in assignment.items()
                     if c == key and t in accepted_tickers]
            reasons.append(
                f"already holding {cluster_count[key]} position(s) in the same "
                f"correlation cluster ({', '.join(peers[:4])}) -- these move "
                f"together, so a third is more of the same bet rather than "
                f"diversification")

        if nlv and (cluster_dollars.get(key, 0.0) + collateral) / nlv > max_cluster_pct:
            reasons.append(
                f"cluster collateral would reach "
                f"{(cluster_dollars.get(key, 0.0) + collateral) / nlv:.0%} of the "
                f"account, over the {max_cluster_pct:.0%} limit")

        if nlv and (sector_dollars.get(sector, 0.0) + collateral) / nlv > max_sector_pct \
                and sector != "untagged":
            reasons.append(
                f"{sector} exposure would reach "
                f"{(sector_dollars.get(sector, 0.0) + collateral) / nlv:.0%}, over the "
                f"{max_sector_pct:.0%} sector limit")

        if not corr.empty and ticker in corr.columns:
            pair = corr.loc[ticker, [t for t in accepted_tickers
                                      if t in corr.columns]].dropna()
            if len(pair) and float(pair.max()) > max_corr:
                reasons.append(
                    f"{float(pair.max()):.2f} correlated with "
                    f"{pair.idxmax()}, over the {max_corr:.2f} limit")

        record = row.to_dict()
        record["cluster"] = key
        record["sector"] = sector
        risk = marginal_risk(ticker, accepted_tickers,
                              collateral=held_collateral,
                              candidate_collateral=collateral, years=years)
        if risk:
            record["marginal_risk"] = risk.marginal_contribution
            record["diversification_benefit"] = risk.diversification_benefit
            record["max_correlation"] = risk.max_correlation
            record["most_correlated_with"] = risk.most_correlated_with

        if reasons:
            record["rejection_reasons"] = reasons
            result.rejected.append(record)
            continue

        if len(result.accepted) >= max_new:
            record["rejection_reasons"] = [
                f"passed every limit but the run proposes at most {max_new} new "
                f"position(s); this is next in line"]
            result.rejected.append(record)
            continue

        result.accepted.append(record)
        accepted_tickers.append(ticker)
        held_collateral[ticker] = collateral
        cluster_count[key] = cluster_count.get(key, 0) + 1
        cluster_dollars[key] = cluster_dollars.get(key, 0.0) + collateral
        sector_dollars[sector] = sector_dollars.get(sector, 0.0) + collateral

    if result.rejected and not result.accepted:
        result.notes.append(
            "Every candidate was blocked on concentration. That usually means the "
            "book is already leaning hard in one direction -- worth looking at the "
            "cluster exposure below before overriding.")
    return result


# --- Simultaneous assignment ----------------------------------------------

@dataclass
class StressResult:
    positions: int
    horizon_days: int
    windows_tested: int
    mean_assigned: float
    p50_assigned: int
    p90_assigned: int
    p99_assigned: int
    worst_assigned: int
    worst_date: str | None
    worst_capital_converted: float
    total_collateral: float
    worst_pct_converted: float
    all_assigned_ever: bool
    verdict: str

    def to_dict(self) -> dict:
        return asdict(self)


def simultaneous_assignment(book: list[dict], horizon: int = 7,
                             years: int = 20) -> StressResult | None:
    """How many of these positions would have been assigned at the same time?

    Replays every historical window of `horizon` trading days, applies each
    position's moneyness to the returns actually observed, and counts how many
    would have finished in the money together. This is the scenario that stops
    a cash-secured wheel: not one bad trade, but the whole book converting to
    stock in the same week and leaving nothing to sell puts with.

    `book` entries need `ticker`, `strike`, `spot` and `collateral`.
    """
    from data_sources.yfinance_sync import load_daily

    if not book:
        return None

    series = {}
    moneyness = {}
    collateral = {}
    for entry in book:
        ticker = str(entry["ticker"]).upper()
        spot, strike = float(entry.get("spot", 0)), float(entry.get("strike", 0))
        if spot <= 0 or strike <= 0:
            continue
        frame = load_daily(ticker, basis="price")
        if frame.empty or len(frame) < horizon + MIN_OVERLAP:
            continue
        cutoff = frame["date"].max() - pd.DateOffset(years=years)
        frame = frame[frame["date"] >= cutoff]
        closes = frame.set_index("date")["close"].astype(float)
        # Forward return over the horizon, from each possible entry day.
        series[ticker] = closes.shift(-horizon) / closes - 1.0
        moneyness[ticker] = strike / spot - 1.0        # negative for an OTM put
        collateral[ticker] = float(entry.get("collateral", strike * 100))

    if not series:
        return None

    forward = pd.DataFrame(series).dropna()
    if forward.empty:
        return None

    breached = pd.DataFrame(
        {t: forward[t] <= moneyness[t] for t in forward.columns})
    counts = breached.sum(axis=1)
    dollars = breached.mul(pd.Series(collateral)).sum(axis=1)

    total = float(sum(collateral.values()))
    worst_index = int(counts.values.argmax())
    worst_count = int(counts.iloc[worst_index])
    worst_date = str(pd.Timestamp(counts.index[worst_index]).date())
    worst_dollars = float(dollars.iloc[worst_index])

    n = len(collateral)
    p90 = int(np.percentile(counts, 90))
    p99 = int(np.percentile(counts, 99))
    pct = worst_dollars / total if total else 0.0

    if worst_count == n:
        verdict = (f"On {worst_date}, ALL {n} positions would have been assigned in the "
                   f"same {horizon}-day window -- ${worst_dollars:,.0f} of collateral "
                   f"converting to stock at once, with nothing left to sell puts "
                   f"against. This book is one correlated bet.")
    elif pct > 0.5:
        verdict = (f"Worst historical window converts {pct:.0%} of collateral "
                   f"(${worst_dollars:,.0f}) to stock at once, on {worst_date}. The "
                   f"wheel would have largely stopped turning for the duration.")
    elif p99 >= max(n // 2, 2):
        verdict = (f"In the worst 1% of windows, {p99} of {n} positions assign "
                   f"together. Uncomfortable but survivable -- keep a cash buffer "
                   f"sized to it.")
    else:
        verdict = (f"Well spread: even the worst 1% of windows assigns only {p99} of "
                   f"{n} positions together (worst ever {worst_count} on {worst_date}).")

    return StressResult(
        positions=n, horizon_days=horizon, windows_tested=len(forward),
        mean_assigned=float(counts.mean()),
        p50_assigned=int(np.percentile(counts, 50)),
        p90_assigned=p90, p99_assigned=p99,
        worst_assigned=worst_count, worst_date=worst_date,
        worst_capital_converted=worst_dollars, total_collateral=total,
        worst_pct_converted=pct, all_assigned_ever=worst_count == n,
        verdict=verdict)


# --- Capital recycling -----------------------------------------------------

def recycling_schedule() -> pd.DataFrame:
    """When does committed collateral actually come back?

    Open puts free their collateral at expiry. Assigned share lots do not free
    anything until the shares are called away or sold -- and Phase 5 measured
    that at a median of 6 sessions but a p90 of 43 and a worst case in the
    hundreds. Sizing new proposals against capital that is nominally
    uncommitted but actually parked in stock is how a book runs out of cash
    while looking fully funded.
    """
    from analytics import paper

    rows = []
    positions = paper.list_positions(status="open", book="taken")
    if not positions.empty:
        for _, row in positions.iterrows():
            rows.append({
                "kind": "put_spread" if row.get("strategy") == "pcs" else "short_put",
                "ticker": row["ticker"],
                "frees_on": pd.Timestamp(row["expiration"]).date(),
                "capital": float(row.get("collateral") or 0.0),
                "certain": True,
                "note": ("returns at expiry, or earlier if closed" if row.get("strategy") == "pcs"
                         else "returns at expiry unless assigned"),
            })

    lots = paper.list_share_lots(open_only=True)
    if not lots.empty:
        for _, lot in lots.iterrows():
            rows.append({
                "kind": "shares", "ticker": lot["ticker"],
                "frees_on": None,
                "capital": float(lot["shares"]) * float(lot["adjusted_basis"]),
                "certain": False,
                "note": "locked until called away or sold -- no scheduled release",
            })

    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.sort_values(["certain", "frees_on"], ascending=[False, True])
    return frame


def exposure_summary(book: list[dict]) -> pd.DataFrame:
    """Collateral by correlation cluster and by sector."""
    if not book:
        return pd.DataFrame()
    tickers = [str(e["ticker"]).upper() for e in book]
    assignment = cluster(tickers)
    nlv = float(load_config().get("account", {}).get("net_liquidating_value", 0.0))

    rows = []
    for entry in book:
        ticker = str(entry["ticker"]).upper()
        rows.append({
            "ticker": ticker,
            "cluster": assignment.get(ticker, -1),
            "sector": sector_of(ticker),
            "collateral": float(entry.get("collateral", 0.0)),
        })
    frame = pd.DataFrame(rows)
    grouped = frame.groupby("cluster").agg(
        tickers=("ticker", lambda s: ", ".join(sorted(s))),
        positions=("ticker", "size"),
        collateral=("collateral", "sum")).reset_index()
    if nlv:
        grouped["pct_of_account"] = grouped["collateral"] / nlv
    return grouped.sort_values("collateral", ascending=False)
