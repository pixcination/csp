r"""
Symbol Lookup (Phase 20B): any ticker's Outlook and candidate trades on
demand, from the modules the pipeline already runs. No new analytics.

    check        Yahoo has prices for it and TastyTrade lists options on it
    register     unknown symbols join the registry tagged `adhoc`: left out of
                 `universe: all`, the nightly data stages, the archive and
                 every auto preset until "Add to universe" (promote_adhoc)
    data         daily bars, earnings (stocks), market metrics, events
    technicals   indicators, trend state, level study (technical_study.run)
    outlook      the dials at every grid horizon, pooled skill with the
                 per-symbol shrink (outlook.for_ticker via build(save=False));
                 the Volatility dial ranked against the saved universe table,
                 which is NOT rewritten
    chains       a targeted capture of this one symbol
    candidates   CSP (physically settled names) and put spreads for the
                 chosen profile and window, the probability engine, and the
                 recommender's strategies

Everything is written as an ordinary run folder (`data/runs/<id>/`) whose
manifest says `kind: lookup`, so Trade Detail opens its rows while
`latest_run()` skips it. Holds the pipeline's run lock, and refuses while a
scheduled job runs or is due within 15 minutes.

    .venv\Scripts\python pipeline\lookup.py PLTR [--profile roth_ira]
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from core.market_calendar import classify, session_block  # noqa: E402
from core.progress import BaseReporter, ConsoleReporter, NullReporter  # noqa: E402

STAGES = [
    ("check", "Check the symbol"),
    ("register", "Register"),
    ("daily", "Daily bars"),
    ("earnings", "Earnings calendar"),
    ("metrics", "Market metrics"),
    ("events", "Events calendar"),
    ("technicals", "Technicals and level study"),
    ("outlook", "Outlook dials"),
    ("chains", "Option chain"),
    ("candidates", "Ranking candidates"),
    ("probabilities", "Probability engine"),
    ("strategies", "Strategy recommender"),
]
#: Yahoo instrumentType -> registry asset class
ASSET_CLASS = {"EQUITY": "stock", "ETF": "etf", "INDEX": "index"}
#: Under two years of bars the level study and the empirical probabilities rest
#: on few samples; the Outlook needs 300 (outlook.build).
SHORT_HISTORY_BARS = 504
OUTLOOK_MIN_BARS = 300
TARGET_SECONDS = 90
OUTLOOK_FILE = "outlook.parquet"


class LookupError_(RuntimeError):
    """The lookup cannot run (unknown symbol, no options, scheduler busy)."""


@dataclass
class Lookup:
    symbol: str
    run_id: str | None = None
    asset_class: str | None = None
    registered: str = ""                  # "adhoc (new)", "adhoc", "universe"
    bars: int = 0
    outlook: pd.DataFrame = field(default_factory=pd.DataFrame)
    skill_source: str | None = None       # "own" | "pooled" | None
    notes: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    seconds: float = 0.0


# --- Steps -------------------------------------------------------------------------------

def check(symbol: str) -> dict:
    """Yahoo prices and TastyTrade options for `symbol`, or LookupError_."""
    import yfinance as yf

    from data_sources import tastytrade_client, universe
    symbol = universe.normalise(symbol)
    if not symbol:
        raise LookupError_("enter a ticker")
    known = universe.get(symbol)
    if known:
        yf_symbol, tt_symbol = known["yf_symbol"], known["tt_symbol"]
    else:
        yf_symbol, tt_symbol, _ = universe.default_mapping(symbol)
    ticker = yf.Ticker(yf_symbol)
    try:
        prices = ticker.history(period="5d")
    except Exception as exc:
        raise LookupError_(f"Yahoo lookup of {yf_symbol} failed: {exc}") from None
    if prices is None or prices.empty:
        raise LookupError_(f"Yahoo has no prices for {yf_symbol}: not a listed symbol, "
                           f"or delisted")
    try:
        kind = (ticker.get_history_metadata() or {}).get("instrumentType")
    except Exception:
        kind = None
    asset_class = (known or {}).get("asset_class") or ASSET_CLASS.get(str(kind).upper())
    if asset_class is None:
        raise LookupError_(f"{symbol} is a {kind or 'unknown'} at Yahoo; only stocks, ETFs "
                           f"and indices are supported")
    try:
        chain = tastytrade_client.fetch_chain(tt_symbol)
    except Exception as exc:
        raise LookupError_(f"TastyTrade option-chain lookup of {tt_symbol} failed: "
                           f"{type(exc).__name__}: {str(exc)[:160]}") from None
    items = (chain or {}).get("data", {}).get("items") or []
    expirations = [e for item in items for e in item.get("expirations") or []]
    if not expirations:
        raise LookupError_(f"TastyTrade lists no options on {tt_symbol}")
    weeklies = any(str(e.get("expiration-type", "")).lower() == "weekly" for e in expirations)
    return {"symbol": symbol, "asset_class": asset_class, "known": known,
            "expirations": len(expirations), "weeklies": weeklies, "yahoo_type": kind}


def register(info: dict) -> str:
    """Registry state after the step: new symbols join tagged adhoc."""
    from data_sources import universe
    known = info["known"]
    if known and bool(known.get("active")):
        return "adhoc" if universe.is_adhoc(known.get("tags")) else "universe"
    if known:
        # A deactivated registry symbol comes back as ad hoc, not into the universe.
        tags = universe.tag_list(known.get("tags"))
        universe.add(info["symbol"], tags=",".join(
            tags + ([] if universe.ADHOC_TAG in tags else [universe.ADHOC_TAG])))
        return "adhoc (re-activated)"
    universe.add(info["symbol"], asset_class=info["asset_class"], tags=universe.ADHOC_TAG,
                 notes="Symbol Lookup", source="lookup", weeklies=info["weeklies"])
    return "adhoc (new)"


def outlook_rows(symbol: str, reporter: BaseReporter) -> tuple[pd.DataFrame, str | None]:
    """The dials at every horizon, the Volatility dial ranked against the
    saved universe table (left unchanged). ("own" | "pooled") skill source."""
    from analytics import outlook
    mine = outlook.build([symbol], reporter=reporter, save=False)
    if mine.empty:
        return mine, None
    table = outlook.load_latest()
    if not table.empty:
        others = table[table["ticker"] != symbol]
        both = outlook.relative_volatility(pd.concat([others, mine], ignore_index=True))
        mine = both[both["ticker"] == symbol].reset_index(drop=True)
    skill = outlook.load_skill()
    own = not skill.empty and (skill["symbol"] == symbol).any()
    return mine, "own" if own else "pooled"


def request_for(symbol: str, account_profile: str, dte_min: int | None = None,
                dte_max: int | None = None, pcs_dte: int | None = None,
                width_pct: float | None = None):
    """Default request settings for one symbol: CSP and put spreads, the
    recommender on, the chosen profile, optional DTE / width edits."""
    from analytics.scan_request import ScanRequest
    return ScanRequest.default(
        strategies=["csp", "pcs"], universe=[symbol], account_profile=account_profile,
        top_n_underlyings="all", recommend=True, dte_min=dte_min, dte_max=dte_max,
        pcs_dte_targets=[int(pcs_dte)] if pcs_dte else None,
        spread_width_pct=[float(width_pct)] if width_pct else None,
        name=f"lookup {symbol}")


# --- The lookup --------------------------------------------------------------------------

def run(symbol: str, account_profile: str = "default", dte_min: int | None = None,
        dte_max: int | None = None, pcs_dte: int | None = None,
        width_pct: float | None = None, reporter: BaseReporter | None = None,
        check_scheduler: bool = True) -> Lookup:
    """Look `symbol` up end to end (module docstring). LookupError_ when it
    cannot run; RunLocked when another run holds the lock."""
    from analytics import technical_study
    from analytics.candidates import evaluate_universe, select_sheet
    from data_sources import chains, events, tasty_metrics, yfinance_sync
    from data_sources import universe as registry
    from pipeline import results as run_results
    from pipeline.run import RunLock, RunManifest, _tickers_with_positions

    reporter = reporter or NullReporter(STAGES)
    if check_scheduler:
        from pipeline import scheduler
        why = scheduler.busy(within_minutes=15)
        if why:
            raise LookupError_(f"Not now: {why}. Symbol Lookup waits for the scheduled jobs "
                               f"(it shares their run lock); try again after it.")
    started = time.monotonic()
    out = Lookup(symbol=registry.normalise(symbol))
    info_state = classify()
    manifest = RunManifest(
        run_id=dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4],
        started_at=dt.datetime.now().isoformat(timespec="seconds"),
        session_block=session_block(), session_state=info_state.state.value, tickers=1,
        kind="lookup", symbol=out.symbol)

    def timed(key: str, fn, *args, **kwargs):
        t0 = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            out.timings[key] = round(time.monotonic() - t0, 1)

    with RunLock():
        with reporter.stage("check", "Check the symbol", total=1):
            info = timed("check", check, out.symbol)
            out.asset_class = info["asset_class"]
            reporter.advance(1, note=f"{info['asset_class']}, {info['expirations']} expirations"
                                     + (", weeklies" if info["weeklies"] else ""))
        with reporter.stage("register", "Register", total=1):
            out.registered = timed("register", register, info)
            reporter.advance(1, note=out.registered)

        symbols = [out.symbol]
        daily = timed("daily", yfinance_sync.sync_daily, symbols, reporter=reporter)
        if daily and daily[0].error:
            out.notes.append(f"daily bars: {daily[0].error}")
        if out.asset_class == "stock":
            timed("earnings", yfinance_sync.sync_earnings, symbols, reporter=reporter)
        metrics = timed("metrics", tasty_metrics.sync, symbols, reporter=reporter)
        if metrics.get("missing"):
            out.notes.append("TastyTrade returned no market metrics (IV rank) for it")
        timed("events", events.build, reporter=reporter)
        out.bars = int(len(yfinance_sync.load_daily(out.symbol, basis="price")))
        if out.bars < SHORT_HISTORY_BARS:
            out.notes.append(
                f"Short history: {out.bars} daily bars (under two years). The level study and "
                f"the empirical probabilities rest on few samples"
                + (f"; the Outlook needs {OUTLOOK_MIN_BARS} and is not shown."
                   if out.bars < OUTLOOK_MIN_BARS else "."))

        tech = timed("technicals", technical_study.run, symbols, reporter=reporter)
        for error in tech.get("errors", [])[:2]:
            out.notes.append(f"technicals: {error}")
        out.outlook, out.skill_source = timed("outlook", outlook_rows, out.symbol, reporter)
        if out.skill_source == "pooled":
            out.notes.append("Outlook skill is the pooled (all-symbol) estimate: this symbol "
                             "has no walk-forward validation of its own, so the dials are shrunk "
                             "by the universe's skill, not its own.")
        if not out.outlook.empty:
            out.outlook.to_parquet(_folder(manifest.run_id) / OUTLOOK_FILE, index=False)

        request = request_for(out.symbol, account_profile, dte_min, dte_max, pcs_dte, width_pct)
        manifest.request = request.to_dict()
        from analytics import underlying_rank
        ranked = timed("rank", underlying_rank.rank, request, symbols=symbols, held=set())
        ivs = {r.symbol: float(r.iv_used) for r in ranked.itertuples()
               if getattr(r, "iv_used", None) is not None and r.iv_used == r.iv_used}
        captured = timed("chains", chains.capture_targets, symbols, request,
                         with_positions=_tickers_with_positions(), ivs=ivs,
                         reporter=reporter)
        if captured and captured[0].error:
            out.notes.append(f"chain capture: {captured[0].error[:160]}")

        full = timed("candidates", evaluate_universe, symbols, reporter=reporter,
                     request=request)
        prob_tables: dict = {}
        if not full.empty:
            from analytics import probabilities
            census = full.attrs.get("census")
            engine = timed("probabilities", probabilities.run_sheet, full, request,
                           reporter=reporter)
            full = engine["sheet"]
            if census:
                full.attrs["census"] = census
            prob_tables = {k: engine[k] for k in ("policies", "metrics", "curves")}
        sheet = select_sheet(full)
        best = sheet[sheet["best_per_ticker"]] if not sheet.empty else sheet
        run_results.write_tables(manifest.run_id,
                                 run_results.annotate_sheet(full, best, pd.DataFrame()),
                                 [], underlyings=ranked, **prob_tables)
        manifest.stages["analyse"] = {
            "candidates_considered": int(len(full)),
            "accepted": int(full["accepted"].sum()) if "accepted" in full else 0,
            "census": full.attrs.get("census") if not full.empty else None}

        from analytics import recommender
        strat = timed("strategies", recommender.run, symbols, request=request,
                      recommend=True, reporter=reporter)
        run_results.write_strategies(manifest.run_id, strat["sheet"], strat["conditions"])
        manifest.stages["strategies"] = {"positions": int(len(strat["sheet"]))}

    out.seconds = round(time.monotonic() - started, 1)
    out.run_id = manifest.run_id
    manifest.stages["lookup"] = {"timings": out.timings, "seconds": out.seconds,
                                 "registered": out.registered, "bars": out.bars,
                                 "skill_source": out.skill_source, "notes": out.notes}
    manifest.warnings = list(out.notes)
    manifest.finished_at = dt.datetime.now().isoformat(timespec="seconds")
    manifest.elapsed_seconds = out.seconds
    manifest.write()
    return out


def _folder(run_id: str) -> Path:
    from core.paths import runs_dir
    path = runs_dir() / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_outlook(run_id: str) -> pd.DataFrame:
    from core.paths import runs_dir
    path = runs_dir() / run_id / OUTLOOK_FILE
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def recent(limit: int = 20) -> list[dict]:
    """Recent lookups, newest first: {run_id, symbol, finished_at, seconds}."""
    from pipeline import results as run_results
    out = []
    for run_id in run_results.list_runs():
        res = run_results.load_run(run_id)
        if res is None or getattr(res.manifest, "kind", "run") != "lookup":
            continue
        out.append({"run_id": run_id, "symbol": res.manifest.symbol,
                    "finished_at": res.finished_at,
                    "seconds": res.manifest.elapsed_seconds})
        if len(out) >= limit:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Symbol Lookup (Phase 20B).")
    ap.add_argument("symbol")
    ap.add_argument("--profile", default="default")
    ap.add_argument("--dte-min", type=int)
    ap.add_argument("--dte-max", type=int)
    ap.add_argument("--pcs-dte", type=int)
    ap.add_argument("--width-pct", type=float)
    ap.add_argument("--ignore-scheduler", action="store_true",
                    help="skip the scheduler check (the run lock still applies)")
    args = ap.parse_args()
    try:
        out = run(args.symbol, args.profile, args.dte_min, args.dte_max, args.pcs_dte,
                  args.width_pct, reporter=ConsoleReporter(STAGES),
                  check_scheduler=not args.ignore_scheduler)
    except LookupError_ as exc:
        print(f"refused: {exc}")
        return 1
    print(f"{out.symbol}: run {out.run_id} in {out.seconds:.0f}s "
          f"(target {TARGET_SECONDS}s) -- {out.registered}; timings {out.timings}")
    for note in out.notes:
        print(f"  note: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
