"""
Overnight gap risk -- the tail a daily-bar model cannot see.

THE ARGUMENT FOR THIS MODULE
----------------------------
Every probability in this tool so far is built from daily closes. That is the
right primitive for "where will the stock finish", and it is the wrong one for
"how does a short put actually get hurt".

A short put is rarely destroyed by drift. It is destroyed by a gap: the stock
closes at 52 on Tuesday, an announcement lands at 06:40, and it opens at 44 on
Wednesday. There was no moment in between at which the position could have been
defended, rolled, or closed. Daily bars record that as a single -15% day and
imply, wrongly, that it was a path you could have reacted to.

`data/raw_1m/` -- 4.6 GB, all 61 tickers back to 2000 -- is the one dataset
that can separate the two, and it has been sitting unread for that purpose
since the project began. Splitting each day's move into its overnight and
intraday components answers three questions daily data cannot:

  1. **How much of the downside is undefendable?** If 70% of a name's worst
     moves happen between the close and the open, tight stop discipline and
     fast rolling are worth very little on it.
  2. **Is the tail fatter than the body suggests?** Gap distributions are far
     more leptokurtic than intraday ones. A name can look calm on daily vol and
     carry a vicious overnight tail.
  3. **What is the real worst case for a weekly put?** A 7-DTE position carries
     six overnight gaps. Their joint tail, not the daily vol, is the number
     that should size it.

CONVENTIONS
-----------
Gap is measured close-to-open on the REGULAR session: previous 16:00 close to
the following 09:30 open. Extended-hours prints are deliberately excluded --
they are thin, often unfillable, and including them would understate the gap by
pricing in liquidity that was not really there for size.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from core.paths import db_1m_cache, load_config

RTH_OPEN = dt.time(9, 30)
RTH_CLOSE = dt.time(16, 0)
MIN_OBSERVATIONS = 250


# --- Building the gap series ----------------------------------------------

def _from_cache(ticker: str, start: str | None = None) -> pd.DataFrame:
    """Regular-session open and close per day, from the 1-minute cache."""
    import duckdb

    path = db_1m_cache()
    if not path.exists():
        return pd.DataFrame()

    con = duckdb.connect(str(path), read_only=True)
    try:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        if "bars_1m" not in tables:
            return pd.DataFrame()
        where = "WHERE ticker = ?"
        params: list = [ticker]
        if start:
            where += " AND datetime >= ?"
            params.append(start)
        # first and last regular-session minute of each day
        query = f"""
            SELECT CAST(datetime AS DATE) AS date,
                   arg_min(open, datetime)  AS rth_open,
                   arg_max(close, datetime) AS rth_close,
                   min(low)  AS rth_low,
                   max(high) AS rth_high
            FROM bars_1m
            {where}
              AND CAST(datetime AS TIME) >= TIME '09:30:00'
              AND CAST(datetime AS TIME) <  TIME '16:00:00'
            GROUP BY 1 ORDER BY 1
        """
        frame = con.execute(query, params).fetchdf()
    except Exception:
        return pd.DataFrame()
    finally:
        con.close()

    if frame.empty:
        return frame
    frame["date"] = pd.to_datetime(frame["date"])
    return frame


def _from_text_archive(ticker: str, months: int = 240) -> pd.DataFrame:
    """Fallback: read the monthly 1-minute text files directly.

    Slower than the DuckDB cache but means gap analysis works before
    `scripts/06` has been run -- which matters, because the cache is one more
    thing that can quietly be missing.
    """
    from data_sources.massive_sync import archive_root

    folder = archive_root() / ticker
    if not folder.is_dir():
        return pd.DataFrame()

    files = sorted(folder.glob(f"{ticker}_*_1m.txt"))[-months:]
    rows = []
    for path in files:
        try:
            frame = pd.read_csv(path)
        except Exception:
            continue
        if frame.empty or "Datetime" not in frame.columns:
            continue
        frame["Datetime"] = pd.to_datetime(frame["Datetime"], errors="coerce")
        frame = frame.dropna(subset=["Datetime"]).sort_values("Datetime")
        times = frame["Datetime"].dt.time
        session = frame[(times >= RTH_OPEN) & (times < RTH_CLOSE)]
        if session.empty:
            continue
        grouped = session.groupby(session["Datetime"].dt.date)
        rows.append(pd.DataFrame({
            "date": pd.to_datetime(list(grouped.groups.keys())),
            "rth_open": grouped["Open"].first().values,
            "rth_close": grouped["Close"].last().values,
            "rth_low": grouped["Low"].min().values,
            "rth_high": grouped["High"].max().values,
        }))
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True).sort_values("date").reset_index(drop=True)


def session_frame(ticker: str, years: int = 20) -> pd.DataFrame:
    """Regular-session OHLC per day, from whichever source is available."""
    start = (dt.date.today() - dt.timedelta(days=int(years * 365.25))).isoformat()
    frame = _from_cache(ticker, start)
    if frame.empty:
        frame = _from_text_archive(ticker)
    if frame.empty:
        return frame
    frame = frame[frame["date"] >= pd.Timestamp(start)]
    return frame.reset_index(drop=True)


def _corporate_action_mask(frame: pd.DataFrame, ticker: str,
                            tolerance: float = 0.04) -> pd.Series:
    """Flag days where the 1-minute archive disagrees with adjusted daily bars.

    WHY THIS IS NECESSARY. The 1-minute archive is not consistently corporate-
    action adjusted -- it was assembled from two sources across two eras, and a
    split baked into one side and not the other shows up as a discontinuity at
    the seam. The first run of this module reported AAPL's worst overnight gap
    as -76.5% on 2018-02-01. Nothing happened to AAPL that day; -76.5% is a 4:1
    split ratio appearing where two differently-adjusted files meet. It dragged
    AAPL's overnight volatility to 78% against a true figure near 30%, and put
    it second in a table ranking undefendable risk.

    The yfinance price basis (`load_daily(basis="price")`, split-adjusted
    consistently across its whole history) is the ground truth. It is
    deliberately NOT dividend adjusted: the archive is not either, so an
    ex-dividend drop appears in both and agrees, instead of being misread as
    a seam and excluded (Phase 8 -- the pre-Phase-8 total-return reference
    flagged ex-dates on high-yield names as disagreements). Any day where the archive's close-to-close return differs from the
    adjusted one by more than `tolerance` is a corporate action or a data seam,
    not a market move, and is excluded. Excluding is right rather than
    rescaling: at a seam we do not know which side is correct.
    """
    from data_sources.yfinance_sync import load_daily

    adjusted = load_daily(ticker, basis="price")
    if adjusted.empty:
        # No ground truth available. Fall back to an absolute sanity bound --
        # a genuine overnight move beyond 50% is vanishingly rare and a split
        # is not, so the prior favours exclusion.
        return frame["total"].abs() > 0.50

    reference = adjusted.set_index("date")["close"].astype(float)
    reference_return = reference.pct_change()
    merged = frame.set_index("date").join(
        reference_return.rename("adjusted_return"), how="left")
    disagreement = (merged["total"] - merged["adjusted_return"]).abs()
    # A missing reference day cannot be validated; keep it unless it is
    # implausible on its own terms.
    mask = disagreement > tolerance
    mask = mask.fillna(frame.set_index("date")["total"].abs() > 0.50)
    return mask.reset_index(drop=True)


def gap_series(ticker: str, years: int = 20,
                exclude_corporate_actions: bool = True) -> pd.DataFrame:
    """Split each day's move into its overnight and intraday components.

        overnight = today's 09:30 open  / yesterday's 16:00 close - 1
        intraday  = today's 16:00 close / today's 09:30 open      - 1
        total     = today's close       / yesterday's close       - 1

    The decomposition is exact up to compounding, which is what makes the
    variance shares below meaningful rather than approximate.
    """
    frame = session_frame(ticker, years)
    if frame.empty or len(frame) < 30:
        return pd.DataFrame()

    frame = frame.copy()
    previous_close = frame["rth_close"].shift(1)
    frame["overnight"] = frame["rth_open"] / previous_close - 1.0
    frame["intraday"] = frame["rth_close"] / frame["rth_open"] - 1.0
    frame["total"] = frame["rth_close"] / previous_close - 1.0
    # Gap direction relative to the prior close, measured at the worst point
    # of the following session -- what a short put would actually have faced.
    frame["overnight_low"] = frame["rth_low"] / previous_close - 1.0
    frame = frame.dropna(subset=["overnight", "intraday"]).reset_index(drop=True)

    if exclude_corporate_actions and not frame.empty:
        contaminated = _corporate_action_mask(frame, ticker)
        frame["excluded"] = contaminated.to_numpy()
        removed = int(frame["excluded"].sum())
        frame = frame[~frame["excluded"]].reset_index(drop=True)
        frame.attrs["excluded_days"] = removed
    else:
        frame.attrs["excluded_days"] = 0
    return frame


# --- Statistics ------------------------------------------------------------

@dataclass
class GapProfile:
    ticker: str
    observations: int
    start_date: str
    end_date: str
    source: str

    overnight_vol: float          # annualised
    intraday_vol: float
    total_vol: float
    overnight_variance_share: float   # fraction of daily variance from gaps

    overnight_p01: float
    overnight_p05: float
    overnight_p50: float
    overnight_p95: float
    overnight_p99: float
    worst_gap: float
    worst_gap_date: str

    gap_kurtosis: float
    intraday_kurtosis: float
    tail_ratio: float             # |overnight p01| / |intraday p01|

    prob_gap_below_2pct: float
    prob_gap_below_5pct: float
    prob_gap_below_10pct: float

    undefendable_share: float     # share of worst days driven by the gap
    excluded_days: int            # corporate actions / data seams removed
    note: str

    def to_dict(self) -> dict:
        return asdict(self)


def profile(ticker: str, years: int = 20) -> GapProfile | None:
    """The full overnight-risk picture for one ticker."""
    frame = gap_series(ticker, years)
    if frame.empty or len(frame) < MIN_OBSERVATIONS:
        return None

    overnight = frame["overnight"].to_numpy()
    intraday = frame["intraday"].to_numpy()
    total = frame["total"].to_numpy()

    on_vol = float(np.std(overnight) * np.sqrt(252))
    id_vol = float(np.std(intraday) * np.sqrt(252))
    tot_vol = float(np.std(total) * np.sqrt(252))
    # Variance shares, not vol shares -- variances add, volatilities do not.
    share = float(np.var(overnight) / np.var(total)) if np.var(total) > 0 else float("nan")

    worst_index = int(np.argmin(overnight))
    percentiles = np.percentile(overnight, [1, 5, 50, 95, 99])

    on_p01 = float(percentiles[0])
    id_p01 = float(np.percentile(intraday, 1))
    tail_ratio = abs(on_p01) / abs(id_p01) if id_p01 != 0 else float("nan")

    # Of the worst 5% of days, how much of the fall happened before the open?
    threshold = np.percentile(total, 5)
    worst_days = frame[frame["total"] <= threshold]
    if len(worst_days):
        contribution = (worst_days["overnight"].clip(upper=0).sum()
                        / worst_days["total"].sum())
        undefendable = float(np.clip(contribution, 0.0, 1.0))
    else:
        undefendable = float("nan")

    excluded = int(frame.attrs.get("excluded_days", 0))
    if excluded > len(frame) * 0.02:
        note = (f"{excluded} day(s) excluded as corporate actions or archive seams -- "
                f"more than 2% of the sample. The 1-minute history for this name has "
                f"adjustment problems; treat these figures as provisional.")
    elif share > 0.5:
        note = (f"Most of this name's daily variance ({share:.0%}) happens overnight. "
                f"Intraday defence -- rolling, closing, watching the tape -- can only "
                f"act on the smaller half of the risk.")
    elif tail_ratio > 1.5:
        note = (f"The body is calm but the overnight tail is {tail_ratio:.1f}x the "
                f"intraday tail. Daily volatility understates what a short put is "
                f"actually exposed to.")
    else:
        note = (f"Gap risk is proportionate: {share:.0%} of variance overnight, tail "
                f"ratio {tail_ratio:.1f}x. Nothing unusual.")

    return GapProfile(
        ticker=ticker, observations=len(frame),
        start_date=str(frame["date"].iloc[0].date()),
        end_date=str(frame["date"].iloc[-1].date()),
        source="1-minute regular session",
        overnight_vol=on_vol, intraday_vol=id_vol, total_vol=tot_vol,
        overnight_variance_share=share,
        overnight_p01=on_p01, overnight_p05=float(percentiles[1]),
        overnight_p50=float(percentiles[2]), overnight_p95=float(percentiles[3]),
        overnight_p99=float(percentiles[4]),
        worst_gap=float(overnight[worst_index]),
        worst_gap_date=str(frame["date"].iloc[worst_index].date()),
        gap_kurtosis=float(pd.Series(overnight).kurtosis()),
        intraday_kurtosis=float(pd.Series(intraday).kurtosis()),
        tail_ratio=float(tail_ratio),
        prob_gap_below_2pct=float((overnight <= -0.02).mean()),
        prob_gap_below_5pct=float((overnight <= -0.05).mean()),
        prob_gap_below_10pct=float((overnight <= -0.10).mean()),
        undefendable_share=undefendable,
        excluded_days=int(frame.attrs.get("excluded_days", 0)), note=note)


# --- What it means for a specific trade -----------------------------------

@dataclass
class GapRisk:
    ticker: str
    strike: float
    spot: float
    moneyness: float
    trading_days: int
    overnight_exposures: int
    prob_gap_through_strike: float      # any single gap clears the strike outright
    prob_any_gap_through: float         # at least one over the position's life
    expected_gap_loss: float            # per share, conditional on it happening
    worst_historical_gap_loss: float
    verdict: str

    def to_dict(self) -> dict:
        return asdict(self)


def trade_gap_risk(ticker: str, spot: float, strike: float,
                    trading_days: int, years: int = 20) -> GapRisk | None:
    """Could a single overnight move take this strike out on its own?

    Distinct from the empirical breach probability in `moves.py`, which counts
    terminal outcomes over the whole window. This asks a sharper question: what
    are the odds of a move you could not have reacted to at all?

    A 7-DTE put carries six overnight exposures. Treating them as independent
    overstates the joint probability slightly -- gaps cluster -- so the figure
    is a mild upper bound, and is labelled as one.
    """
    frame = gap_series(ticker, years)
    if frame.empty or len(frame) < MIN_OVERLAP_FOR_TRADE:
        return None
    if spot <= 0 or strike <= 0:
        return None

    moneyness = strike / spot - 1.0      # negative for an OTM put
    overnight = frame["overnight"].to_numpy()

    through = overnight <= moneyness
    single = float(through.mean())
    exposures = max(trading_days, 1)
    joint = float(1.0 - (1.0 - single) ** exposures)

    if through.any():
        shortfall = (moneyness - overnight[through]) * spot
        expected = float(shortfall.mean())
        worst = float((moneyness - overnight.min()) * spot)
    else:
        expected, worst = 0.0, 0.0

    if joint >= 0.05:
        verdict = (f"{joint:.1%} chance that a single overnight move clears "
                   f"${strike:g} outright at some point in the next {exposures} "
                   f"sessions. That is a move with no opportunity to defend -- "
                   f"size the position as if the roll engine did not exist.")
    elif joint >= 0.01:
        verdict = (f"{joint:.1%} chance of an undefendable gap through the strike. "
                   f"Low but not negligible over {exposures} overnight exposures.")
    else:
        verdict = (f"Gap risk through this strike is minimal ({joint:.2%} over "
                   f"{exposures} sessions).")

    return GapRisk(
        ticker=ticker, strike=strike, spot=spot, moneyness=float(moneyness),
        trading_days=trading_days, overnight_exposures=exposures,
        prob_gap_through_strike=single, prob_any_gap_through=joint,
        expected_gap_loss=expected, worst_historical_gap_loss=worst,
        verdict=verdict)


MIN_OVERLAP_FOR_TRADE = 250


def universe_profiles(tickers: list[str] | None = None, years: int = 20,
                       reporter=None) -> pd.DataFrame:
    """Gap profile for every ticker, ranked by how much risk is undefendable."""
    from core.paths import load_universe
    from core.progress import NullReporter

    tickers = tickers or load_universe()
    reporter = reporter or NullReporter()
    rows = []
    with reporter.stage("gaps", "Overnight gap profiles", total=len(tickers)):
        for ticker in tickers:
            try:
                result = profile(ticker, years)
                if result:
                    rows.append(result.to_dict())
                    reporter.advance(1, note=f"{ticker} "
                                              f"{result.overnight_variance_share:.0%} overnight")
                else:
                    reporter.advance(1, note=f"{ticker} insufficient 1-minute data")
            except Exception as exc:
                reporter.advance(1, note=f"{ticker} error")
                reporter.log(f"{ticker}: {type(exc).__name__}: {exc}")

    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    return frame.sort_values("overnight_variance_share", ascending=False
                              ).reset_index(drop=True)
