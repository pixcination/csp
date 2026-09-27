"""
Phase 9: universe registry and symbol mapping, batched yfinance with indices,
weekly bars without lookahead, Stage 1 on yfinance, TastyTrade market
metrics, the events table and the event policy.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data_sources import events, tasty_metrics, universe  # noqa: E402
import data_sources.yfinance_sync as ys  # noqa: E402


# --- Symbol mapping ----------------------------------------------------------

@pytest.mark.parametrize("raw, canonical", [
    ("brk-b", "BRK.B"), ("BRK/B", "BRK.B"), ("BRK.B", "BRK.B"),
    ("^spx", "SPX"), (" aapl ", "AAPL")])
def test_symbols_normalise_to_one_canonical_form(raw, canonical):
    assert universe.normalise(raw) == canonical


@pytest.mark.parametrize("symbol, expected", [
    ("BRK.B", ("BRK-B", "BRK/B", 1.0)),
    ("AAPL", ("AAPL", "AAPL", 1.0)),
    ("SPX", ("^SPX", "SPX", 1.0)),
    ("XSP", ("^SPX", "XSP", 0.1)),     # ^XSP only exists from 2021
    ("NDX", ("^NDX", "NDX", 1.0)),
    ("RUT", ("^RUT", "RUT", 1.0))])
def test_vendor_mapping(symbol, expected):
    assert universe.default_mapping(symbol) == expected


# --- Registry ------------------------------------------------------------------

@pytest.fixture
def registry(tmp_path, monkeypatch):
    """An isolated registry seeded from a small universe file + tags."""
    (tmp_path / "config").mkdir()
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "final_universe.txt").write_text(
        "# test\nAAPL\nSPY\nBRK-B\n", encoding="utf-8")
    pd.DataFrame({"ticker": ["AAPL", "SPY", "BRK-B"], "asset_class": ["Stock", "ETF", "Stock"],
                  "category": ["technology", "broad_index_us", "financials"],
                  "quality_tier": ["core", "core", "core"],
                  "leverage_flag": ["No", "No", "No"], "notes": ["", "", ""]}).to_csv(
        tmp_path / "output" / "stage2_quality_tags_master.csv", index=False)
    monkeypatch.setattr(universe, "db_universe", lambda: tmp_path / "universe.duckdb")
    monkeypatch.setattr(universe, "config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(universe, "output_dir", lambda: tmp_path / "output")
    monkeypatch.setattr(universe, "load_universe_file",
                        lambda: ["AAPL", "SPY", "BRK-B"])
    return tmp_path


def test_seed_brings_in_the_universe_file_tags_and_defaults(registry):
    universe.ensure()
    frame = universe.load().set_index("symbol")
    assert {"AAPL", "SPY", "BRK.B", "QQQ", "IWM", "DIA", "SPX", "XSP", "NDX", "RUT"} \
        <= set(frame.index)
    assert frame.loc["SPY", "asset_class"] == "etf"
    assert frame.loc["BRK.B", "category"] == "financials"
    assert frame.loc["BRK.B", "yf_symbol"] == "BRK-B"
    assert frame.loc["SPX", "settlement"] == "cash"
    assert frame.loc["SPX", "exercise"] == "european"
    assert frame.loc["AAPL", "settlement"] == "physical"


def test_csp_scope_excludes_cash_settled_indices(registry):
    csp, everything = universe.symbols("csp"), universe.symbols("all")
    assert "SPX" in everything and "SPX" not in csp
    assert "SPY" in csp and "AAPL" in csp


def test_add_deactivate_and_snapshot(registry):
    universe.add("msft", tags="megacap")
    assert "MSFT" in universe.symbols("all")
    universe.set_active("MSFT", False)
    assert "MSFT" not in universe.symbols("all")
    snapshot = pd.read_csv(registry / "config" / universe.SNAPSHOT)
    assert "MSFT" in set(snapshot["symbol"])
    assert snapshot.set_index("symbol").loc["MSFT", "tags"] == "megacap"


def test_registry_reseeds_from_the_versioned_snapshot(registry):
    universe.add("MSFT", tags="megacap")
    (registry / "universe.duckdb").unlink()            # data/ wiped
    universe.ensure()
    frame = universe.load().set_index("symbol")
    assert frame.loc["MSFT", "tags"] == "megacap"      # hand edits survived


def test_update_rejects_unknown_fields(registry):
    with pytest.raises(ValueError):
        universe.update("AAPL", colour="blue")


def test_load_universe_falls_back_to_the_text_file(monkeypatch):
    from core import paths
    import data_sources.universe as reg
    monkeypatch.setattr(reg, "symbols", lambda scope="csp": [])
    monkeypatch.setattr(paths, "load_universe_file", lambda: ["ZZZ"])
    assert paths.load_universe() == ["ZZZ"]


# --- Batched yfinance with one series serving two symbols ------------------------

def _yahoo_frame(n=30, start=100.0):
    dates = pd.bdate_range("2026-01-02", periods=n)
    close = start + np.arange(n, dtype=float)
    return pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1,
                         "Close": close, "Adj Close": close, "Volume": 1e6,
                         "Dividends": 0.0, "Stock Splits": 0.0},
                        index=pd.DatetimeIndex(dates, name="Date"))


def test_one_download_fills_spx_and_scaled_xsp(tmp_path, monkeypatch):
    path = tmp_path / "daily.duckdb"
    monkeypatch.setattr(ys, "db_universe_daily", lambda: path)
    monkeypatch.setattr(ys, "previous_trading_day", lambda d: dt.date(2099, 1, 1))
    monkeypatch.setattr(ys, "_vendor_map", lambda t: {"SPX": ("^SPX", 1.0),
                                                      "XSP": ("^SPX", 0.1)})
    calls = []

    class FakeYF:
        def download(self, symbols, **kwargs):
            calls.append(list(symbols))
            return pd.concat({s: _yahoo_frame(start=7000.0) for s in symbols}, axis=1)

    monkeypatch.setattr(ys, "_yf", lambda: FakeYF())
    results = ys.sync_daily(["SPX", "XSP"])
    assert all(r.error is None for r in results)
    assert calls == [["^SPX"]]                          # downloaded once
    spx, xsp = ys.load_daily("SPX"), ys.load_daily("XSP")
    assert xsp["close"].iloc[-1] == pytest.approx(spx["close"].iloc[-1] / 10)


def test_missing_symbol_reports_an_error_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(ys, "db_universe_daily", lambda: tmp_path / "d.duckdb")
    monkeypatch.setattr(ys, "previous_trading_day", lambda d: dt.date(2099, 1, 1))
    monkeypatch.setattr(ys, "_vendor_map", lambda t: {s: (s, 1.0) for s in t})

    class FakeYF:
        def download(self, symbols, **kwargs):
            present = [s for s in symbols if s != "GONE"]
            return pd.concat({s: _yahoo_frame() for s in present}, axis=1) if present \
                else pd.DataFrame()

    monkeypatch.setattr(ys, "_yf", lambda: FakeYF())
    monkeypatch.setattr("time.sleep", lambda s: None)
    results = {r.ticker: r for r in ys.sync_daily(["AAA", "GONE"])}
    assert results["AAA"].error is None and results["AAA"].rows_added == 30
    assert results["GONE"].error == "no data returned"


# --- Weekly bars: no lookahead --------------------------------------------------

def _daily(start="2026-03-02", end="2026-04-17"):
    dates = [d for d in pd.bdate_range(start, end)
             if d.date() != dt.date(2026, 4, 3)]          # Good Friday 2026
    close = np.arange(len(dates), dtype=float) + 100
    return pd.DataFrame({"date": dates, "open": close, "high": close + 1,
                         "low": close - 1, "close": close, "volume": 1.0})


def test_weekly_resample_ohlc():
    from analytics import bars
    daily = _daily()
    wk = bars.weekly(daily).set_index("week_end")
    first = wk.loc[pd.Timestamp("2026-03-06")]
    assert first["open"] == 100 and first["close"] == 104
    assert first["high"] == 105 and first["low"] == 99 and first["sessions"] == 5


def test_weekly_values_on_a_daily_timeline_never_look_ahead():
    from analytics import bars
    daily = _daily()
    aligned = bars.weekly_on_daily(daily)
    closes = daily.set_index("date")["close"]
    # Wednesday 2026-03-11 must see the week ending 03-06, not its own week.
    assert aligned[pd.Timestamp("2026-03-11")] == closes[pd.Timestamp("2026-03-06")]
    # On Friday's close the week is complete and becomes usable.
    assert aligned[pd.Timestamp("2026-03-13")] == closes[pd.Timestamp("2026-03-13")]
    # The first week has nothing completed before its Friday.
    assert np.isnan(aligned[pd.Timestamp("2026-03-04")])
    # No daily value ever equals a close from a later date.
    for day, value in aligned.dropna().items():
        assert value in set(closes[closes.index <= day])


def test_holiday_week_completes_on_its_last_session():
    """Good Friday 2026-04-03: the week is complete at Thursday's close."""
    from analytics import bars
    daily = _daily()
    wk = bars.weekly(daily).set_index("week_end")
    week = wk.loc[pd.Timestamp("2026-04-03")]
    assert week["scheduled_last_session"] == pd.Timestamp("2026-04-02")
    assert week["sessions"] == 4 and week["complete"]
    aligned = bars.weekly_on_daily(daily)
    assert aligned[pd.Timestamp("2026-04-02")] == week["close"]


