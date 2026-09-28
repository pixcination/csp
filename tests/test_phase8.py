"""
Phase 8: raw daily bars and the price basis, persisted run results, the
Windows-safe run lock, freshness checks, and the legacy retirement.
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import data_sources.yfinance_sync as ys  # noqa: E402


# --- Fixtures --------------------------------------------------------------

def _raw_frame(ticker="DIVCO", n=300, ex_index=200, dividend=2.0, start=100.0,
               drift=0.0, seed=0) -> pd.DataFrame:
    """Split-adjusted raw bars with one ex-dividend drop the market really had."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-02", periods=n)
    returns = rng.normal(drift, 0.004, n)
    close = start * np.exp(np.cumsum(returns))
    close[ex_index:] -= dividend               # the ex-date drop, and after
    dividends = np.zeros(n)
    dividends[ex_index] = dividend
    frame = pd.DataFrame({
        "ticker": ticker, "date": dates.date,
        "open": close, "high": close * 1.002, "low": close * 0.998, "close": close,
        "volume": 1e6, "dividends": dividends, "splits": 0.0})
    # Yahoo's adj_close, built the same backward way.
    factor = ys.total_return_factor(frame)
    frame["adj_close"] = frame["close"] * factor
    return frame[ys.RAW_COLUMNS]


@pytest.fixture
def daily_db(tmp_path, monkeypatch):
    path = tmp_path / "universe_daily.duckdb"
    monkeypatch.setattr(ys, "db_universe_daily", lambda: path)
    con = duckdb.connect(str(path))
    con.execute(ys.RAW_SCHEMA)
    con.close()
    return path


def _insert(path, frame):
    con = duckdb.connect(str(path))
    con.register("f", frame)
    con.execute(f"INSERT INTO {ys.RAW_TABLE} SELECT {', '.join(ys.RAW_COLUMNS)} FROM f")
    con.close()


# --- Price basis vs total basis --------------------------------------------

def test_ex_dividend_drop_is_in_price_basis_and_not_in_total(daily_db):
    raw = _raw_frame(dividend=3.0)
    _insert(daily_db, raw)
    price = ys.load_daily("DIVCO", basis="price")
    total = ys.load_daily("DIVCO", basis="total")
    assert price.attrs["price_basis"] == "price"
    assert total.attrs["price_basis"] == "total"

    ex = 200
    price_ret = price["close"].iloc[ex] / price["close"].iloc[ex - 1] - 1
    total_ret = total["close"].iloc[ex] / total["close"].iloc[ex - 1] - 1
    expected_drop = -3.0 / price["close"].iloc[ex - 1]
    assert price_ret < expected_drop * 0.5          # the drop is there (~ -3%)
    assert abs(total_ret) < 0.02                    # and gone on total basis
    # CRSP identity: the ex-date total return is the close against the
    # prior close net of the dividend.
    c_prev, c_ex = price["close"].iloc[ex - 1], price["close"].iloc[ex]
    assert total_ret == pytest.approx(c_ex / (c_prev - 3.0) - 1, abs=1e-12)


def test_total_basis_is_anchored_at_the_latest_bar(daily_db):
    _insert(daily_db, _raw_frame())
    price = ys.load_daily("DIVCO", basis="price")
    total = ys.load_daily("DIVCO", basis="total")
    assert total["close"].iloc[-1] == pytest.approx(price["close"].iloc[-1])
    assert total["close"].iloc[0] < price["close"].iloc[0]


def test_breach_probability_is_higher_on_the_price_basis(daily_db):
    """The whole point of §A.3.2: dividend-adjusting erases the drop, which
    understates P(breach) over windows that span an ex-date."""
    from analytics.moves import breach_probabilities
    frames = []
    base = _raw_frame(n=1500, ex_index=10, dividend=0.0)
    # quarterly ex-dates paying 1.5% each
    close = base["close"].to_numpy().copy()
    divs = np.zeros(len(close))
    for i in range(60, len(close), 63):
        d = close[i - 1] * 0.015
        close[i:] -= d
        divs[i] = d
    base["close"] = close
    base["open"], base["high"], base["low"] = close, close * 1.002, close * 0.998
    base["dividends"] = divs
    _insert(daily_db, base)
    price = ys.load_daily("DIVCO", basis="price")
    total = ys.load_daily("DIVCO", basis="total")
    spot = float(price["close"].iloc[-1])
    kw = dict(spot=spot, strike=spot * 0.98, horizon=21, vol_conditioned=False,
              min_observations=30)
    p = breach_probabilities(price, "DIVCO", **kw).prob_terminal
    t = breach_probabilities(total, "DIVCO", **kw).prob_terminal
    assert p > t


