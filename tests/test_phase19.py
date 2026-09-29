"""Phase 19 -- the Phase 18 follow-ups (profile, run, per-contract P&L, flags),
the decisions before it (CSP 7 +/- 4 window, spread-leg volume warning,
request inheritance), and the scheduler (slot plan, missed/overrun rules,
auto-log rules, refusals)."""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import pytest

from analytics import paper, sizing, tracking
from analytics.scan_request import ScanRequest, missing_fields
from core import user_settings as us
from core.market_calendar import ET
from pipeline import scheduler

PCS = {"strategy": "pcs", "ticker": "SPY", "strike": 700.0, "long_strike": 690.0,
       "expiration": "2026-11-20", "modelled_fill": 2.00, "contracts": 3,
       "bid": 5.00, "ask": 5.20, "mid": 5.10, "long_bid": 3.00, "long_ask": 3.12,
       "long_mid": 3.06, "implied_vol": 0.18, "long_iv": 0.20, "spot": 740.0, "em": 25.0,
       "accepted": True, "rejections": (), "trade_id": "pcs|SPY|2026-11-20|700|690"}


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "user_settings.yaml"
    monkeypatch.setattr(us, "path", lambda: path)
    return path


@pytest.fixture
def ledger(tmp_path, monkeypatch, settings_file):
    db = tmp_path / "trade_log.duckdb"
    monkeypatch.setattr(paper, "db_trade_log", lambda: db)
    monkeypatch.setattr(tracking, "market_context", lambda *a, **k: {
        "iv_rank": 0.3, "iv_pct": 0.4, "trend": "range", "rsi": 50.0,
        "days_to_earnings": None, "vix_ratio": 0.8})
    paper.ensure_schema()
    return db


def _sheet(n=20, passing=14):
    rows = []
    for i in range(n):
        rows.append(dict(PCS, strike=700.0 - i, long_strike=690.0 - i, rank_key=float(n - i),
                         trade_id=f"pcs|SPY|2026-11-20|{700 - i}|{690 - i}",
                         accepted=i < passing, rejections=() if i < passing else ("one",)))
    return pd.DataFrame(rows)


# --- Phase 18 follow-ups -------------------------------------------------------------------

def test_profile_and_size_are_stamped(ledger):
    us.save_account_profile("roth_ira", {"net_liquidating_value": 36000.0})
    us.save_account_profile("taxable", {"net_liquidating_value": 250000.0, "placeholder": True})
    real = tracking.log([PCS], run_id="r1", account_profile="roth_ira")[0]["position_id"]
    fake = tracking.log([dict(PCS, trade_id="x")], account_profile="taxable")[0]["position_id"]
    research = tracking.log([dict(PCS, trade_id="y")])[0]["position_id"]
    taken = paper.accept(dict(PCS, trade_id=None), actual_fill=2.0).position_id
    rows = paper.list_positions().set_index("id")
    assert rows.loc[real, "account_profile"] == "roth_ira"
    assert rows.loc[real, "profile_nlv"] == 36000.0 and rows.loc[real, "run_id"] == "r1"
    assert rows.loc[real, "sized_contracts"] == 3
    assert bool(rows.loc[real, "dollar_pnl_valid"])
    assert not bool(rows.loc[fake, "dollar_pnl_valid"])              # placeholder profile
    assert rows.loc[research, "account_profile"] == "default"
    assert not bool(rows.loc[research, "dollar_pnl_valid"])          # research $3M
    assert bool(rows.loc[taken, "dollar_pnl_valid"])                 # real fills are real


