"""
Phase 5 validation suite -- run everything that checks whether the engine is
telling the truth.

    python scripts/validate.py                    # everything
    python scripts/validate.py --regimes          # regime scorecard only
    python scripts/validate.py --walk-forward     # walk-forward only
    python scripts/validate.py --calibration      # paper-book calibration only
    python scripts/validate.py --iv-coverage      # how close IV rank is to active

Each section writes a CSV to `output/` and prints a verdict. Nothing changes
`config.yaml` -- these produce evidence for decisions, not the decisions.

ORDER OF EVIDENCE
-----------------
Regimes ask whether a *ticker* is a durable wheel candidate. Walk-forward asks
whether the *parameters* survive out of sample. Calibration asks whether the
*probabilities* match reality. Only the third depends on live trading, which is
why the first two can run today and the third accumulates over months.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.paths import load_universe, output_dir  # noqa: E402
from core.progress import ConsoleReporter  # noqa: E402


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n  {title}\n{'=' * 78}")


def check_data_ready() -> bool:
    """Diagnose the data layer ONCE, before running anything over 61 tickers.

    Without this, a missing table renders as 61 identical "no history" notes and
    a summary that blames the tickers. One upfront check names the actual cause
    and the one command that fixes it.
    """
    from data_sources.yfinance_sync import daily_data_status

    status = daily_data_status()
    if status["source"] == "daily_bars_tr":
        print(f"  Daily bars: {status['total_return_rows']:,} rows across "
              f"{status['total_return_tickers']} tickers "
              f"({status['adjusted']}-adjusted)")
        if status.get("mixed_basis"):
            print(f"\n  WARNING -- {status['action']}\n")
            print("  Proceeding, but tickers scored on different price bases are not"
                  "\n  directly comparable. Treat cross-ticker rankings with caution"
                  "\n  until the sync completes.\n")
        elif status.get("coverage"):
            print(f"  Universe coverage: {status['universe_covered']}/"
                  f"{status['universe_size']} ({status['coverage']:.0%})")
        return True

    if status["source"] == "daily_bars":
        print(f"  Daily bars: {status['legacy_rows']:,} rows across "
              f"{status['legacy_tickers']} tickers, SPLIT-ADJUSTED ONLY")
        print(f"  {status['action']}")
        print("  Proceeding on the legacy table -- results are directionally valid "
              "but\n  understate returns on dividend payers.")
        return True

    print(f"\n  NO DAILY PRICE DATA.\n")
    print(f"    database : {status['database']}")
    print(f"    exists   : {status['exists']}")
    print(f"    tr rows  : {status['total_return_rows']:,}")
    print(f"    legacy   : {status['legacy_rows']:,}")
    print(f"\n  {status['action']}\n")
    print("  Every ticker will report 'no history' until this is populated -- that "
          "is\n  one missing pipeline stage, not 61 data problems.")
    return False


def run_regimes(tickers, years: int) -> None:
    from analytics.regimes import build

    rule("REGIME SCORECARD -- does the edge survive every environment?")
    reporter = ConsoleReporter([("regimes", "Regime scorecard")])
    detail, consistency = build(tickers, years=years, reporter=reporter)

    if consistency.empty:
        print("\n  Not enough cycles per regime. Populate the daily database first:"
              "\n    python pipeline/run.py")
        return

    detail.to_csv(output_dir() / "regime_detail.csv", index=False)
    consistency.to_csv(output_dir() / "regime_consistency.csv", index=False)

    print(f"\n{'ticker':<8} {'tier':<10} {'score':>6} {'regimes+':>9} "
          f"{'median':>8} {'worst':>8} {'stress':>8} {'worst cyc':>10}")
    for row in consistency.head(20).itertuples():
        print(f"{row.ticker:<8} {str(row.tier):<10} {row.consistency_score:>6.2f} "
              f"{row.regimes_positive:>4}/{row.regimes_covered:<4} "
              f"{row.median_annualised:>7.1%} {row.worst_regime_annualised:>7.1%} "
              f"{row.stress_median:>7.1%} {row.worst_cycle_days:>9}d")

    tiers = consistency["tier"].value_counts()
    print(f"\n  Tiers: " + "  ".join(f"{k} {v}" for k, v in tiers.items()))
    avoid = consistency[consistency["tier"] == "avoid"]["ticker"].tolist()
    if avoid:
        print(f"  Weakest names: {', '.join(avoid[:12])}"
              + (" ..." if len(avoid) > 12 else ""))
    print(f"\n  Written to {output_dir() / 'regime_consistency.csv'}")


def run_walk_forward(tickers, years: int) -> None:
    from analytics.walkforward import across_universe

    rule("WALK-FORWARD -- do the fitted parameters survive out of sample?")
    reporter = ConsoleReporter([("walkforward", "Walk-forward validation")])
    summary, pooled = across_universe(tickers, years=years, reporter=reporter)

    if summary.empty:
        print("\n  No usable tickers. Populate the daily database first.")
        return

    summary.to_csv(output_dir() / "walkforward_summary.csv", index=False)

    print(f"\n{'ticker':<8} {'folds':>6} {'in-samp':>9} {'out-samp':>9} "
          f"{'baseline':>9} {'degrade':>8} {'stability':>10}")
    for row in summary.head(20).itertuples():
        print(f"{row.ticker:<8} {row.n_folds:>6} {row.mean_in_sample:>8.1%} "
              f"{row.mean_out_of_sample:>8.1%} {row.mean_baseline_oos:>8.1%} "
              f"{row.degradation_ratio:>7.0%} {row.parameter_stability:>9.0%}")

    print(f"\n  POOLED across {pooled['tickers']} tickers, "
          f"{pooled['total_folds']} folds")
    print(f"    in-sample        {pooled['mean_in_sample']:>7.1%}")
    print(f"    out-of-sample    {pooled['mean_out_of_sample']:>7.1%}")
    print(f"    fixed baseline   {pooled['mean_baseline_oos']:>7.1%}")
    print(f"    degradation      {pooled['median_degradation_ratio']:>7.0%}")
    print(f"    param stability  {pooled['mean_parameter_stability']:>7.0%}")
    print(f"    adaptive wins    {pooled['adaptive_beat_rate']:>7.0%} of folds")
    print(f"\n  {pooled['verdict']}")

    if pooled["mean_parameter_stability"] < 0.35:
        print("\n  Parameter selection is UNSTABLE across folds. Even where the "
              "\n  strategy survives, the specific optimum does not repeat -- which "
              "\n  means the grid is largely measuring noise. Fix one defensible "
              "\n  setting rather than re-tuning.")
    if pooled["adaptive_beat_rate"] < 0.55:
        print("\n  Re-optimising does not reliably beat a fixed baseline. Stop tuning.")
    print(f"\n  Written to {output_dir() / 'walkforward_summary.csv'}")


def run_calibration() -> None:
    from analytics.calibration import report

    rule("CALIBRATION -- are the probabilities and fills honest?")
    result = report()

    performance = result["performance"]
    if not performance.get("n_closed"):
        print("\n  No closed paper positions yet. This section becomes meaningful "
              "\n  once you have recorded ~25 outcomes on the Decisions page, and "
              "\n  trustworthy at ~100. Nothing else in Phase 5 depends on it.")
        return

    probability = result["probability"]
    if probability:
        print(f"\n  PROBABILITIES  ({probability['n']} closed outcomes)")
        print(f"    predicted     {probability['mean_predicted']:>7.1%}")
        print(f"    realised      {probability['mean_actual']:>7.1%}")
        print(f"    brier         {probability['brier']:>7.4f}  "
              f"(reliability {probability['reliability']:.4f}, "
              f"resolution {probability['resolution']:.4f})")
        print(f"    skill         {probability['skill']:>+7.3f}")
        print(f"\n    {probability['verdict']}")

        buckets = result["reliability_buckets"]
        if buckets is not None and not buckets.empty:
            print(f"\n    {'n':>4} {'predicted':>10} {'actual':>8} {'gap':>8}")
            for row in buckets.itertuples():
                print(f"    {row.n:>4} {row.predicted:>9.1%} "
                      f"{row.actual:>7.1%} {row.gap:>+7.1%}")

    fills = result["fills"]
    if fills:
        print(f"\n  FILLS  ({fills['n']} recorded)")
        print(f"    assumed fraction  {fills['assumed_fraction']:>6.2f}")
        if fills["implied_fraction"] is not None:
            print(f"    implied fraction  {fills['implied_fraction']:>6.2f}")
        print(f"    mean slippage     {fills['mean_slippage']:>+6.3f}")
        print(f"\n    {fills['verdict']}")

    if result["recommendations"]:
        print("\n  CONFIG RECOMMENDATIONS (not applied)")
        for rec in result["recommendations"]:
            print(f"    {rec['key']}")
            print(f"      {rec['current']}  ->  {rec['suggested']}"
                  f"   [{rec['confidence']} confidence]")

    gate = result["ready_for_automation"]
    print(f"\n  AUTOMATION READINESS: {gate['passed']}/{gate['total']} gates passed")
    for check in gate["checks"]:
        mark = ("PASS" if check["pass"] is True
                else "----" if check["pass"] is None else "FAIL")
        print(f"    [{mark}] {check['check']:<44} {check['detail']}")


def run_signals(tickers, years: int) -> None:
    """Phase 7 signals: gap profiles and skew, both from data already on disk."""
    from analytics import gaps, skew

    rule("GAP RISK -- how much of the downside is undefendable?")
    reporter = ConsoleReporter([("gaps", "Overnight gap profiles")])
    frame = gaps.universe_profiles(tickers, years=years, reporter=reporter)
    if frame.empty:
        print("\n  No usable 1-minute data. Check data/raw_1m/.")
    else:
        frame.to_csv(output_dir() / "gap_profiles.csv", index=False)
        # `yrs` and `cut` are not decoration. Half this universe only reaches
        # back to 2024 in the 1-minute archive, so a name measured over 2.5
        # years sits in the same ranked table as one measured over 20 -- and
        # a short window that happens to contain a squeeze reads as a
        # permanent property of the stock. `cut` is the corporate-action
        # filter's count, shown so the filter is auditable rather than silent.
        print(f"\n{'ticker':<8} {'on vol':>8} {'id vol':>8} {'on share':>9} "
              f"{'tail':>7} {'P(-5%)':>8} {'worst':>8} {'yrs':>5} {'cut':>4}")
        for row in frame.head(15).itertuples():
            years = getattr(row, "observations", 0) / 252.0
            print(f"{row.ticker:<8} {row.overnight_vol:>7.1%} {row.intraday_vol:>7.1%} "
                  f"{row.overnight_variance_share:>8.0%} {row.tail_ratio:>6.1f}x "
                  f"{row.prob_gap_below_5pct:>7.2%} {row.worst_gap:>7.1%} "
                  f"{years:>5.1f} {getattr(row, 'excluded_days', 0):>4d}")

        short = frame[frame["observations"] < 1000]
        if len(short):
            print(f"\n  {len(short)}/{len(frame)} names have under 4 years of "
                  f"1-minute history. Their figures describe a recent window, not "
                  f"a long-run property -- compare them to each other, not to the "
                  f"20-year names.")
        cut = int(frame.get("excluded_days", pd.Series(dtype=int)).sum())
        if cut:
            print(f"  {cut} day(s) excluded across the universe as corporate "
                  f"actions or archive seams.")
        risky = frame[frame["overnight_variance_share"] >= 0.60]
        print(f"\n  {len(risky)}/{len(frame)} names carry 60%+ of daily variance "
              f"overnight.")
        if len(risky):
            print(f"  On these, rolling and stop discipline act on the smaller half "
                  f"of the risk:\n    {', '.join(risky['ticker'].head(12))}")
        print(f"\n  Written to {output_dir() / 'gap_profiles.csv'}")

    rule("SKEW -- are you being paid for the tail you are selling?")
    reporter = ConsoleReporter([("skew", "Skew and term structure")])
    frame = skew.universe_skew(tickers, reporter=reporter)
    if frame.empty:
        print("\n  No chain snapshots with IV. Run the pipeline during market hours.")
        return
    frame.to_csv(output_dir() / "skew.csv", index=False)
    if "classification" in frame.columns:
        counts = frame["classification"].value_counts().to_dict()
        print("\n  " + "  ".join(f"{k}: {v}" for k, v in counts.items()))

        # How many names the quotes could actually resolve is the first thing
        # worth knowing. On a weekend capture it is usually a small minority,
        # and a table that does not say so invites reading noise as signal.
        measurable = frame[frame["classification"] != "unmeasurable"]
        print(f"  {len(measurable)}/{len(frame)} names have quotes tight enough "
              f"to fix the sign of skew.")
        if "skew_high" in frame.columns and "skew_low" in frame.columns:
            width = (frame["skew_high"] - frame["skew_low"]).dropna()
            if len(width):
                print(f"  Median quote band: {width.median():.0%} of ATM vol "
                      f"(tightest {width.min():.0%}, widest {width.max():.0%}).")

        inverted = measurable[measurable["classification"] == "inverted"]
        if len(inverted):
            print(f"\n  INVERTED skew (blocked at the gate): "
                  f"{', '.join(inverted['ticker'])}")
        unmeasurable = frame[frame["classification"] == "unmeasurable"]
        if len(unmeasurable):
            print(f"\n  Not measurable from these quotes -- warned, not blocked: "
                  f"{', '.join(unmeasurable['ticker'].head(12))}"
                  f"{' ...' if len(unmeasurable) > 12 else ''}")
    if "ts_event_suspected" in frame.columns:
        events = frame[frame["ts_event_suspected"] == True]  # noqa: E712
        if len(events):
            print(f"\n  Single-name backwardation -- a dateable event is expected: "
                  f"{', '.join(events['ticker'].head(12))}")
    print(f"\n  Written to {output_dir() / 'skew.csv'}")


def run_iv_coverage() -> None:
    from analytics.iv_history import MIN_OBSERVATIONS_FOR_RANK, coverage

    rule("IV RANK COVERAGE -- when does the dormant component switch on?")
    table = coverage()
    if table.empty:
        print("\n  No universe loaded.")
        return

    active = int(table["active"].sum())
    print(f"\n  {active}/{len(table)} tickers have the "
          f"{MIN_OBSERVATIONS_FOR_RANK} regular-session captures IV rank needs.")
    if active < len(table):
        median_needed = int(table["needed"].median())
        print(f"  Median shortfall: {median_needed} more captures.")
        print(f"  At one weekday run during market hours, that is roughly "
              f"{median_needed} trading days away.")
    print(f"\n{'ticker':<8} {'obs':>5} {'needed':>7} {'first':>12} {'latest':>12}")
    for row in table.head(15).itertuples():
        print(f"{row.ticker:<8} {row.observations:>5} {row.needed:>7} "
              f"{str(row.first or '-'):>12} {str(row.latest or '-'):>12}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", default=None, help="comma-separated subset")
    ap.add_argument("--years", type=int, default=20, help="history to use")
    ap.add_argument("--regimes", action="store_true")
    ap.add_argument("--walk-forward", action="store_true")
    ap.add_argument("--calibration", action="store_true")
    ap.add_argument("--iv-coverage", action="store_true")
    ap.add_argument("--signals", action="store_true",
                     help="gap risk and skew (Phase 7)")
    args = ap.parse_args()

    selected = any([args.regimes, args.walk_forward, args.calibration,
                     args.iv_coverage, args.signals])
    tickers = ([t.strip().upper() for t in args.tickers.split(",") if t.strip()]
               if args.tickers else load_universe())

    if not tickers and (args.regimes or args.walk_forward or not selected):
        print("No universe. Run the pipeline, or pass --tickers.")
        return 1

    print(f"Validation suite -- {len(tickers)} ticker(s), {args.years}y of history")

    needs_history = args.regimes or args.walk_forward or args.signals or not selected
    data_ready = check_data_ready() if needs_history else True
    if needs_history and not data_ready:
        # Calibration and IV coverage do not read price history, so still run
        # them if they were asked for -- a blocked section should not silently
        # cancel the ones that would have worked.
        if args.calibration:
            run_calibration()
        if args.iv_coverage:
            run_iv_coverage()
        return 1

    if args.regimes or not selected:
        run_regimes(tickers, args.years)
    if args.walk_forward or not selected:
        run_walk_forward(tickers, args.years)
    if args.calibration or not selected:
        run_calibration()
    if args.signals or not selected:
        run_signals(tickers, args.years)
    if args.iv_coverage or not selected:
        run_iv_coverage()

    print(f"\n{'=' * 78}")
    print("  Validation is evidence, not a decision. Nothing above changed "
          "config.yaml.")
    print(f"{'=' * 78}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