def test_total_return_wrapper_matches_load_daily(daily_db):
    _insert(daily_db, _raw_frame())
    a = ys.load_daily_total_return("DIVCO")
    b = ys.load_daily("DIVCO", basis="total")
    pd.testing.assert_frame_equal(a, b)


def test_unknown_basis_is_rejected():
    with pytest.raises(ValueError):
        ys.load_daily("SPY", basis="adjusted")


def test_start_filter_does_not_change_total_values(daily_db):
    _insert(daily_db, _raw_frame())
    full = ys.load_daily("DIVCO", basis="total").set_index("date")["close"]
    tail = ys.load_daily("DIVCO", basis="total", start="2024-06-01").set_index("date")["close"]
    pd.testing.assert_series_equal(full.loc[tail.index], tail)


def test_adjustment_check_agrees_with_consistent_adj_close(daily_db):
    _insert(daily_db, _raw_frame())
    check = ys.adjustment_check("DIVCO")
    assert check["max_deviation"] < 1e-9


def test_fallback_to_legacy_split_adjusted_table_is_labelled(daily_db):
    con = duckdb.connect(str(daily_db))
    con.execute("CREATE TABLE daily_bars (ticker VARCHAR, date DATE, open DOUBLE, "
                "high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT, bar_count INTEGER)")
    con.execute("INSERT INTO daily_bars VALUES ('OLD', DATE '2024-01-02', 1, 1, 1, 1, 10, 1)")
    con.close()
    frame = ys.load_daily("OLD")
    assert frame.attrs["price_basis"] == "split_adjusted_legacy"
    assert ys.load_daily("OLD", allow_fallback=False).empty


# --- Incremental sync: the adjustment-vintage bug ---------------------------

def _yahoo(frame: pd.DataFrame) -> pd.DataFrame:
    """RAW_COLUMNS -> what yfinance.history(auto_adjust=False) returns."""
    out = frame.rename(columns={"open": "Open", "high": "High", "low": "Low",
                                "close": "Close", "adj_close": "Adj Close",
                                "volume": "Volume", "dividends": "Dividends",
                                "splits": "Stock Splits"})
    out["Date"] = pd.to_datetime(out["date"])
    return out.drop(columns=["ticker", "date"]).set_index("Date")


class _FakeYF:
    """Stands in for yfinance.download (Phase 9 batches the sync)."""

    def __init__(self, frame):
        self.frame = frame
        self.calls = []

    def download(self, symbols, start=None, period=None, **_):
        self.calls.append("full" if period == "max" else f"since {start}")
        f = self.frame
        if start:
            f = f[pd.to_datetime(f["date"]) >= pd.Timestamp(start)]
        yahoo = _yahoo(f)
        return pd.concat({s: yahoo for s in symbols}, axis=1)


@pytest.fixture(autouse=True)
def _identity_vendor_map(monkeypatch):
    monkeypatch.setattr(ys, "_vendor_map", lambda tickers: {t: (t, 1.0) for t in tickers})


def test_repull_reason_detects_every_invalidating_change():
    stored = _raw_frame(n=50, ex_index=10, dividend=0.5)
    base = stored.tail(5).copy()
    assert ys.repull_reason(stored, base) is None

    new_div = pd.concat([base, base.tail(1).assign(
        date=stored["date"].max() + dt.timedelta(days=3), dividends=0.4)])
    assert ys.repull_reason(stored, new_div) == "new dividend"

    new_split = pd.concat([base, base.tail(1).assign(
        date=stored["date"].max() + dt.timedelta(days=3), splits=2.0)])
    assert ys.repull_reason(stored, new_split) == "new split"

    restated = base.assign(close=base["close"] * 1.01)
    assert ys.repull_reason(stored, restated) == "history restated"

    late = base.copy()
    late.loc[late.index[0], "dividends"] = 0.3
    assert ys.repull_reason(stored, late) == "late-posted dividend"