def test_profile_comes_from_the_runs_request(ledger, tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    (runs / "r9").mkdir(parents=True)
    (runs / "r9" / "manifest.json").write_text(json.dumps(
        {"request": {"account_profile": "roth_ira"}}))
    monkeypatch.setattr("core.paths.runs_dir", lambda: runs)
    us.save_account_profile("roth_ira", {"net_liquidating_value": 36000.0})
    pid = tracking.log([PCS], run_id="r9")[0]["position_id"]
    assert paper.list_positions().set_index("id").loc[pid, "account_profile"] == "roth_ira"


def test_legacy_rows_are_tagged_default_and_left_out_of_dollars(tmp_path, monkeypatch,
                                                                  settings_file):
    import duckdb
    db = tmp_path / "old.duckdb"
    monkeypatch.setattr(paper, "db_trade_log", lambda: db)
    con = duckdb.connect(str(db))
    for statement in paper.SCHEMA:
        con.execute(statement)
    for column in ("book", "sample", "hold_pnl"):                   # a Phase 18 book
        con.execute(f"ALTER TABLE paper_positions ADD COLUMN {column} "
                    f"{paper.POSITION_COLUMNS[column]}")
    for book, pnl in (("tracked", 100.0), ("taken", 50.0)):
        con.execute("INSERT INTO paper_positions (ticker, strategy, strike, expiration, "
                    "contracts, modelled_fill, status, book, entry_date, exit_date, exit_price, "
                    "entry_fees, exit_fees, collateral, hold_pnl) VALUES ('F', 'csp', 11, "
                    "DATE '2026-08-28', 4, 0.2, 'expired_otm', ?, DATE '2026-08-21', "
                    "DATE '2026-08-28', 0, 0, 0, 4400, ?)", [book, pnl])
    con.close()
    rows = paper.list_positions().sort_values("id")
    assert list(rows["account_profile"]) == ["default", "default"]
    assert list(rows["dollar_pnl_valid"]) == [False, True]
    assert list(rows["sized_contracts"]) == [4, 4]
    assert list(rows["hold_pnl_per_contract"]) == [25.0, 12.5]
    stats = paper.performance()
    assert stats["n_closed"] == 2 and stats["win_rate"] == 1.0       # rates: every row
    assert stats["total_realized"] == pytest.approx(0.2 * 400)       # dollars: taken only
    assert stats["n_dollar_excluded"] == 1


def test_flags_are_idempotent(ledger):
    pid = tracking.log([PCS])[0]["position_id"]
    assert paper.add_flag([pid], "pre_fix_bars") == 1
    assert paper.add_flag([pid], "pre_fix_bars") == 0
    assert paper.add_flag([pid], "other") == 1
    assert paper.list_positions().iloc[0]["flags"] == "pre_fix_bars,other"
    with pytest.raises(ValueError):
        paper.add_flag([pid], "Bad Flag")


def test_outcomes_and_marks_per_contract(ledger, monkeypatch):
    pid = tracking.log([PCS])[0]["position_id"]                     # 3 contracts at 2.00
    con = paper._connect()
    con.execute("INSERT INTO position_marks (position_id, marked_at, mark, verdict, "
                "verdict_reason) VALUES (?, TIMESTAMP '2026-10-20 11:00', 0.9, 'close', "
                "'profit target reached')", [pid])
    con.close()
    monkeypatch.setattr(tracking, "_close_on", lambda t, d: 745.0)
    tracking.expire_due(today=dt.date(2026, 11, 20))
    row = paper.list_positions().set_index("id").loc[pid]
    assert row["hold_pnl_per_contract"] == pytest.approx(row["hold_pnl"] / 3)
    assert row["managed_pnl_per_contract"] == pytest.approx(row["managed_pnl"] / 3)
    fees = (2.0 - 0.9) * 300 - row["managed_pnl"]                   # entry + close fees
    assert row["managed_pnl_per_contract"] == pytest.approx((2.0 - 0.9) * 100 - fees / 3)


def test_update_stores_pnl_per_contract(ledger, monkeypatch):
    from data_sources import chains
    tracking.log([PCS])
    chain = pd.DataFrame([{"expiration": "2026-11-20", "strike_price": k, "root_symbol": "SPY",
                           "put_bid": m - 0.05, "put_ask": m + 0.05, "put_mark": m,
                           "put_iv": 0.18, "put_delta": -0.2, "put_gamma": 0.004,
                           "put_theta": -0.1, "put_vega": 0.9}
                          for k, m in ((700.0, 4.4), (690.0, 2.9))])
    monkeypatch.setattr(chains, "load_chain", lambda ticker, block=None:
                        (chain, pd.DataFrame([{"mark": 745.0}])))
    out = tracking.update(pull=False, n_paths=300, now=dt.datetime(2026, 9, 28, 11, 0))
    assert out.iloc[0]["pnl_per_contract"] == pytest.approx((2.0 - 1.5) * 100)
    assert tracking.marks().iloc[0]["pnl_per_contract"] == pytest.approx(50.0)


# --- Auto-logging rules ----------------------------------------------------------------------

def test_auto_eligible_drops_naked_and_research_only():
    sheet = pd.DataFrame({"trade_id": ["a", "b", "c", "d"],
                          "margin_class": ["defined_risk", "naked", "defined_risk", None],
                          "research_only": [False, False, True, None]})
    assert list(tracking.auto_eligible(sheet)["trade_id"]) == ["a", "d"]


def test_auto_log_respects_the_daily_cap_and_dedupes(ledger):
    sheet = _sheet()
    first = tracking.auto_log(sheet, "run-1", "auto_pcs", "default", k=5, m=3, daily_cap=6,
                              today=dt.date.today())
    assert first["opened"] == 6 and first["capped"] == 5          # 5 top + 3 + 3 control
    top = paper.list_positions()
    assert set(top[top["sample"] == "top"]["trade_id"]) == {
        f"pcs|SPY|2026-11-20|{700 - i}|{690 - i}" for i in range(5)}  # top first
    again = tracking.auto_log(sheet, "run-1", "auto_pcs", "default", k=5, m=3, daily_cap=6,
                              today=dt.date.today())
    assert again["opened"] == 0 and again["observed"] == 6         # no duplicates
    assert len(paper.list_positions()) == 6


def test_observe_only_opens_nothing(ledger):
    tracking.log([PCS], preset="auto_pcs")
    out = tracking.observe_only(_sheet(5, 5), "run-2", "auto_pcs")
    assert out["observed"] == 1 and len(paper.list_positions()) == 1
    assert {r["action"] for r in out["results"]} == {"observed", "not_open"}


# --- Decisions before Phase 19 -------------------------------------------------------------

def test_csp_window_is_7_plus_minus_4_and_holds_a_friday_every_weekday():
    request = ScanRequest.default()
    assert request.dte_window("csp") == (3, 11)
    monday = dt.date(2026, 9, 28)
    for offset in range(5):                                         # Mon..Fri
        day = monday + dt.timedelta(days=offset)
        fridays = [(day + dt.timedelta(days=d)).weekday() == 4 for d in range(3, 12)]
        assert any(fridays), day


def test_spread_leg_low_volume_is_a_warning_not_a_rejection():
    account = sizing.AccountState(net_liquidating_value=1_000_000, cash_available=1_000_000)
    floors = {"min_open_interest": 100, "min_option_volume": 0, "warn_option_volume": 25}
    result = sizing.max_contracts_for_position(
        500.0, account, legs=[(5000, 3, "short leg"), (4000, 40, "long leg")],
        leg_floors=floors)
    assert result.ok and result.contracts >= 1
    assert len(result.warnings) == 1 and "short leg: low volume" in result.warnings[0]
    thin = sizing.max_contracts_for_position(500.0, account, legs=[(80, 3, "")],
                                             leg_floors=floors)
    assert thin.rejected                                            # OI 100 is still a gate


def test_json_requests_inherit_scan_defaults_and_say_so():
    partial = ScanRequest.from_dict({"strategies": ["pcs"], "universe": ["SPY"]})
    assert partial.spread_width_pct == [4.0] and partial.pcs_dte_targets == [45]
    assert "spread_width_pct" in partial.inherited and "top_n_underlyings" in partial.inherited
    assert "spread_width_pct" in partial.inherit_warning()
    explicit = ScanRequest.from_dict(partial.to_dict())
    assert explicit.inherited == () and explicit.inherit_warning() is None
    assert missing_fields(partial.to_dict()) == []
    assert "strike_rule" in missing_fields({"strategies": ["csp"]})
    assert ScanRequest.default().inherited == ()


# --- Scheduler -------------------------------------------------------------------------------

CFG = {"enabled": True, "grace_minutes": 30, "mark": {"start": "09:45", "end": "15:45",
                                                        "every_minutes": 60},
       "scan_and_log": {"time": "10:45"}, "archive": {"time": "15:30"},
       "nightly": {"time": "18:30"},
       "auto_presets": {"auto_pcs": {"top_k": 5, "control_m": 3}}}


def _at(day, hhmm):
    h, m = hhmm.split(":")
    return dt.datetime.combine(day, dt.time(int(h), int(m)), ET)


def test_plan_normal_day_half_day_and_holiday():
    day = dt.date(2026, 9, 28)
    plan = scheduler.plan_day(day, CFG)
    marks = [s for s in plan if s.job == "mark"]
    assert [s.when.strftime("%H:%M") for s in marks] == [
        "09:45", "10:45", "11:45", "12:45", "13:45", "14:45", "15:45"]
    logs = [s for s in plan if s.job == "scan_and_log"]
    assert len(logs) == 1 and logs[0].preset == "auto_pcs" and logs[0].when == _at(day, "10:45")
    at_1045 = [s.job for s in plan if s.when == _at(day, "10:45")]
    assert at_1045 == ["mark", "scan_and_log"]                     # mark first
    assert [s.job for s in plan][-3:] == ["archive", "mark", "nightly"]   # 15:30, 15:45, 18:30
    auto = scheduler.auto_presets(CFG)["auto_pcs"]
    assert auto["daily_cap"] == 11                                 # K + 2M
    half = scheduler.plan_day(dt.date(2026, 11, 27), CFG)          # 13:00 close
    assert max(s.when for s in half if s.job == "mark") == _at(dt.date(2026, 11, 27), "12:45")
    assert [s for s in half if s.job == "archive"][0].when == _at(dt.date(2026, 11, 27), "12:30")
    assert scheduler.plan_day(dt.date(2026, 11, 26), CFG) == []    # Thanksgiving
    assert scheduler.plan_day(dt.date(2026, 10, 3), CFG) == []     # Saturday
    assert scheduler.plan_day(day, {**CFG, "enabled": False}) == []


def test_observe_hourly_fills_the_other_mark_slots():
    cfg = {**CFG, "auto_presets": {"auto_pcs": {"observe_hourly": True}}}
    observe = [s for s in scheduler.plan_day(dt.date(2026, 9, 28), cfg) if s.job == "observe"]
    assert len(observe) == 6 and all(s.when.strftime("%H:%M") != "10:45" for s in observe)


def test_auto_presets_can_be_staggered():
    cfg = {**CFG, "auto_presets": {"a": {}, "b": {"time": "11:15"}, "c": {"time": "11:30"}}}
    logs = [s for s in scheduler.plan_day(dt.date(2026, 9, 28), cfg) if s.job == "scan_and_log"]
    assert [(s.preset, s.when.strftime("%H:%M")) for s in logs] == [
        ("a", "10:45"), ("b", "11:15"), ("c", "11:30")]


def test_decide_wait_run_missed_and_overrun():
    day = dt.date(2026, 9, 28)
    slot = scheduler.Slot("mark", _at(day, "11:45"))
    assert scheduler.decide(slot, _at(day, "11:40"), [])[0] == "wait"
    assert scheduler.decide(slot, _at(day, "12:10"), [])[0] == "run"       # inside the grace
    verdict, why = scheduler.decide(slot, _at(day, "12:20"), [])
    assert verdict == "missed" and "35 min late" in why
    previous = {"job": "mark", "preset": None, "started": _at(day, "10:45").isoformat(),
                "finished": _at(day, "11:50").isoformat()}
    verdict, why = scheduler.decide(slot, _at(day, "11:51"), [previous])
    assert verdict == "skipped" and why.startswith("overrun")
    other = dict(previous, job="archive")                                  # another job: no
    assert scheduler.decide(slot, _at(day, "11:51"), [other])[0] == "run"


def test_tick_records_missed_slots_and_returns_the_due_one(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "folder", lambda: tmp_path)
    day = dt.date(2026, 9, 28)
    slot = scheduler.tick(_at(day, "11:50"), CFG, {})
    assert slot is not None and slot.job == "mark" and slot.when == _at(day, "11:45")
    hist = scheduler.history()
    assert [(h["job"], h["status"]) for h in hist] == [
        ("mark", "missed"), ("mark", "missed"), ("scan_and_log", "missed")]
    # Recorded once: the next tick does not duplicate them.
    scheduler.tick(_at(day, "11:51"), CFG, {})
    assert len(scheduler.history()) == 3


def test_execute_records_ok_failed_and_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "folder", lambda: tmp_path)
    day = dt.date(2026, 9, 28)
    monkeypatch.setitem(scheduler.RUNNERS, "mark", lambda s, r: {"message": "marked 2"})
    monkeypatch.setitem(scheduler.RUNNERS, "archive", lambda s, r: 1 / 0)

    def refuse(slot, reporter):
        raise scheduler.Refused("profile 'roth_ira' holds placeholder values")
    monkeypatch.setitem(scheduler.RUNNERS, "scan_and_log", refuse)
    scheduler.execute(scheduler.Slot("mark", _at(day, "09:45")))
    scheduler.execute(scheduler.Slot("archive", _at(day, "15:45")))
    scheduler.execute(scheduler.Slot("scan_and_log", _at(day, "10:45"), "auto_pcs"))
    statuses = {h["job"]: (h["status"], h["message"]) for h in scheduler.history()}
    assert statuses["mark"] == ("ok", "marked 2")
    assert statuses["archive"][0] == "failed" and "ZeroDivisionError" in statuses["archive"][1]
    assert statuses["scan_and_log"][0] == "refused"


