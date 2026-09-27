"""
The activation button.

    python pipeline/run.py                 # full run
    python pipeline/run.py --quick         # skip refresh, analyse what is on disk
    python pipeline/run.py --tickers SPY,F # a subset
    python pipeline/run.py --force-chains  # ignore the staleness window
    python pipeline/run.py --data-only     # nightly: bars, metrics, events; no chains

One orchestrator owns the whole run. Each stage decides for itself whether it
has anything to do, so the cost of pressing the button scales with how stale
things actually are: a Saturday click after a Friday run does almost nothing
and finishes in seconds, while a Tuesday-morning click re-pulls chains and
nothing else.

DESIGN RULES
------------
* **Staleness-aware, not unconditional.** "Make everything current" is the
  contract, not "do all the work."
* **A run lock.** Two clicks cannot overlap; the second is told who holds the
  lock and since when, rather than silently interleaving parquet writes.
* **Stage isolation.** A failure in one stage degrades the run and is
  reported; it does not abort the others. A dead earnings feed should not
  cost you the chain snapshot you were actually waiting for.
* **A manifest.** Every run writes `data/runs/<id>/manifest.json` recording
  what ran, what was skipped, how long it took and what the session state
  was, so a recommendation can always be traced back to the data behind it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import env  # noqa: E402
from core.market_calendar import classify, session_block  # noqa: E402
from core.paths import load_config, load_universe, runs_dir  # noqa: E402
from core.progress import BaseReporter, ConsoleReporter  # noqa: E402

STAGES = [
    ("preflight", "Preflight and session"),
    ("universe", "Universe registry"),
    ("reference", "Reference data"),
    ("daily", "Daily bars"),
    ("earnings", "Earnings calendar"),
    ("metrics", "Market metrics"),
    ("events", "Events calendar"),
    ("stage1", "Stage 1 screen"),
    ("chains", "Option chains"),
    ("analyse", "Analysis"),
    ("candidates", "Ranking candidates"),
    ("wheel", "Wheel management"),
    ("portfolio", "Portfolio construction"),
]


class RunLocked(RuntimeError):
    pass


def _json_safe(value):
    """Recursively replace NaN/Infinity with None so the manifest is real JSON."""
    import math
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


@dataclass
class RunManifest:
    run_id: str
    started_at: str
    session_block: str
    session_state: str
    tickers: int
    finished_at: str | None = None
    elapsed_seconds: float | None = None
    stages: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    banner: str = ""

    def write(self) -> Path:
        folder = runs_dir() / self.run_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "manifest.json"
        # json.dumps emits bare NaN/Infinity, which is not valid JSON and is
        # rejected by strict parsers -- including json.loads(..., parse_constant)
        # and every browser. Missing values become null.
        path.write_text(json.dumps(_json_safe(asdict(self)), indent=2, default=str),
                        encoding="utf-8")
        return path


class RunLock:
    """A lock file carrying the owning PID, so a crashed run can be reclaimed."""

    def __init__(self):
        self.path = runs_dir() / "run.lock"

    def __enter__(self):
        if self.path.exists():
            try:
                info = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                info = {}
            pid, started = info.get("pid"), info.get("started_at", "unknown")
            if pid and _pid_alive(pid):
                raise RunLocked(
                    f"Another run is in progress (pid {pid}, started {started}). "
                    f"Wait for it, or delete {self.path} if you are certain it died.")
            self.path.unlink(missing_ok=True)
        self.path.write_text(json.dumps({
            "pid": os.getpid(),
            "started_at": dt.datetime.now().isoformat(timespec="seconds"),
        }), encoding="utf-8")
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)
        return False


def _pid_alive(pid: int) -> bool:
    """Is `pid` running? Must never affect the process it asks about.

    `os.kill(pid, 0)` is the POSIX idiom, but on Windows os.kill calls
    TerminateProcess(pid, exit_code=sig) for any signal other than the two
    console events -- so "checking" the lock holder killed it, with exit
    code 0 so it looked like a clean finish. Found in Phase 8 when a second
    process checking the lock silently ended a pipeline run mid-capture.
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            # Access denied means it exists; anything else means it does not.
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# --- Stages ----------------------------------------------------------------