def test_new_dividend_triggers_a_full_repull_and_leaves_no_seam(daily_db, monkeypatch):
    history = _raw_frame(n=260, ex_index=100, dividend=1.0)
    first = history.iloc[:250]
    _insert(daily_db, first)

    # Ten days later Yahoo has a new dividend at row 255.
    later = _raw_frame(n=260, ex_index=100, dividend=1.0)
    later.loc[255:, "close"] -= 1.5
    later.loc[255, "dividends"] = 1.5
    later["adj_close"] = later["close"] * ys.total_return_factor(later)
    fake = _FakeYF(later)
    monkeypatch.setattr(ys, "_yf", lambda: fake)
    monkeypatch.setattr(ys, "previous_trading_day", lambda d: dt.date(2099, 1, 1))

    [result] = ys.sync_daily(["DIVCO"])
    assert result.error is None
    assert result.full_repull == "new dividend"
    assert fake.calls[-1] == "full"

    total = ys.load_daily("DIVCO", basis="total")
    assert len(total) == 260
    # Every stored bar is on one adjustment vintage: daily total returns are
    # smooth across the old/new boundary (no step where the tail was appended).
    returns = total["close"].pct_change().abs()
    assert returns.max() < 0.03


def test_quiet_incremental_sync_does_not_repull(daily_db, monkeypatch):
    history = _raw_frame(n=260, ex_index=100, dividend=1.0)
    _insert(daily_db, history.iloc[:250])
    fake = _FakeYF(history)
    monkeypatch.setattr(ys, "_yf", lambda: fake)
    monkeypatch.setattr(ys, "previous_trading_day", lambda d: dt.date(2099, 1, 1))
    [result] = ys.sync_daily(["DIVCO"])
    assert result.full_repull is None
    assert result.rows_added == 10
    assert all(call.startswith("since") for call in fake.calls)


# --- Which modules use which basis ------------------------------------------

PRICE_BASIS = ["analytics/candidates.py", "analytics/covered_call.py",
               "analytics/roll_engine.py", "analytics/gaps.py",
               "analytics/portfolio.py", "pipeline/run.py"]
TOTAL_BASIS = ["analytics/regimes.py", "analytics/walkforward.py",
               "app/pages/2_Wheel.py", "scripts/sweep_universe.py"]


def _load_daily_bases(path: str) -> set[str]:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    bases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) in (
                "load_daily", "load_daily_total_return"):
            if node.func.id == "load_daily_total_return":
                bases.add("total")
                continue
            kw = {k.arg: k.value for k in node.keywords}
            bases.add(kw["basis"].value if "basis" in kw else "price")
    return bases


@pytest.mark.parametrize("path", PRICE_BASIS)
def test_probability_and_strike_modules_use_the_price_basis(path):
    assert _load_daily_bases(path) == {"price"}


def _loads_dividends(path: str) -> bool:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and getattr(node.func, "id", None) == "load_daily"]
    return bool(calls) and all(
        any(k.arg == "with_dividends" and getattr(k.value, "value", False) for k in c.keywords)
        for c in calls)


@pytest.mark.parametrize("path", TOTAL_BASIS)
def test_long_run_performance_modules_use_price_basis_with_dividends(path):
    """Phase 8 put the wheel on the total basis and deferred the right model to
    Phase 15: traded prices for the strikes, plus each dividend as cash while
    shares are held (`wheel_backtest.run_wheel`)."""
    assert _load_daily_bases(path) == {"price"}
    assert _loads_dividends(path)


# --- Persisted run results --------------------------------------------------

@pytest.fixture
def runs(tmp_path, monkeypatch):
    import pipeline.results as results
    monkeypatch.setattr(results, "runs_dir", lambda: tmp_path)
    return tmp_path


def _manifest(folder: Path, run_id: str, finished=True, analyse=None):
    (folder / run_id).mkdir(parents=True, exist_ok=True)
    data = {"run_id": run_id, "started_at": "2026-09-25T10:00:00",
            "session_block": "2026-09-25_rth", "session_state": "regular",
            "tickers": 2, "finished_at": "2026-09-25T10:05:00" if finished else None,
            "elapsed_seconds": 300.0, "stages": {"analyse": analyse or {}},
            "warnings": [], "banner": ""}
    (folder / run_id / "manifest.json").write_text(json.dumps(data), encoding="utf-8")


def _sheet():
    return pd.DataFrame({
        "ticker": ["AAA", "AAA", "BBB"],
        "expiration": [dt.date(2026, 10, 2)] * 3,
        "strike": [50.0, 48.0, 20.0],
        "ev_annualised": [0.2, 0.1, -0.1],
        "accepted": [True, True, False],
        "rejections": [(), (), ("expected value is negative",)],
        "warnings": [(), ("thin",), ()],
    })


