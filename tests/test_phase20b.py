"""Phase 20B -- Symbol Lookup: ad-hoc registration kept out of the universe
and the auto presets, the scheduler guard, lookup runs kept out of
`latest_run`, sample `lookup` kept out of accuracy statistics, the Outlook
ranked without rewriting the universe table; the weekly health check; the
macOS preparation (detached worker per platform, the LaunchAgent)."""
from __future__ import annotations

import datetime as dt
import json
import plistlib
import subprocess
import sys

import pandas as pd
import pytest

from analytics import outlook, paper, tracking
from analytics.scan_request import ScanRequest, resolve_universe
from core import user_settings as us
from core.market_calendar import ET
from data_sources import universe
from pipeline import lookup, scheduler

CSP = {"ticker": "F", "strike": 11.0, "expiration": "2026-08-28", "modelled_fill": 0.18,
       "contracts": 2, "bid": 0.17, "ask": 0.20, "mid": 0.185, "spot": 12.0,
       "accepted": True, "trade_id": "csp|F|2026-08-28|11", "prob_otm": 0.8}


@pytest.fixture
def registry(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "final_universe.txt").write_text("AAPL\nSPY\n", encoding="utf-8")
    monkeypatch.setattr(universe, "db_universe", lambda: tmp_path / "universe.duckdb")
    monkeypatch.setattr(universe, "config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(universe, "output_dir", lambda: tmp_path / "output")
    monkeypatch.setattr(universe, "load_universe_file", lambda: ["AAPL", "SPY"])
    universe.ensure()
    universe.add("PLTR", tags="adhoc", source="lookup")
    return tmp_path


@pytest.fixture
def runs(tmp_path, monkeypatch):
    """An isolated data/runs with one normal and one lookup run."""
    import core.paths
    from pipeline import results
    root = tmp_path / "runs"
    for run_id, kind in (("20260929-100000-aaaa", "run"), ("20260929-200000-bbbb", "lookup")):
        (root / run_id).mkdir(parents=True)
        (root / run_id / "manifest.json").write_text(json.dumps({
            "run_id": run_id, "started_at": "2026-09-29T10:00:00", "session_block": "x",
            "session_state": "rth", "tickers": 1, "finished_at": "2026-09-29T10:01:00",
            "stages": {"analyse": {}}, "kind": kind, "symbol": "PLTR",
            "request": {"account_profile": "default"}}), encoding="utf-8")
    monkeypatch.setattr(core.paths, "runs_dir", lambda: root)
    monkeypatch.setattr(results, "runs_dir", lambda: root)
    return root


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    db = tmp_path / "trade_log.duckdb"
    monkeypatch.setattr(paper, "db_trade_log", lambda: db)
    monkeypatch.setattr(tracking, "market_context", lambda *a, **k: {
        "iv_rank": 0.3, "iv_pct": 0.4, "trend": "range", "rsi": 50.0,
        "days_to_earnings": None, "vix_ratio": 0.8})
    paper.ensure_schema()
    return db


# --- Ad-hoc symbols ------------------------------------------------------------------------

def test_adhoc_symbols_stay_out_of_the_universe_until_promoted(registry):
    assert universe.is_adhoc("adhoc") and universe.is_adhoc("x, ADHOC") \
        and not universe.is_adhoc("adhocish") and not universe.is_adhoc(None)
    assert "PLTR" not in universe.symbols("all") and "PLTR" not in universe.symbols("csp")
    assert universe.adhoc_symbols() == {"PLTR"}
    all_ = ScanRequest.default(universe="all", strategies=["csp", "pcs"])
    assert "PLTR" not in resolve_universe(all_) and "AAPL" in resolve_universe(all_)
    assert "PLTR" in resolve_universe(ScanRequest.default(universe=["PLTR"]))
    assert resolve_universe(ScanRequest.default(universe="tag:adhoc")) == ["PLTR"]
    universe.update("PLTR", tags="adhoc,watch")
    universe.promote_adhoc("PLTR")
    assert universe.get("PLTR")["tags"] == "watch"
    assert "PLTR" in universe.symbols("all") and "PLTR" in resolve_universe(all_)


def test_auto_presets_drop_adhoc_symbols(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(us, "path", lambda: tmp_path / "user_settings.yaml")
    us.save_account_profile("roth_ira", {"net_liquidating_value": 36000.0, "placeholder": False})
    both = ScanRequest.default(account_profile="roth_ira", universe=["SPY", "PLTR"]).to_dict()
    only = ScanRequest.default(account_profile="roth_ira", universe=["PLTR"]).to_dict()
    us.save_scan_preset("both", both)
    us.save_scan_preset("only", only)
    assert scheduler.check_auto("both").universe == ["SPY"]
    with pytest.raises(scheduler.Refused, match="ad-hoc"):
        scheduler.check_auto("only")


def test_register_new_known_and_deactivated(registry):
    info = {"symbol": "HOOD", "asset_class": "stock", "known": None, "weeklies": True}
    assert lookup.register(info) == "adhoc (new)"
    assert universe.is_adhoc(universe.get("HOOD")["tags"])
    assert lookup.register({**info, "symbol": "AAPL", "known": universe.get("AAPL")}) == "universe"
    universe.update("SPY", active=False, tags="core")
    assert lookup.register({**info, "symbol": "SPY",
                            "known": universe.get("SPY")}) == "adhoc (re-activated)"
    spy = universe.get("SPY")
    assert bool(spy["active"]) and universe.tag_list(spy["tags"]) == ["core", "adhoc"]
    assert "SPY" not in universe.symbols("all")


def test_check_refuses_unknown_symbols_and_names_without_options(registry, monkeypatch):
    import yfinance

    from data_sources import tastytrade_client

    class Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, period):
            return pd.DataFrame() if self.symbol == "NOPE" else pd.DataFrame({"Close": [1.0]})

        def get_history_metadata(self):
            return {"instrumentType": "EQUITY"}
    monkeypatch.setattr(yfinance, "Ticker", Ticker)
    chains = {"HOOD": [{"expiration-type": "Weekly"}, {"expiration-type": "Regular"}]}
    monkeypatch.setattr(tastytrade_client, "fetch_chain", lambda s: {
        "data": {"items": [{"expirations": chains[s]}] if s in chains else []}})
    with pytest.raises(lookup.LookupError_, match="no prices"):
        lookup.check("NOPE")
    with pytest.raises(lookup.LookupError_, match="no options"):
        lookup.check("ABCD")
    info = lookup.check("hood")
    assert (info["symbol"], info["asset_class"], info["expirations"], info["weeklies"]) == \
        ("HOOD", "stock", 2, True)


# --- The scheduler guard -------------------------------------------------------------------

CFG = {"enabled": True, "grace_minutes": 30,
       "mark": {"start": "09:45", "end": "15:45", "every_minutes": 60},
       "scan_and_log": {"time": "10:45"}, "archive": {"time": "15:30"},
       "nightly": {"time": "18:30"}, "auto_presets": {}}


def _at(hhmm, day=dt.date(2026, 9, 30)):
    h, m = hhmm.split(":")
    return dt.datetime.combine(day, dt.time(int(h), int(m)), ET)


def test_busy_when_a_job_runs_or_is_due_within_15_minutes(monkeypatch):
    done = [{"key": "2026-09-30|mark|-|09:45"}, {"key": "2026-09-30|mark|-|10:45"}]
    monkeypatch.setattr(scheduler, "settings", lambda: CFG)
    monkeypatch.setattr(scheduler, "history", lambda since=None, limit=None: done)
    monkeypatch.setattr(scheduler, "worker_pid", lambda: None)
    assert scheduler.busy(_at("11:00")) is None                      # next mark 11:45
    assert "11:45 mark" in scheduler.busy(_at("11:31"))              # due within 15
    assert "due now" in scheduler.busy(_at("11:50"))                 # pending, inside grace
    monkeypatch.setattr(scheduler, "history", lambda since=None, limit=None: [])
    assert "10:45 mark is due now" in scheduler.busy(_at("11:00"))   # not yet run
    monkeypatch.setattr(scheduler, "history", lambda since=None, limit=None: done + [
        {"key": "2026-09-30|mark|-|11:45"}])
    assert scheduler.busy(_at("11:50")) is None                      # already recorded
    monkeypatch.setattr(scheduler, "worker_pid", lambda: 123)
    monkeypatch.setattr(scheduler, "_read_json",
                        lambda name: {"state": "running 10:45 scan_and_log (x)"})
    assert "running 10:45" in scheduler.busy(_at("11:00"))


def test_lookup_refuses_while_the_scheduler_is_busy(monkeypatch):
    monkeypatch.setattr(scheduler, "busy", lambda within_minutes=15: "11:45 mark is due at 11:45")
    with pytest.raises(lookup.LookupError_, match="Not now: 11:45 mark"):
        lookup.run("PLTR")


# --- Lookup runs and sample `lookup` -------------------------------------------------------

def test_latest_run_skips_lookups(runs):
    from pipeline import results
    assert results.latest_run(with_analysis=False).run_id == "20260929-100000-aaaa"
    assert [r["run_id"] for r in lookup.recent()] == ["20260929-200000-bbbb"]
    assert paper.run_kind("20260929-200000-bbbb") == "lookup"
    assert paper.run_kind("20260929-100000-aaaa") == "run" and paper.run_kind(None) is None


def test_lookup_trades_are_sample_lookup_and_left_out_of_accuracy(runs, ledger):
    tracking.log([CSP], run_id="20260929-200000-bbbb")
    tracking.log([{**CSP, "strike": 10.0, "trade_id": "csp|F|2026-08-28|10"}],
                 run_id="20260929-100000-aaaa", sample="top")
    taken = paper.accept(dict(CSP, strike=9.0), run_id="20260929-200000-bbbb")
    frame = paper.list_positions()
    by_strike = dict(zip(frame["strike"], frame["sample"]))
    assert by_strike == {11.0: "lookup", 10.0: "top", 9.0: "lookup"}
    obs = tracking.observations()
    assert set(obs["sample"]) == {"lookup", "top"}
    assert set(paper.for_accuracy(frame)["strike"]) == {10.0}
    con = paper._connect()
    try:
        con.execute("UPDATE paper_positions SET status = 'expired_otm', exit_date = "
                    "DATE '2026-08-28', exit_price = 0, rec_prob_otm = 0.8")
    finally:
        con.close()
    stats = paper.performance()
    assert stats["n_closed"] == 3 and stats["n_scored"] == 1
    assert taken.position_id


# --- The Outlook without rewriting the universe table ---------------------------------------

def test_outlook_rows_rank_against_the_universe_without_saving(monkeypatch):
    saved = []

    def build(tickers, reporter=None, save=True, **kw):
        saved.append(save)
        return pd.DataFrame({"ticker": ["PLTR"], "horizon": [30], "vol_ratio": [2.0],
                             "vol_ratio_lo": [1.8], "vol_ratio_hi": [2.2], "volatility": [0.0],
                             "volatility_lo": [0.0], "volatility_hi": [0.0]})
    others = pd.DataFrame({"ticker": ["A", "B", "C", "PLTR"], "horizon": [30] * 4,
                           "vol_ratio": [0.8, 1.0, 1.2, 0.1], "vol_ratio_lo": [0.7] * 4,
                           "vol_ratio_hi": [1.3] * 4, "volatility": [1.0] * 4,
                           "volatility_lo": [1.0] * 4, "volatility_hi": [1.0] * 4})
    monkeypatch.setattr(outlook, "build", build)
    monkeypatch.setattr(outlook, "load_latest", lambda: others)
    monkeypatch.setattr(outlook, "load_skill",
                        lambda: pd.DataFrame({"symbol": ["ALL", "SPY"]}))
    rows, source = lookup.outlook_rows("PLTR", reporter=None)
    assert saved == [False] and source == "pooled"
    assert len(rows) == 1 and rows["volatility"].iloc[0] == pytest.approx(10.0 * 3.5 / 4)
    monkeypatch.setattr(outlook, "load_skill",
                        lambda: pd.DataFrame({"symbol": ["ALL", "PLTR"]}))
    assert lookup.outlook_rows("PLTR", reporter=None)[1] == "own"


# --- Weekly health check -------------------------------------------------------------------

def test_health_check_reports_mark_phases_and_proposes_a_fix(tmp_path, monkeypatch):
    from scripts import health_check
    monkeypatch.setattr(scheduler, "folder", lambda: tmp_path)
    hist = [
        {"key": "2026-09-29|mark|-|09:45", "job": "mark", "status": "ok",
         "planned": "2026-09-29T09:45:00-04:00", "duration_s": 230.0, "chain_s": 190.0,
         "marks_s": 40.0, "tickers": 20, "positions": 42,
         "message": "marked 42 open position(s), 42 priced"},
        {"key": "2026-09-30|mark|-|09:45", "job": "mark", "status": "ok",
         "planned": "2026-09-30T09:45:00-04:00", "duration_s": 1000.0, "chain_s": 600.0,
         "marks_s": 400.0, "tickers": 60, "positions": 420, "message": "marked 420"},
        {"key": "2026-09-30|archive|-|15:30", "job": "archive", "status": "missed",
         "planned": "2026-09-30T15:30:00-04:00", "message": "missed: late"},
    ]
    monkeypatch.setattr(scheduler, "history", lambda since=None, limit=None: hist)
    monkeypatch.setattr(lookup, "recent", lambda limit=20: [])
    text = health_check.report(7, now=_at("19:00"))
    assert "Jobs over 15 minutes: 1" in text and "2026-09-30|mark|-|09:45: 16.7 min" in text
    assert "Proposed fix" in text and "Reuse fresh chains" in text
    assert "9.5" in text and "0.95" in text                 # s/ticker, s/position
    assert "Not ok: 1" in text


def test_health_check_splits_old_marks_from_the_worker_log(tmp_path, monkeypatch):
    from scripts import health_check
    monkeypatch.setattr(scheduler, "folder", lambda: tmp_path)
    (tmp_path / "worker.log").write_text(
        "2026-09-29 11:45:04,584 11:45 mark: starting\n"
        "2026-09-29 11:45:04,778   Chains for open positions (20)\n"
        "2026-09-29 11:48:15,535   Chains for open positions: done 20\n"
        "2026-09-29 11:48:15,536   Marks and probabilities (42)\n"
        "2026-09-29 11:48:54,426   Marks and probabilities: done 42\n", encoding="utf-8")
    phases = health_check.log_phases()["2026-09-29T11:45"]
    assert phases["tickers"] == 20 and phases["positions"] == 42
    assert phases["chain_s"] == pytest.approx(191, abs=1) and phases["marks_s"] == pytest.approx(39, abs=1)


# --- macOS preparation ---------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only: detached pythonw worker")
def test_worker_spawn_on_windows_is_unchanged():
    argv, kwargs = scheduler.spawn_args()
    assert kwargs["creationflags"] == (subprocess.DETACHED_PROCESS
                                       | subprocess.CREATE_NEW_PROCESS_GROUP
                                       | subprocess.CREATE_NO_WINDOW)
    assert "start_new_session" not in kwargs
    assert argv[1].endswith("scheduler.py")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: worker in its own session")
def test_worker_spawn_on_posix_gets_its_own_session():
    argv, kwargs = scheduler.spawn_args()
    assert kwargs["start_new_session"] is True and "creationflags" not in kwargs
    assert argv[0] == sys.executable and argv[1].endswith("scheduler.py")


def test_launchd_plist(tmp_path):
    from scripts import launchd_agent
    plist = plistlib.loads(launchd_agent.plist_bytes("/u/csp/.venv/bin/python", tmp_path))
    assert plist["Label"] == launchd_agent.LABEL and plist["RunAtLoad"] is True
    assert "KeepAlive" not in plist
    assert plist["ProgramArguments"][:2] == ["/usr/bin/caffeinate", "-i"]
    assert plist["ProgramArguments"][-1].endswith("scheduler.py")
    assert plist["WorkingDirectory"] == str(tmp_path)
    bare = plistlib.loads(launchd_agent.plist_bytes("py", tmp_path, caffeinate=False))
    assert bare["ProgramArguments"][0] == "py"