def test_last_completed_week_as_of_midweek():
    from analytics import bars
    week = bars.last_completed_week(_daily(), dt.date(2026, 3, 18))
    assert week["week_end"] == pd.Timestamp("2026-03-13")


# --- Stage 1 on yfinance -----------------------------------------------------------

THRESHOLDS = {"min_history_years": 3, "max_staleness_days": 10, "min_adv_dollars": 5e7,
              "min_price": 15, "max_price": 1000, "min_rv": 0.12, "max_rv": 0.75,
              "max_drawdown_floor": -0.65}


def _metrics(**kw):
    base = {"history_years": 10.0, "days_since_last": 1, "adv_90d_dollars": 1e9,
            "last_price": 100.0, "rv_20d_annualized": 0.25, "max_drawdown": -0.3,
            "pct_off_peak_now": -0.05}
    base.update(kw)
    return base


def test_stage1_rules_match_scripts_02():
    from analytics.universe_screen import classify
    assert classify(_metrics(), THRESHOLDS) == ("tier1_backtestable", [])
    assert classify(_metrics(history_years=2), THRESHOLDS)[0] == "tier2_limited_history"
    assert classify(_metrics(adv_90d_dollars=1e6), THRESHOLDS)[1] == ["adv_too_low"]
    assert classify(_metrics(rv_20d_annualized=0.10), THRESHOLDS)[1] == ["volatility_out_of_range"]
    assert classify(_metrics(max_drawdown=-0.8, pct_off_peak_now=-0.5),
                    THRESHOLDS)[1] == ["unrecovered_drawdown"]
    # fell hard but recovered: not rejected
    assert classify(_metrics(max_drawdown=-0.8, pct_off_peak_now=-0.1), THRESHOLDS)[1] == []