def test_full_sheet_round_trips_with_rejections(runs):
    from pipeline import results
    _manifest(runs, "20260925-100000-aaaa")
    selected = _sheet().iloc[[0]]
    annotated = results.annotate_sheet(_sheet(), selected, selected)
    results.write_tables("20260925-100000-aaaa", annotated,
                         [{"position_id": 1, "action": "hold"}])

    loaded = results.load_run("20260925-100000-aaaa")
    assert loaded.has_full_sheet
    assert len(loaded.candidates) == 3                      # rejected kept
    assert list(loaded.candidates["rejections"].iloc[2]) == ["expected value is negative"]
    assert loaded.candidates["selected"].tolist() == [True, False, False]
    assert loaded.candidates["proposed"].tolist() == [True, False, False]
    assert loaded.positions["action"].tolist() == ["hold"]


def test_latest_run_skips_unfinished_runs(runs):
    from pipeline import results
    _manifest(runs, "20260925-100000-aaaa")
    _manifest(runs, "20260926-100000-bbbb", finished=False)   # crashed / in progress
    assert results.latest_run().run_id == "20260925-100000-aaaa"
    assert results.latest_run(finished_only=False).run_id == "20260926-100000-bbbb"


def test_pre_phase8_run_falls_back_to_manifest_proposals(runs):
    from pipeline import results
    _manifest(runs, "20260822-234040-8351",
              analyse={"candidates": [{"ticker": "F", "strike": 11.0,
                                       "expiration": "2026-08-28"}]})
    loaded = results.latest_run()
    assert not loaded.has_full_sheet
    assert loaded.candidates["ticker"].tolist() == ["F"]
    assert "run 20260822-234040-8351" in loaded.label


def test_no_runs_means_none(runs):
    from pipeline import results
    assert results.latest_run() is None


def test_decisions_page_shows_the_run_from_disk_after_a_restart(runs, monkeypatch):
    """Acceptance: restart -> Decisions still shows the last run."""
    from streamlit.testing.v1 import AppTest
    from pipeline import results
    _manifest(runs, "20260925-100000-aaaa", analyse={"candidates": [], "candidates_considered": 0})
    results.write_tables("20260925-100000-aaaa",
                         results.annotate_sheet(_sheet(), _sheet().iloc[[0]], _sheet().iloc[[0]]),
                         [])
    at = AppTest.from_file(str(ROOT / "app" / "pages" / "1_Decisions.py"), default_timeout=120)
    at.run()
    assert not at.exception
    captions = " ".join(c.value for c in at.caption)
    assert "20260925-100000-aaaa" in captions
    assert "latest run on disk" in captions


# --- Run lock ----------------------------------------------------------------

def test_liveness_check_does_not_kill_the_process_it_checks():
    """os.kill(pid, 0) on Windows is TerminateProcess -- it killed live runs."""
    from pipeline.run import _pid_alive
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        time.sleep(0.3)
        assert _pid_alive(child.pid)
        time.sleep(0.3)
        assert child.poll() is None, "the liveness check terminated the process"
    finally:
        child.kill()
        child.wait()
    assert not _pid_alive(child.pid)
    assert _pid_alive(os.getpid())


# --- Freshness ---------------------------------------------------------------

def test_last_completed_session_waits_for_the_close():
    from core.freshness import last_completed_session
    from core.market_calendar import ET
    friday_noon = dt.datetime(2026, 9, 25, 12, 0, tzinfo=ET)
    friday_eve = dt.datetime(2026, 9, 25, 17, 0, tzinfo=ET)
    sunday = dt.datetime(2026, 9, 27, 12, 0, tzinfo=ET)
    assert last_completed_session(friday_noon) == dt.date(2026, 9, 24)
    assert last_completed_session(friday_eve) == dt.date(2026, 9, 25)
    assert last_completed_session(sunday) == dt.date(2026, 9, 25)


def test_stale_daily_bars_warn(daily_db, monkeypatch):
    from core import freshness
    monkeypatch.setattr(freshness, "db_universe_daily", lambda: daily_db)
    _insert(daily_db, _raw_frame(n=20, ex_index=5))       # ends Jan 2024
    item = freshness._daily_bars(freshness.thresholds())
    assert item.status == freshness.WARN
    assert "behind" in item.age