def test_auto_presets_must_be_explicit_and_not_placeholder(settings_file):
    us.save_account_profile("roth_ira", {"net_liquidating_value": 36000.0, "placeholder": True})
    request = ScanRequest.default(account_profile="roth_ira", universe=["SPY"]).to_dict()
    us.save_scan_preset("auto_pcs", request)
    us.set_auto_preset("auto_pcs", {"top_k": 5, "control_m": 3})
    with pytest.raises(scheduler.Refused, match="placeholder"):
        scheduler.check_auto("auto_pcs")
    us.save_account_profile("roth_ira", {"net_liquidating_value": 36000.0, "placeholder": False})
    assert scheduler.check_auto("auto_pcs").account_profile == "roth_ira"
    # A hand-edited preset that leaves fields out is refused, and cannot be marked auto.
    data = us.load()
    data["scan_presets"]["thin"] = {"strategies": ["csp"], "universe": ["SPY"]}
    us._write(data)
    with pytest.raises(us.SettingsError, match="not fully explicit"):
        us.set_auto_preset("thin", {"top_k": 5})
    data = us.load()
    data["schedule"]["auto_presets"]["thin"] = {"top_k": 5}
    us._write(data)
    with pytest.raises(scheduler.Refused, match="not fully explicit"):
        scheduler.check_auto("thin")
    with pytest.raises(scheduler.Refused, match="no longer exists"):
        scheduler.check_auto("gone")
    us.delete_scan_preset("auto_pcs")
    assert "auto_pcs" not in (us.load()["schedule"]["auto_presets"])