def test_drawdown_window_ignores_an_ancient_crash():
    from analytics.universe_screen import metrics
    dates = pd.bdate_range("2000-01-03", "2026-09-25")
    close = np.full(len(dates), 50.0)
    close[:500] = 200.0                                    # a 2000-2001 peak
    daily = pd.DataFrame({"date": dates, "close": close, "volume": 1e7})
    all_history = metrics(daily, dt.date(2026, 9, 25))
    recent = metrics(daily, dt.date(2026, 9, 25), drawdown_years=10)
    assert all_history["pct_off_peak_now"] == pytest.approx(-0.75)
    assert recent["pct_off_peak_now"] == pytest.approx(0.0)
    assert recent["history_years"] == all_history["history_years"]


# --- TastyTrade market metrics ---------------------------------------------------------

ITEM = {   # shape verified live 2026-09-27 (trimmed)
    "symbol": "AAPL", "implied-volatility-index": "0.23753549",
    "implied-volatility-index-rank": "0.429646786",
    "implied-volatility-index-rank-source": "tos",
    "tos-implied-volatility-index-rank": "0.429646786",
    "tw-implied-volatility-index-rank": "0.253995187",
    "implied-volatility-percentile": "0.114166632",
    "implied-volatility-30-day": "23.75", "historical-volatility-30-day": "21.49",
    "liquidity-rating": 4, "beta": "1.070028424",
    "dividend-next-date": "2022-08-05", "dividend-ex-date": "2026-08-10",
    "earnings": {"visible": True, "expected-report-date": "2026-10-29", "estimated": False},
    "sector": "Technology",
    "option-expiration-implied-volatilities": [
        {"expiration-date": d, "option-chain-type": "Standard", "settlement-type": s,
         "implied-volatility": "0.2"}
        for d, s in [("2026-09-25", "PM"), ("2026-10-02", "PM"), ("2026-10-09", "PM"),
                     ("2026-10-16", "AM"), ("2026-10-16", "PM"), ("2026-12-18", "PM")]],
}