def test_freshness_report_never_raises(monkeypatch, tmp_path):
    from core import freshness
    monkeypatch.setattr(freshness, "db_universe_daily", lambda: tmp_path / "none.duckdb")
    monkeypatch.setattr(freshness, "db_1m_cache", lambda: tmp_path / "none2.duckdb")
    monkeypatch.setattr(freshness, "db_universe", lambda: tmp_path / "none3.duckdb")
    monkeypatch.setattr(freshness, "db_technicals", lambda: tmp_path / "none4.duckdb")
    monkeypatch.setattr(freshness, "reference_dir", lambda: tmp_path)
    monkeypatch.setattr(freshness, "chains_dir", lambda: tmp_path / "chains")
    items = freshness.report()
    assert items and all(i.status == freshness.MISSING for i in items)


# --- Legacy retirement -------------------------------------------------------

def test_app_has_no_legacy_pages():
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    for page in ("1_Scanner.py", "2_Ticker_Detail.py", "3_Trade_Log.py"):
        assert page not in main
        assert not (ROOT / "app" / "pages" / page).exists()


def test_nothing_live_imports_the_legacy_stack():
    offenders = []
    for folder in ("analytics", "app", "core", "data_sources", "pipeline"):
        for path in (ROOT / folder).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                elif isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                for name in names:
                    if name.startswith("legacy") or name in (
                            "analytics.scoring", "analytics.backtest",
                            "analytics.trade_log", "analytics.data_access"):
                        offenders.append(f"{path.relative_to(ROOT)}: {name}")
    assert not offenders, offenders


def test_legacy_trade_log_migration_is_idempotent(tmp_path, monkeypatch):
    from analytics import paper
    db = tmp_path / "trade_log.duckdb"
    monkeypatch.setattr(paper, "db_trade_log", lambda: db)
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE positions (id INTEGER, ticker VARCHAR, strategy VARCHAR, "
                "strike DOUBLE, expiration DATE, contracts INTEGER, "
                "premium_collected DOUBLE, commission DOUBLE, entry_date DATE, "
                "status VARCHAR, exit_date DATE, exit_price DOUBLE, notes VARCHAR)")
    con.execute("INSERT INTO positions VALUES (7, 'f', 'csp', 11, DATE '2026-10-02', 2, "
                "0.25, 2.24, DATE '2026-09-25', 'open', NULL, NULL, 'hand entry')")
    con.close()
    assert paper.migrate_legacy_trade_log() == 1
    assert paper.migrate_legacy_trade_log() == 0
    book = paper.list_positions(status="open")
    assert len(book) == 1
    row = book.iloc[0]
    assert row["ticker"] == "F" and row["actual_fill"] == 0.25
    assert row["collateral"] == 11 * 100 * 2
    assert "legacy trade_log id 7" in row["notes"]


def test_iv_history_reads_legacy_stage3_layout(tmp_path, monkeypatch):
    from analytics import iv_history
    folder = tmp_path / "2026-07-10"
    folder.mkdir()
    pd.DataFrame({"strike_price": [10.0]}).to_parquet(folder / "XYZ_full_chain_101500.parquet")
    pd.DataFrame({"mark": [11.0]}).to_parquet(folder / "XYZ_underlying_101500.parquet")
    monkeypatch.setattr(iv_history, "_stage3_root", lambda: tmp_path)
    assert iv_history._stage3_dates("XYZ") == ["2026-07-10"]
    chain, under = iv_history._load_stage3("XYZ", "2026-07-10")
    assert len(chain) == 1 and float(under["mark"].iloc[0]) == 11.0


def test_dividend_file_is_derived_from_raw_bars(daily_db, tmp_path, monkeypatch):
    """Nothing called sync_dividends, so the ex-dividend warning froze."""
    monkeypatch.setattr(ys, "reference_dir", lambda: tmp_path)
    raw = _raw_frame(n=300, ex_index=50, dividend=0.5)
    raw.loc[[120, 180, 240], "dividends"] = 0.5
    _insert(daily_db, raw)
    frame = ys.write_dividends_from_raw(per_ticker=3)
    assert len(frame) == 3                                   # most recent three
    assert (tmp_path / ys.DIVIDENDS_FILE).exists()
    assert list(frame["symbol"].unique()) == ["DIVCO"]
    assert frame["ex_date"].max() == raw["date"].iloc[240]