def _stage_preflight(reporter: BaseReporter, manifest: RunManifest) -> dict:
    from data_sources import tastytrade_client as tt
    with reporter.stage("preflight", "Preflight and session", total=3):
        healthy, report = env.doctor()
        reporter.advance(1, note="credentials " + ("ok" if healthy else "MISSING"))
        if not healthy:
            manifest.warnings.append("required credentials missing")
            reporter.log(report.splitlines()[-1])

        for stray in env.stray_env_files():
            manifest.warnings.append(f"duplicate .env at {stray}")
            reporter.log(f"WARNING: duplicate .env at {stray} -- see consolidate.py")

        ok, message = tt.check_connection()
        reporter.advance(1, note="tastytrade " + ("ok" if ok else "FAILED"))
        if not ok:
            manifest.warnings.append(f"tastytrade: {message}")
            reporter.log(message)

        info = classify()
        severity, banner = info.banner()
        manifest.banner = banner
        reporter.advance(1, note=f"session {info.state.value}")
        reporter.log(banner)
        return {"credentials_ok": healthy, "tastytrade_ok": ok,
                "session_state": info.state.value, "severity": severity}


def _stage_reference(reporter: BaseReporter, manifest: RunManifest) -> dict:
    from data_sources import reference
    results = reference.refresh_all(reporter=reporter)
    for res in results:
        if res.error:
            manifest.warnings.append(f"reference/{res.source}: {res.error}")
    return {"sources": [asdict(r) for r in results]}


def _stage_daily(reporter: BaseReporter, manifest: RunManifest,
                  tickers: list[str]) -> dict:
    from data_sources import yfinance_sync
    results = yfinance_sync.sync_daily(tickers, reporter=reporter)
    failed = [r.ticker for r in results if r.error]
    if failed:
        manifest.warnings.append(f"daily bars failed: {', '.join(failed[:8])}")
    try:
        dividends = len(yfinance_sync.write_dividends_from_raw())
    except Exception as exc:
        dividends = 0
        manifest.warnings.append(f"dividend ex-dates: {exc}")
    return {"synced": len(results) - len(failed), "failed": failed,
            "rows_added": sum(r.rows_added for r in results),
            "full_repulls": {r.ticker: r.full_repull for r in results
                             if r.full_repull and r.full_repull != "initial"},
            "dividend_rows": dividends}