def test_market_metrics_parse_units_and_fields():
    row = tasty_metrics.parse(ITEM, "AAPL", dt.date(2026, 9, 27))
    assert row["ivr"] == pytest.approx(0.4296, abs=1e-4)          # fraction
    assert row["ivr_tw"] == pytest.approx(0.2540, abs=1e-4)
    assert row["iv_30d"] == pytest.approx(0.2375)                 # percent -> fraction
    assert row["hv_30d"] == pytest.approx(0.2149)
    assert row["earnings_date"] == dt.date(2026, 10, 29)
    assert row["earnings_estimated"] is False
    assert row["dividend_ex_date"] == dt.date(2026, 8, 10)        # never the stale next-date
    assert row["n_expirations"] == 5                              # past one dropped
    assert row["weeklies"] is True
    assert row["settlement_times"] == "AM+PM"


def test_index_metrics_without_earnings_parse():
    item = {"symbol": "SPX", "implied-volatility-index-rank": "0.3", "earnings": None,
            "option-expiration-implied-volatilities": []}
    row = tasty_metrics.parse(item, "SPX", dt.date(2026, 9, 27))
    assert row["earnings_date"] is None and row["weeklies"] is False


# --- Events ------------------------------------------------------------------------------

def test_third_friday_and_holiday_moves():
    assert events.third_friday(2026, 9) == dt.date(2026, 9, 18)
    rows = {r["date"]: r for r in events.expiration_events(dt.date(2026, 6, 1),
                                                           dt.date(2026, 6, 30))}
    # June 2026's third Friday is Juneteenth: quad witching moves to Thursday.
    assert dt.date(2026, 6, 18) in rows
    assert rows[dt.date(2026, 6, 18)]["type"] == "quad_witching"
    assert "holiday" in rows[dt.date(2026, 6, 18)]["note"]


def test_macro_calendar_file_parses_with_verified_fomc_dates():
    rows = events.macro_events()
    fomc = sorted(r["date"] for r in rows if r["type"] == "fomc")
    assert len(fomc) == 16
    assert dt.date(2026, 10, 28) in fomc and dt.date(2027, 12, 8) in fomc
    assert all(r["time_of_day"] == "14:00" for r in rows if r["type"] == "fomc")
    assert any(r["type"] == "cpi" and r["date"] == dt.date(2026, 10, 14) for r in rows)


def _events_frame(rows):
    frame = pd.DataFrame([events._row(**r) for r in rows])
    frame["built_at"] = dt.datetime.now()
    return frame


@pytest.fixture
def event_table(monkeypatch):
    def install(rows):
        frame = _events_frame(rows)
        monkeypatch.setattr(events, "load", lambda: frame)
    return install


START, END = dt.date(2026, 10, 1), dt.date(2026, 10, 9)


def test_earnings_inside_the_window_blocks_a_stock(event_table):
    event_table([dict(symbol="AAPL", date=dt.date(2026, 10, 8), type_="earnings",
                      time_of_day="amc")])
    check = events.check("AAPL", START, END, "csp", asset_class="stock")
    assert check.blocks and check.earnings_block is not None


def test_earnings_after_expiry_does_not_block(event_table):
    event_table([dict(symbol="AAPL", date=dt.date(2026, 10, 29), type_="earnings")])
    assert events.check("AAPL", START, END, "csp", asset_class="stock").action == "ok"


def test_unknown_earnings_blocks_stocks_but_never_etfs(event_table):
    """Before Phase 9 this rule rejected every ETF on every run."""
    event_table([])
    assert events.check("AAPL", START, END, "csp", asset_class="stock").blocks
    assert events.check("SPY", START, END, "csp", asset_class="etf").action == "ok"
    assert events.check("SPX", START, END, "pcs", asset_class="index").action == "ok"


