"""Phase 18 -- trade tracking: tracked vs taken, the C.3 sample, dedupe and
observations, promote, the update (marks, probabilities from now,
attribution), auto-expiry with hold and managed outcomes, retention."""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import pytest

from analytics import paper, tracking

PCS = {"strategy": "pcs", "ticker": "SPY", "strike": 700.0, "long_strike": 690.0,
       "expiration": "2026-11-20", "modelled_fill": 2.00, "contracts": 3,
       "bid": 5.00, "ask": 5.20, "mid": 5.10, "long_bid": 3.00, "long_ask": 3.12,
       "long_mid": 3.06, "implied_vol": 0.18, "long_iv": 0.20, "delta": -0.25,
       "long_delta": -0.17, "pop_blend": 0.74, "p_hit_50_blend": 0.62,
       "p_max_loss_blend": 0.08, "headline_policy": "close_50_stop_2x_or_21dte",
       "settlement": "physical", "spot": 740.0, "em": 25.0, "accepted": True,
       "trade_id": "pcs|SPY|2026-11-20|700|690"}
CSP = {"ticker": "F", "strike": 11.0, "expiration": "2026-08-28", "modelled_fill": 0.18,
       "contracts": 20, "bid": 0.17, "ask": 0.20, "mid": 0.185, "spot": 12.0,
       "accepted": True, "trade_id": "csp|F|2026-08-28|11"}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    db = tmp_path / "trade_log.duckdb"
    monkeypatch.setattr(paper, "db_trade_log", lambda: db)
    monkeypatch.setattr(tracking, "market_context", lambda *a, **k: {
        "iv_rank": 0.3, "iv_pct": 0.4, "trend": "range", "rsi": 50.0,
        "days_to_earnings": None, "vix_ratio": 0.8})
    paper.ensure_schema()
    return db


# --- Books ---------------------------------------------------------------------------------

def test_tracked_never_opens_a_cycle_and_is_filtered(ledger):
    tracked = tracking.log([CSP], sample="top")[0]
    taken = paper.accept(dict(CSP, strike=10.5, trade_id=None), actual_fill=0.16)
    assert tracked["action"] == "opened"
    rows = paper.list_positions()
    t = rows[rows["id"] == tracked["position_id"]].iloc[0]
    assert t["book"] == "tracked" and t["sample"] == "top" and pd.isna(t["cycle_id"])
    assert json.loads(t["entry_context"])["spot"] == 12.0
    assert rows[rows["id"] == taken.position_id].iloc[0]["book"] == "taken"
    assert list(paper.list_positions("open", book="taken")["id"]) == [taken.position_id]
    assert list(paper.list_positions("open", book="tracked")["id"]) == [tracked["position_id"]]
    with pytest.raises(ValueError):
        paper.accept(CSP, book="shadow")


def test_existing_rows_migrate_to_taken(tmp_path, monkeypatch):
    import duckdb
    db = tmp_path / "old.duckdb"
    monkeypatch.setattr(paper, "db_trade_log", lambda: db)
    con = duckdb.connect(str(db))
    for statement in paper.SCHEMA:
        con.execute(statement)
    con.execute("INSERT INTO paper_positions (ticker, strategy, strike, expiration, contracts, "
                "modelled_fill, status) VALUES ('F', 'csp', 11, DATE '2026-08-28', 1, 0.2, "
                "'open')")
    con.close()
    frame = paper.list_positions()
    assert frame.iloc[0]["book"] == "taken" and frame.iloc[0]["sample"] == "manual"


def test_dedupe_appends_an_observation(ledger):
    first = tracking.log([PCS], run_id="r1")[0]
    again = tracking.log([dict(PCS, modelled_fill=2.10, rank=3)], run_id="r2")[0]
    assert first["action"] == "opened" and again["action"] == "observed"
    assert again["position_id"] == first["position_id"]
    obs = tracking.observations(first["position_id"])
    assert list(obs["run_id"]) == ["r1", "r2"] and obs["modelled_fill"].iloc[1] == 2.10
    assert len(paper.list_positions("open")) == 1
    # A taken trade with the same key is a different book: no dedupe across books.
    assert tracking.log([PCS], book="taken")[0]["action"] == "opened"


def test_stock_leg_rows_are_skipped_not_raised(ledger):
    row = {"ticker": "SPY", "strategy": "covered_call", "expiration": "2026-11-20",
           "strike": 780.0, "modelled_fill": -700.0, "contracts": 1, "trade_id": "cc|SPY",
           "legs_json": json.dumps([{"option_type": "stock", "side": "long", "strike": 770,
                                     "expiration": "2026-11-20"}])}
    assert tracking.log([row])[0]["action"] == "skipped"