def _stage_universe(reporter: BaseReporter, manifest: RunManifest) -> dict:
    from data_sources import universe
    with reporter.stage("universe", "Universe registry", total=1):
        rows = universe.ensure()
        frame = universe.load(active_only=True)
        counts = frame["asset_class"].value_counts().to_dict()
        reporter.advance(1, note=f"{len(frame)} active of {rows}: " +
                         ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    return {"rows": rows, "active": len(frame), "by_class": counts}


def _stocks(tickers: list[str]) -> list[str]:
    """Only stocks report earnings; asking yfinance for SPY's is noise."""
    try:
        from data_sources import universe
        frame = universe.load()
        stocks = set(frame.loc[frame["asset_class"] == "stock", "symbol"])
        return [t for t in tickers if t in stocks or t not in set(frame["symbol"])]
    except Exception:
        return tickers


def _stage_earnings(reporter: BaseReporter, manifest: RunManifest,
                     tickers: list[str], max_age_hours: int = 20) -> dict:
    from core.paths import reference_dir
    from data_sources import yfinance_sync
    tickers = _stocks(tickers)
    path = reference_dir() / yfinance_sync.EARNINGS_FILE
    if path.exists():
        age = dt.datetime.now() - dt.datetime.fromtimestamp(path.stat().st_mtime)
        known = set(yfinance_sync.load_earnings().get("ticker", pd.Series(dtype=str)))
        # Fresh AND complete: a symbol added since the last pull must not wait a day.
        if age < dt.timedelta(hours=max_age_hours) and set(tickers) <= known:
            with reporter.stage("earnings", "Earnings calendar"):
                reporter.skip(f"{age.total_seconds() / 3600:.0f}h old")
            return {"skipped": True}
    frame = yfinance_sync.sync_earnings(tickers, reporter=reporter)
    covered = frame["ticker"].nunique() if not frame.empty else 0
    if covered < len(tickers):
        missing = sorted(set(tickers) - set(frame["ticker"])) if not frame.empty else tickers
        manifest.warnings.append(
            f"yfinance earnings dates missing for {len(missing)}/{len(tickers)} stock(s) "
            f"({', '.join(missing[:6])}); TastyTrade dates still apply -- the events "
            f"stage reports what remains unknown")
    return {"tickers_covered": covered, "rows": len(frame)}


def _stage_metrics(reporter: BaseReporter, manifest: RunManifest,
                   tickers: list[str]) -> dict:
    from data_sources import tasty_metrics
    result = tasty_metrics.sync(tickers, reporter=reporter)
    if result["errors"]:
        manifest.warnings.append(f"market metrics: {result['errors'][0]}")
    if result["missing"]:
        manifest.warnings.append(
            f"market metrics missing for: {', '.join(result['missing'][:8])}")
    return result


def _stage_events(reporter: BaseReporter, manifest: RunManifest,
                  tickers: list[str]) -> dict:
    from data_sources import events
    counts = events.build(reporter=reporter)
    health = events.earnings_health()
    if not health["healthy"]:
        manifest.warnings.append(f"earnings calendar: {health['note']}")
    disagree = events.load()
    disagree = disagree[(disagree["type"] == "earnings") & disagree["sources_disagree"]
                        & (disagree["date"] >= dt.date.today())]
    for row in disagree.itertuples():
        reporter.log(f"  earnings date disagreement {row.symbol}: {row.note}")
    return {"counts": counts, "earnings_coverage": health["coverage"],
            "earnings_missing": health["missing"],
            "disagreements": disagree["symbol"].tolist()}


def _stage_stage1(reporter: BaseReporter, manifest: RunManifest,
                  tickers: list[str]) -> dict:
    from analytics import universe_screen
    with reporter.stage("stage1", "Stage 1 screen", total=1):
        frame = universe_screen.screen(tickers)
        counts = frame["tier"].value_counts().to_dict()
        reporter.advance(1, note=", ".join(f"{v} {k}" for k, v in counts.items()))
    return {"tiers": counts}


def _stage_chains(reporter: BaseReporter, manifest: RunManifest,
                   tickers: list[str], force: bool) -> dict:
    from data_sources import chains
    with_positions = _tickers_with_positions()
    results = chains.capture_universe(tickers, with_positions=with_positions,
                                       force=force, reporter=reporter)
    failed = [r.ticker for r in results if r.error]
    skipped = [r.ticker for r in results if r.skipped]
    if failed:
        manifest.warnings.append(f"chain capture failed: {', '.join(failed[:8])}")
    return {"captured": len(results) - len(failed) - len(skipped),
            "skipped": len(skipped), "failed": failed,
            "block": session_block()}


def _stage_analyse(reporter: BaseReporter, manifest: RunManifest,
                    tickers: list[str]) -> dict:
    """Score entries, evaluate open positions, and apply the regime gate.

    Deliberately thin for now -- the full candidate ranking arrives with the
    EV-based scoring rewrite. What runs today is everything already built and
    tested: the regime gate, capacity, and the management engine over any
    open position.
    """
    from analytics import regime, sizing
    results: dict = {}
    with reporter.stage("analyse", "Analysis", total=3):
        reading = regime.current()
        results["regime"] = reading.to_dict()
        reporter.advance(1, note=reading.headline)
        reporter.log(reading.detail)

        capacity = sizing.capacity_report()
        results["capacity"] = capacity
        reporter.advance(1, note=f"${capacity['deployable_cash']:,.0f} deployable, "
                                 f"max strike ${capacity['max_tradable_strike']:,.0f}")

        decisions = _evaluate_open_positions(reporter)
        results["open_positions"] = decisions
        reporter.advance(1, note=f"{len(decisions)} open position(s)")

    # Candidate ranking is its own stage: it is the expensive part of the
    # analysis and the part you actually wait for.
    from analytics.candidates import evaluate_universe, select_sheet
    full_sheet = evaluate_universe(tickers, reporter=reporter)
    sheet = select_sheet(full_sheet)
    proposed = pd.DataFrame()
    if not sheet.empty:
        # Portfolio construction runs BEFORE the proposal list is finalised.
        # Ranking by EV and then checking concentration afterwards would
        # propose three names and quietly drop two -- the limits have to
        # participate in the selection, not audit it.
        from analytics import portfolio

        held = sorted(_tickers_with_positions())
        with reporter.stage("portfolio", "Portfolio construction", total=1):
            try:
                selection = portfolio.select(sheet, held=held)
                proposed = pd.DataFrame(selection.accepted)
                results["portfolio_rejected"] = selection.rejected
                results["clusters"] = selection.clusters
                for note in selection.notes:
                    reporter.log(note)
                reporter.advance(1, note=f"{len(selection.accepted)} accepted, "
                                          f"{len(selection.rejected)} held back")
                for record in selection.rejected[:5]:
                    reasons = record.get("rejection_reasons") or []
                    if reasons:
                        reporter.log(f"  held back {record.get('ticker')}: {reasons[0][:90]}")
            except Exception as exc:
                reporter.advance(1, note="concentration check failed")
                manifest.warnings.append(f"portfolio: {exc}")
                proposed = sheet[sheet["proposed"]] if "proposed" in sheet else sheet

        results["candidates"] = (proposed.to_dict("records")
                                 if not proposed.empty else [])
        results["candidates_considered"] = int(len(sheet))
        if getattr(sheet, "attrs", {}).get("census"):
            results["census"] = sheet.attrs["census"]
        reporter.log(f"{len(proposed)} trade(s) proposed from "
                     f"{len(sheet)} qualifying candidate(s)")
        for row in proposed.itertuples():
            reporter.log(f"  {row.ticker} {row.expiration} ${row.strike:g}p "
                         f"x{row.contracts} @ ~${row.modelled_fill:.2f} -- "
                         f"EV {row.ev_annualised:.1%} annualised")

        # Stress the resulting book, existing positions included.
        try:
            from analytics import paper, portfolio
            book = [{"ticker": r["ticker"], "spot": r.get("spot"),
                     "strike": r.get("strike"), "collateral": r.get("collateral")}
                    for r in results["candidates"]]
            open_positions = paper.list_positions(status="open")
            for _, row in open_positions.iterrows():
                book.append({"ticker": row["ticker"], "spot": None,
                             "strike": float(row["strike"]),
                             "collateral": float(row.get("collateral") or 0.0)})
            stress = portfolio.simultaneous_assignment(
                [b for b in book if b.get("spot")])
            if stress:
                results["stress"] = stress.to_dict()
                reporter.log(stress.verdict)
        except Exception as exc:
            manifest.warnings.append(f"stress test: {exc}")
    else:
        results["candidates"] = []
        census = getattr(sheet, "attrs", {}).get("census")
        if census:
            results["census"] = census
            manifest.warnings.append(f"no candidates: {census['headline']}")
        else:
            reporter.log("no candidates passed the entry gates")

    # Persist the tables beside the manifest so pages survive a restart.
    try:
        from pipeline import results as run_results
        written = run_results.write_tables(
            manifest.run_id,
            run_results.annotate_sheet(full_sheet, sheet, proposed),
            results.get("open_positions", []))
        results["persisted"] = written
    except Exception as exc:
        manifest.warnings.append(f"could not persist run tables: {exc}")

    results.update(_stage_wheel(reporter, manifest))
    return results


def _stage_wheel(reporter: BaseReporter, manifest: RunManifest) -> dict:
    """The second half of the wheel: calls against assigned shares, and rolls
    for any put that has gone against you.

    Runs after candidates because managing what you already hold takes
    precedence over opening something new -- and because a roll decision can
    consume the capital a new candidate was going to use.
    """
    from analytics import covered_call, paper, roll_engine

    out: dict = {"covered_calls": [], "rolls": []}
    with reporter.stage("wheel", "Wheel management", total=2):
        try:
            lots = paper.list_share_lots(open_only=True)
            if lots.empty:
                reporter.advance(1, note="no assigned shares")
            else:
                sheet = covered_call.sheet_for_all_lots()
                out["covered_calls"] = (sheet.to_dict("records")
                                        if not sheet.empty else [])
                writable = sum(1 for r in out["covered_calls"] if r.get("accepted"))
                reporter.advance(1, note=f"{writable}/{len(lots)} lot(s) writable")
                for record in out["covered_calls"]:
                    if not record.get("accepted"):
                        reporter.log(f"  {record.get('ticker')}: "
                                     f"{record.get('rationale', 'no call above basis')}")
        except Exception as exc:
            reporter.advance(1, note="covered calls failed")
            manifest.warnings.append(f"covered calls: {exc}")

        try:
            positions = paper.list_positions(status="open")
            at_risk = []
            if not positions.empty:
                from data_sources import chains
                for _, row in positions.iterrows():
                    ticker = str(row["ticker"]).upper()
                    _, under = chains.load_chain(ticker)
                    spot = chains.spot_from_underlying(under)
                    # Only bother ranking rolls for positions actually in trouble;
                    # a put 8% out of the money does not need a roll analysis.
                    if spot and spot < float(row["strike"]) * 1.02:
                        at_risk.append((row, spot))

            for row, spot in at_risk:
                decision = roll_engine.best_roll_or_accept(
                    str(row["ticker"]).upper(), float(row["strike"]),
                    pd.Timestamp(row["expiration"]).date(), int(row["contracts"]))
                decision["position_id"] = int(row["id"])
                decision["spot"] = spot
                out["rolls"].append(decision)
                reporter.log(f"  {row['ticker']}: {decision['action']} -- "
                             f"{decision['message'][:110]}")
            reporter.advance(1, note=f"{len(at_risk)} position(s) needing defence")
        except Exception as exc:
            reporter.advance(1, note="roll analysis failed")
            manifest.warnings.append(f"roll engine: {exc}")
    return out


def _tickers_with_positions() -> set[str]:
    """Tickers with an open paper-book position (chains get a wider window)."""
    try:
        from analytics import paper
        frame = paper.list_positions(status="open")
        if frame.empty:
            return set()
        return set(frame["ticker"].str.upper())
    except Exception:
        return set()


def _evaluate_open_positions(reporter: BaseReporter) -> list[dict]:
    """Run the management engine over every open short put in the paper book.

    Read the retired Trade Log's table until Phase 8, so positions accepted
    through the Decisions page were never evaluated here.
    """
    try:
        from analytics import paper
        open_rows = paper.list_positions(status="open")
    except Exception:
        return []
    if open_rows.empty:
        return []
    open_rows = open_rows[open_rows["strategy"].fillna("csp").isin(["csp", "put"])]

    from analytics.exit_rules import OpenPut, evaluate_short_put
    from core.market_calendar import trading_days_between
    from data_sources import chains
    from data_sources.yfinance_sync import load_daily

    out = []
    today = dt.date.today()
    for _, row in open_rows.iterrows():
        ticker = str(row["ticker"]).upper()
        chain, under = chains.load_chain(ticker)
        spot = chains.spot_from_underlying(under)
        if spot is None:
            continue
        mark = _mark_for(chain, float(row["strike"]), row["expiration"])
        left = trading_days_between(today, pd.Timestamp(row["expiration"]).date())
        position = OpenPut(
            ticker=ticker, strike=float(row["strike"]),
            contracts=int(row["contracts"]),
            entry_credit=float(row["actual_fill"]
                               if pd.notna(row["actual_fill"]) else row["modelled_fill"]),
            spot=spot, current_mark=mark if mark is not None else 0.0,
            trading_days_left=max(left, 0))
        daily = load_daily(ticker, basis="price")
        decision = evaluate_short_put(position, daily)
        out.append({"position_id": int(row["id"]), **decision.to_dict()})
        if decision.urgency != "routine":
            reporter.log(f"{decision.urgency.upper()}: {decision.headline}")
    return out


def _mark_for(chain, strike: float, expiration) -> float | None:
    if chain is None or chain.empty:
        return None
    import pandas as pd
    frame = chain.copy()
    frame["expiration"] = pd.to_datetime(frame["expiration"])
    target = pd.Timestamp(expiration)
    match = frame[(frame["expiration"] == target)
                   & (frame["strike_price"].astype(float) == float(strike))]
    if match.empty:
        return None
    for column in ("put_mark", "put_bid"):
        if column in match.columns:
            value = match[column].iloc[0]
            if pd.notna(value):
                return float(value)
    return None


import pandas as pd  # noqa: E402  (used by helpers above)


# --- Entry point -----------------------------------------------------------

def run(tickers: list[str] | None = None, quick: bool = False,
        force_chains: bool = False, data_only: bool = False,
        reporter: BaseReporter | None = None) -> RunManifest:
    """`data_only`: refresh data (bars, earnings, metrics, events, Stage 1)
    and stop -- the nightly job, so the interactive run only pulls chains."""
    # Data stages cover every active registry symbol, indices included;
    # chains and the CSP analysis cover what a cash-secured put can trade.
    data_universe = tickers or load_universe(scope="all")
    tradable = set(load_universe(scope="csp"))
    registered = set(load_universe(scope="all"))
    # --tickers may name symbols not yet in the registry; those are treated
    # as tradable (the registry default is a physically settled stock).
    universe = [t for t in data_universe if t in tradable or t not in registered]
    if not data_universe:
        raise RuntimeError(
            "No universe. The registry and output/final_universe.txt are both "
            "empty -- add symbols on the Universe page, or pass --tickers.")

    info = classify()
    manifest = RunManifest(
        run_id=dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4],
        started_at=dt.datetime.now().isoformat(timespec="seconds"),
        session_block=session_block(), session_state=info.state.value,
        tickers=len(data_universe))

    reporter = reporter or ConsoleReporter(STAGES)
    started = dt.datetime.now()

    def guarded(key: str, fn, *args):
        try:
            manifest.stages[key] = fn(*args) or {}
        except Exception as exc:
            manifest.stages[key] = {"error": f"{type(exc).__name__}: {exc}"}
            manifest.warnings.append(f"{key} stage failed: {exc}")
            reporter.log(f"stage '{key}' failed: {type(exc).__name__}: {exc}")

    with RunLock():
        guarded("preflight", _stage_preflight, reporter, manifest)
        guarded("universe", _stage_universe, reporter, manifest)
        if not quick:
            guarded("reference", _stage_reference, reporter, manifest)
            guarded("daily", _stage_daily, reporter, manifest, data_universe)
            guarded("earnings", _stage_earnings, reporter, manifest, data_universe)
            guarded("metrics", _stage_metrics, reporter, manifest, data_universe)
            guarded("events", _stage_events, reporter, manifest, data_universe)
            guarded("stage1", _stage_stage1, reporter, manifest, data_universe)
            if not data_only:
                guarded("chains", _stage_chains, reporter, manifest, universe,
                        force_chains)
        if not data_only:
            guarded("analyse", _stage_analyse, reporter, manifest, universe)

    manifest.finished_at = dt.datetime.now().isoformat(timespec="seconds")
    manifest.elapsed_seconds = (dt.datetime.now() - started).total_seconds()
    path = manifest.write()
    reporter.log(f"manifest: {path}")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description="Refresh everything and analyse.")
    ap.add_argument("--tickers", default=None, help="comma-separated subset")
    ap.add_argument("--quick", action="store_true",
                     help="skip all refresh stages; analyse what is on disk")
    ap.add_argument("--force-chains", action="store_true",
                     help="re-pull chains regardless of the staleness window")
    ap.add_argument("--data-only", action="store_true",
                    help="refresh data only (nightly job): no chains, no analysis")
    args = ap.parse_args()

    tickers = ([t.strip().upper() for t in args.tickers.split(",") if t.strip()]
               if args.tickers else None)
    reporter = ConsoleReporter(STAGES)
    try:
        manifest = run(tickers, quick=args.quick, force_chains=args.force_chains,
                       data_only=args.data_only, reporter=reporter)
    except RunLocked as exc:
        print(f"\n{exc}")
        return 2
    print()
    print(reporter.report())
    if manifest.warnings:
        print(f"\n{len(manifest.warnings)} warning(s):")
        for item in manifest.warnings:
            print(f"  - {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