def test_unknown_earnings_only_warns_when_the_calendar_is_degraded(event_table):
    event_table([])
    check = events.check("AAPL", START, END, "csp", asset_class="stock",
                         calendar_healthy=False)
    assert check.action == "warn"


def test_macro_events_warn_and_ignored_types_do_not_appear(event_table):
    event_table([dict(symbol="*", date=dt.date(2026, 10, 2), type_="nfp"),
                 dict(symbol="*", date=dt.date(2026, 10, 7), type_="cpi"),
                 dict(symbol="AAPL", date=dt.date(2026, 10, 29), type_="earnings")])
    check = events.check("AAPL", START, END, "csp", asset_class="stock")
    assert check.action == "warn"
    assert [h.type for h in check.hits] == ["cpi"]              # nfp is ignore


def test_ex_dividend_policy_applies_to_short_calls_only(event_table):
    event_table([dict(symbol="KO", date=dt.date(2026, 10, 5), type_="ex_dividend"),
                 dict(symbol="KO", date=dt.date(2026, 10, 29), type_="earnings")])
    assert events.check("KO", START, END, "csp", asset_class="stock").action == "ok"
    assert events.check("KO", START, END, "covered_call", asset_class="stock").action == "warn"


def test_days_before_widens_the_window(event_table, monkeypatch):
    cfg = {"event_policy": {"fomc": {"action": "block", "days_before": 2,
                                     "days_after": 0, "applies_to": ["csp"]}}}
    monkeypatch.setattr(events, "load_config", lambda: cfg)
    event_table([dict(symbol="*", date=dt.date(2026, 9, 29), type_="fomc")])
    assert events.check("SPY", START, END, "csp", asset_class="etf").blocks
    event_table([dict(symbol="*", date=dt.date(2026, 9, 28), type_="fomc")])
    assert events.check("SPY", START, END, "csp", asset_class="etf").action == "ok"


def test_earnings_sources_merge_to_the_earlier_date(monkeypatch):
    today = dt.date(2026, 9, 27)
    yf = pd.DataFrame({"ticker": ["TXN", "TXN"],
                       "earnings_date": [dt.date(2026, 7, 21), dt.date(2026, 10, 27)],
                       "time_of_day": ["amc", "amc"]})
    tasty = pd.DataFrame({"symbol": ["TXN"], "earnings_date": [dt.date(2026, 10, 20)],
                          "earnings_estimated": [False]})
    monkeypatch.setattr(ys, "load_earnings", lambda: yf)
    monkeypatch.setattr(tasty_metrics, "latest", lambda symbols=None, **k: tasty)
    rows = events.earnings_events(["TXN"], today)
    nxt = [r for r in rows if r["date"] >= today]
    assert len(nxt) == 1 and nxt[0]["date"] == dt.date(2026, 10, 20)
    assert nxt[0]["sources_disagree"] and nxt[0]["source"] == "yfinance+tasty"
    assert any(r["date"] == dt.date(2026, 7, 21) and r["note"] == "reported" for r in rows)


def test_stale_tasty_earnings_date_is_not_forward(monkeypatch):
    """HRL's tasty expected date was already past on 2026-09-27."""
    today = dt.date(2026, 9, 27)
    monkeypatch.setattr(ys, "load_earnings", lambda: pd.DataFrame(
        columns=["ticker", "earnings_date", "time_of_day"]))
    monkeypatch.setattr(tasty_metrics, "latest", lambda symbols=None, **k: pd.DataFrame(
        {"symbol": ["HRL"], "earnings_date": [dt.date(2026, 8, 27)],
         "earnings_estimated": [False]}))
    assert events.earnings_events(["HRL"], today) == []


@pytest.mark.parametrize("stamp, expected", [
    ("2026-10-29 16:00:00-04:00", "amc"), ("2026-10-29 07:00:00-04:00", "bmo"),
    ("2026-10-29 12:00:00-04:00", "during"), ("2026-10-29 00:00:00-04:00", "unknown")])
def test_earnings_time_of_day(stamp, expected):
    assert ys.earnings_time_of_day(pd.Timestamp(stamp)) == expected


# --- Earnings reactions ------------------------------------------------------------------