def test_sample_rows_top_control_and_near_miss():
    n = 30
    sheet = pd.DataFrame({
        "trade_id": [f"t{i}" for i in range(n)], "rank_key": list(range(n, 0, -1)),
        "accepted": [i < 20 for i in range(n)],
        "rejections": [() if i < 20 else (("one",) if i % 2 else ("a", "b")) for i in range(n)]})
    picked = tracking.sample_rows(sheet, k=5, m=3, seed="run-1")
    top = picked[picked["sample"] == "top"]
    assert list(top["trade_id"]) == ["t0", "t1", "t2", "t3", "t4"]
    assert list(top["rank"]) == [1, 2, 3, 4, 5]
    control = picked[picked["sample"] == "control"]
    passing = control[control["accepted"]]
    near = control[~control["accepted"]]
    assert len(passing) == 3 and set(passing["trade_id"]) <= {f"t{i}" for i in range(5, 20)}
    assert len(near) == 3 and all(len(r) == 1 for r in near["rejections"])
    again = tracking.sample_rows(sheet, k=5, m=3, seed="run-1")
    assert list(again["trade_id"]) == list(picked["trade_id"])        # reproducible


def test_promote_links_a_taken_trade(ledger):
    tracked = tracking.log([PCS])[0]["position_id"]
    result = tracking.promote(tracked, actual_fill=1.95, contracts=2)
    rows = paper.list_positions().set_index("id")
    assert rows.loc[result.position_id, "book"] == "taken"
    assert rows.loc[result.position_id, "promoted_from"] == tracked
    assert rows.loc[result.position_id, "actual_fill"] == pytest.approx(1.95)
    assert rows.loc[tracked, "status"] == "open"                # the forward test runs on


# --- Update ----------------------------------------------------------------------------------

def _chain(spot_shift: float = 0.0, short_mark: float = 5.0, long_mark: float = 3.0,
           iv: float = 0.18):
    rows = []
    for strike, mark, delta in ((700.0, short_mark, -0.25), (690.0, long_mark, -0.17)):
        rows.append({"expiration": "2026-11-20", "strike_price": strike, "root_symbol": "SPY",
                     "put_bid": mark - 0.05, "put_ask": mark + 0.05, "put_mark": mark,
                     "put_iv": iv, "put_delta": delta, "put_gamma": 0.004,
                     "put_theta": -0.10, "put_vega": 0.9})
    return pd.DataFrame(rows), pd.DataFrame([{"mark": 740.0 + spot_shift}])


def test_update_marks_attribution_and_probabilities(ledger, monkeypatch):
    from data_sources import chains
    pid = tracking.log([PCS])[0]["position_id"]
    state = {"chain": _chain()}
    monkeypatch.setattr(chains, "load_chain", lambda ticker, block=None: state["chain"])
    t0 = dt.datetime(2026, 9, 28, 11, 0)
    first = tracking.update(pull=False, n_paths=500, now=t0)
    assert len(first) == 1 and first.iloc[0]["mark"] == pytest.approx(2.0)
    assert first.iloc[0]["pnl_change"] is None or pd.isna(first.iloc[0]["pnl_change"])
    assert 0 <= first.iloc[0]["p_target_now"] <= 1 and first.iloc[0]["verdict"]
    state["chain"] = _chain(spot_shift=5.0, short_mark=4.4, long_mark=2.7, iv=0.17)
    second = tracking.update(pull=False, n_paths=500, now=t0 + dt.timedelta(hours=1))
    row = second.iloc[0]
    assert row["pnl_change"] == pytest.approx((2.0 - 1.7) * 100 * 3)
    parts = row[["attr_delta", "attr_gamma", "attr_theta", "attr_vega", "attr_residual"]]
    assert parts.sum() == pytest.approx(row["pnl_change"])
    assert row["attr_delta"] > 0                                  # spot up helps a short put spread
    marks = tracking.marks(pid)
    assert len(marks) == 2 and marks["best_pct"].iloc[-1] >= marks["profit_pct"].iloc[-1]
    frame = tracking.entry_vs_now(paper.list_positions().iloc[0].to_dict(),
                                  marks.iloc[-1].to_dict())
    assert {"spot", "POP", "trend"} <= set(frame["measure"])


def test_attribution_arithmetic():
    prev = {"mark": 2.0, "spot": 100.0, "marked_at": "2026-09-28 10:00",
            "legs_json": json.dumps([{"side": "short", "qty": 1, "delta": -0.3, "gamma": 0.02,
                                      "theta": -0.05, "vega": 0.1, "iv": 0.20}])}
    now = {"mark": 1.8, "spot": 101.0, "marked_at": "2026-09-29 10:00",
           "legs": [{"iv": 0.19}]}
    out = tracking.attribution(prev, now, contracts=2)
    scale = -1 * 100 * 2
    assert out["attr_delta"] == pytest.approx(scale * -0.3 * 1.0)
    assert out["attr_gamma"] == pytest.approx(scale * 0.5 * 0.02)
    assert out["attr_theta"] == pytest.approx(scale * -0.05 * 1.0)
    assert out["attr_vega"] == pytest.approx(scale * 0.1 * -1.0)
    assert out["pnl_change"] == pytest.approx(40.0)