def test_schedule_settings_validate(settings_file):
    us.save_schedule({"scan_and_log": {"time": "11:15"}, "grace_minutes": 20})
    cfg = scheduler.settings()
    assert cfg["scan_and_log"]["time"] == "11:15" and cfg["grace_minutes"] == 20
    assert cfg["mark"]["start"] == "09:45"                          # config default kept
    with pytest.raises(us.SettingsError):
        us.save_schedule({"nightly": {"time": "25:00"}})
    with pytest.raises(us.SettingsError):
        us.save_schedule({"bogus": 1})


# --- Fixes from the first unattended day (2026-09-29) ------------------------------------------

def test_holdings_for_portfolio_limits_count_taken_only(ledger):
    from pipeline import run
    tracking.log([PCS])                                              # tracked SPY
    paper.accept(dict(PCS, ticker="QQQ", trade_id=None), actual_fill=2.0)   # taken QQQ
    assert run._tickers_with_positions() == {"SPY", "QQQ"}           # chains: both
    assert run._tickers_with_positions(book="taken") == {"QQQ"}      # limits: real trades


def test_census_explains_every_ticker(monkeypatch):
    from analytics import candidates
    from data_sources import chains
    monkeypatch.setattr(chains, "load_chain", lambda t, block=None, complete=False: (pd.DataFrame(), pd.DataFrame()))
    frame = candidates.evaluate_universe(["XSP"], request=ScanRequest.default(
        strategies=["pcs"], universe=["XSP"]))
    assert frame.empty
    assert frame.attrs["census"]["ticker_notes"] == {"XSP": "no chain snapshot"}


def test_blocks_sort_chronologically_and_targeted_snapshots_are_skipped(tmp_path, monkeypatch):
    from data_sources import chains
    monkeypatch.setattr(chains, "chains_dir", lambda: tmp_path)
    blocks = ["2026-09-29_post", "2026-09-29_rth_09", "2026-09-29_rth_15", "2026-09-29_pre",
              "2026-09-28_closed", "2026-09-29_rth_10"]
    for block in blocks:
        chain_path, under_path = chains._paths("XSP", block)
        chain_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"expiration": ["2026-11-20"]}).to_parquet(chain_path)
        pd.DataFrame([{"mark": 700.0, "targeted_only": block in ("2026-09-29_post",
                                                                   "2026-09-29_rth_15")}]
                     ).to_parquet(under_path)
    assert chains.list_blocks() == ["2026-09-28_closed", "2026-09-29_pre", "2026-09-29_rth_09",
                                    "2026-09-29_rth_10", "2026-09-29_rth_15", "2026-09-29_post"]
    assert chains.latest_block_for("XSP") == "2026-09-29_post"                 # the freshest
    assert chains.latest_block_for("XSP", complete=True) == "2026-09-29_rth_10"  # full chain