def test_amc_report_reacts_the_next_session():
    from analytics import earnings_history
    dates = pd.bdate_range("2026-01-01", periods=40)
    close = np.full(40, 100.0)
    close[21:] = 110.0                         # the jump lands on session 21
    opens = close.copy()
    opens[21] = 108.0
    daily = pd.DataFrame({"date": dates, "open": opens, "high": close + 1,
                          "low": close - 1, "close": close, "volume": 1.0})
    reports = pd.DataFrame({"date": [dates[20].date()], "time_of_day": ["amc"]})
    out = earnings_history.reactions("X", daily=daily, report_dates=reports)
    row = out.iloc[0]
    assert row["reaction_date"] == dates[21].date()
    assert row["close_to_close"] == pytest.approx(0.10)
    assert row["gap"] == pytest.approx(0.08)
    assert row["atr_multiple"] > 0


# --- Pipeline ------------------------------------------------------------------------------

def test_pipeline_has_the_phase9_stages_and_data_only_flag():
    import pipeline.run as run
    keys = [k for k, _ in run.STAGES]
    for stage in ("universe", "metrics", "events", "stage1"):
        assert stage in keys
    assert keys.index("events") < keys.index("chains")
    import inspect
    assert "data_only" in inspect.signature(run.run).parameters


def test_latest_run_skips_data_only_runs(tmp_path, monkeypatch):
    """A nightly --data-only run must not blank the results pages."""
    import json
    import pipeline.results as results
    monkeypatch.setattr(results, "runs_dir", lambda: tmp_path)
    for run_id, stages in (("20260926-100000-aaaa", {"analyse": {"candidates": []}}),
                           ("20260927-020000-bbbb", {"daily": {}, "events": {}})):
        (tmp_path / run_id).mkdir()
        (tmp_path / run_id / "manifest.json").write_text(json.dumps({
            "run_id": run_id, "started_at": "x", "session_block": "b",
            "session_state": "closed", "tickers": 1, "finished_at": "2026-09-27T02:01:00",
            "stages": stages}), encoding="utf-8")
    assert results.latest_run().run_id == "20260926-100000-aaaa"
    assert results.latest_run(with_analysis=False).run_id == "20260927-020000-bbbb"


def test_earnings_pull_uses_the_yahoo_symbol(monkeypatch, tmp_path):
    """BRK.B returned nothing until the vendor mapping was applied."""
    asked = []

    class FakeTicker:
        def __init__(self, symbol):
            asked.append(symbol)

        def get_earnings_dates(self, limit=8):
            return pd.DataFrame()

    class FakeYF:
        Ticker = FakeTicker

    monkeypatch.setattr(ys, "_yf", lambda: FakeYF())
    monkeypatch.setattr(ys, "_vendor_map", lambda t: {"BRK.B": ("BRK-B", 1.0)})
    monkeypatch.setattr(ys, "reference_dir", lambda: tmp_path)
    ys.sync_earnings(["BRK.B"])
    assert asked == ["BRK-B"]


def test_partial_earnings_sync_keeps_other_symbols(monkeypatch, tmp_path):
    """A one-symbol pull used to overwrite the whole earnings file."""
    pd.DataFrame({"ticker": ["AAPL", "KO"], "earnings_date": [dt.date(2026, 10, 29)] * 2,
                  "time_of_day": ["amc", "bmo"]}).to_parquet(tmp_path / ys.EARNINGS_FILE)

    class FakeTicker:
        def __init__(self, symbol):
            pass

        def get_earnings_dates(self, limit=8):
            return pd.DataFrame({"EPS Estimate": [1.0]}, index=pd.DatetimeIndex(
                [pd.Timestamp("2026-11-06 07:00", tz="America/New_York")]))

    class FakeYF:
        Ticker = FakeTicker

    monkeypatch.setattr(ys, "_yf", lambda: FakeYF())
    monkeypatch.setattr(ys, "_vendor_map", lambda t: {s: (s, 1.0) for s in t})
    monkeypatch.setattr(ys, "reference_dir", lambda: tmp_path)
    ys.sync_earnings(["KO"])
    stored = pd.read_parquet(tmp_path / ys.EARNINGS_FILE)
    assert set(stored["ticker"]) == {"AAPL", "KO"}
    assert len(stored[stored["ticker"] == "KO"]) == 1                 # replaced, not appended