# --- Expiry and outcomes ------------------------------------------------------------------

def test_expire_due_records_hold_and_managed(ledger, monkeypatch):
    otm = tracking.log([PCS])[0]["position_id"]
    itm = tracking.log([dict(PCS, strike=760.0, long_strike=750.0,
                             trade_id="pcs|SPY|2026-11-20|760|750")])[0]["position_id"]
    csp = tracking.log([dict(CSP, expiration="2026-11-20", trade_id="csp|F|2026-11-20|11")])[0][
        "position_id"]
    closes = {"SPY": 745.0, "F": 10.0}
    monkeypatch.setattr(tracking, "_close_on", lambda t, d: closes[t])
    # A close verdict on the OTM spread's recorded marks = the managed exit.
    con = paper._connect()
    con.execute("INSERT INTO position_marks (position_id, marked_at, mark, verdict, "
                "verdict_reason) VALUES (?, TIMESTAMP '2026-10-20 11:00', 0.9, 'close', "
                "'profit target reached')", [otm])
    con.close()
    assert tracking.expire_due(today=dt.date(2026, 11, 19)) == []      # nothing due yet
    done = {r["position_id"]: r for r in tracking.expire_due(today=dt.date(2026, 11, 20))}
    rows = paper.list_positions().set_index("id")
    assert rows.loc[otm, "status"] == "expired_otm"
    assert rows.loc[otm, "hold_pnl"] == pytest.approx(2.0 * 300 - rows.loc[otm, "entry_fees"])
    assert rows.loc[otm, "managed_pnl"] < rows.loc[otm, "hold_pnl"]     # closed early at 0.9
    assert "profit target" in rows.loc[otm, "managed_rule"]
    assert rows.loc[itm, "status"] == "settled"
    assert rows.loc[itm, "hold_pnl"] == pytest.approx(rows.loc[itm, "managed_pnl"])
    assert rows.loc[itm, "hold_pnl"] < 0
    assert rows.loc[csp, "status"] == "assigned" and done[csp]["hold_pnl"] < 0
    assert paper.list_share_lots(open_only=False).empty            # tracked: no share lot
    assert len(tracking.closed_outcomes()) == 3


# --- Retention and archive -----------------------------------------------------------------

def test_retention_keeps_manifests_referenced_runs_and_one_block_a_day(tmp_path, monkeypatch):
    from pipeline import retention
    runs, blocks = tmp_path / "runs", tmp_path / "chains"
    for run in ("20260101-100000-aaaa", "20260102-100000-bbbb", "20260927-100000-cccc"):
        (runs / run).mkdir(parents=True)
        for name in ("manifest.json", "candidates.parquet") + retention.PROB_TABLES:
            (runs / run / name).write_text("x")
    for block in ("2026-01-05_rth_10", "2026-01-05_rth_15", "2026-01-05_post",
                  "2026-01-06_closed", "2026-09-25_rth_10", "2026-09-25_rth_11"):
        (blocks / block).mkdir(parents=True)
        (blocks / block / "SPY_chain.parquet").write_text("x")
    monkeypatch.setattr(retention, "runs_dir", lambda: runs)
    monkeypatch.setattr(retention, "chains_dir", lambda: blocks)
    monkeypatch.setattr(retention, "_referenced_runs", lambda: {"20260102-100000-bbbb"})
    planned = retention.plan(today=dt.date(2026, 9, 28))
    assert {p.parent.name for p in planned["run_files"]} == {"20260101-100000-aaaa"}
    assert {p.name for p in planned["chain_blocks"]} == {"2026-01-05_rth_10", "2026-01-05_post"}
    retention.apply(planned)
    assert (runs / "20260101-100000-aaaa" / "manifest.json").exists()
    assert (runs / "20260101-100000-aaaa" / "candidates.parquet").exists()
    assert not (runs / "20260101-100000-aaaa" / "prob_curves.parquet").exists()
    assert (runs / "20260102-100000-bbbb" / "prob_curves.parquet").exists()
    assert sorted(p.name for p in blocks.iterdir()) == [
        "2026-01-05_rth_15", "2026-01-06_closed", "2026-09-25_rth_10", "2026-09-25_rth_11"]


def test_open_book_counts_taken_only(ledger):
    from analytics import book
    tracking.log([PCS])
    assert book.open_book(chain_loader=lambda t: (pd.DataFrame(), None)).empty
    paper.accept(dict(PCS, trade_id=None), actual_fill=2.0)
    frame = book.open_book(chain_loader=lambda t: (pd.DataFrame(), None),
                           daily_loader=lambda *a, **k: pd.DataFrame())
    assert len(frame) == 1
