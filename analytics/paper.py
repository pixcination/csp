"""
The paper book: accept a recommendation, record what you actually filled at.

Signals are generated here; orders are executed in another account. So this is
not a simulation of trades that never happened -- it is a ledger of real
decisions with real fills, entered by hand, whose *recommendation* is preserved
alongside them.

That distinction is the whole point. Every entry stores three prices:

    modelled_fill   what the tool said you should expect (mid, minus a fraction
                    of the half-spread)
    actual_fill     what you really got in the other account
    slippage        the difference

plus the bid/ask/mid the fill was taken against. Without the quote, slippage is
only a dollar figure; with it, the actual slippage *fraction* is recoverable
per trade -- which is the parameter the cost model uses, and the thing that
needs measuring rather than assuming.

Accumulate a few hundred of those and you can calibrate
`costs.slippage_fraction_of_half_spread` against reality instead of guessing
it -- which is the single input most likely to be wrong today, and the one
that most distorts every yield the tool displays.

The schema is wheel-shaped from the start: positions link to cycles, and cycles
own share lots. Nothing here writes an order anywhere. When live execution is
eventually built, it fills in `broker_order_id` and stops depending on
`entered_by = 'manual'`; the ledger does not otherwise change.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import duckdb
import pandas as pd

from analytics import costs
from core.paths import db_trade_log, load_config

STATUSES = ["open", "expired_otm", "closed_early", "rolled", "assigned"]
CYCLE_STATES = ["put_open", "shares_held", "call_open", "closed"]

SCHEMA = [
    "CREATE SEQUENCE IF NOT EXISTS cycle_id_seq START 1",
    "CREATE SEQUENCE IF NOT EXISTS paper_position_id_seq START 1",
    "CREATE SEQUENCE IF NOT EXISTS share_lot_id_seq START 1",
    """
    CREATE TABLE IF NOT EXISTS cycles (
        cycle_id INTEGER PRIMARY KEY DEFAULT nextval('cycle_id_seq'),
        ticker VARCHAR, state VARCHAR, opened_date DATE, closed_date DATE,
        total_premium DOUBLE DEFAULT 0, total_fees DOUBLE DEFAULT 0,
        realized_stock_pnl DOUBLE DEFAULT 0, notes VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_positions (
        id INTEGER PRIMARY KEY DEFAULT nextval('paper_position_id_seq'),
        cycle_id INTEGER,
        ticker VARCHAR, strategy VARCHAR, strike DOUBLE, expiration DATE,
        contracts INTEGER,
        modelled_fill DOUBLE, actual_fill DOUBLE, slippage DOUBLE,
        quote_bid DOUBLE, quote_ask DOUBLE, quote_mid DOUBLE,
        entry_date DATE, entry_fees DOUBLE,
        status VARCHAR, exit_date DATE, exit_price DOUBLE, exit_fees DOUBLE,
        collateral DOUBLE,
        rec_prob_otm DOUBLE, rec_expected_value DOUBLE, rec_ev_annualised DOUBLE,
        rec_iv_rv DOUBLE, rec_sample VARCHAR, rec_rationale VARCHAR,
        entered_by VARCHAR DEFAULT 'manual', broker_order_id VARCHAR,
        run_id VARCHAR, notes VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS share_lots (
        lot_id INTEGER PRIMARY KEY DEFAULT nextval('share_lot_id_seq'),
        cycle_id INTEGER, ticker VARCHAR, shares INTEGER,
        acquired_date DATE, acquisition_price DOUBLE,
        adjusted_basis DOUBLE, source_position_id INTEGER,
        disposed_date DATE, disposal_price DOUBLE
    )
    """,
]


def _connect(read_only: bool = False):
    con = duckdb.connect(str(db_trade_log()), read_only=read_only)
    if not read_only:
        for statement in SCHEMA:
            con.execute(statement)
    return con


def ensure_schema() -> None:
    con = _connect()
    con.close()


# --- Accepting a recommendation -------------------------------------------

@dataclass
class AcceptResult:
    position_id: int
    cycle_id: int
    contracts: int
    actual_fill: float
    modelled_fill: float
    slippage: float
    net_credit: float
    collateral: float
    message: str


def accept(recommendation: dict, contracts: int | None = None,
            actual_fill: float | None = None, entry_date: dt.date | None = None,
            run_id: str | None = None, notes: str = "") -> AcceptResult:
    """Record an accepted trade.

    `actual_fill` is what you really got in the executing account. Leave it None
    and the modelled fill is used, but the row is still marked so calibration
    can exclude it -- an assumed fill is not evidence about slippage.

    `contracts` overrides the recommended size; you may have taken less because
    the fill dried up, or more because you disagreed with the cap. Either way,
    what is recorded is what happened.
    """
    cfg = load_config().get("execution", {})
    if not cfg.get("allow_manual_fill_override", True) and actual_fill is not None:
        raise ValueError("manual fill override is disabled in config")

    ticker = str(recommendation["ticker"]).upper()
    strike = float(recommendation["strike"])
    expiration = pd.Timestamp(recommendation["expiration"]).date()
    modelled = float(recommendation["modelled_fill"])
    size = int(contracts if contracts is not None else recommendation["contracts"])
    if size < 1:
        raise ValueError("contracts must be at least 1")

    fill = float(actual_fill) if actual_fill is not None else modelled
    slippage = fill - modelled
    entry_date = entry_date or dt.date.today()
    entry_fees = costs.option_open(size, "sell").total
    collateral = strike * 100.0 * size
    net_credit = fill * 100.0 * size - entry_fees

    con = _connect()
    try:
        cycle_id = con.execute(
            "INSERT INTO cycles (ticker, state, opened_date, total_premium, total_fees) "
            "VALUES (?, 'put_open', ?, ?, ?) RETURNING cycle_id",
            [ticker, entry_date, fill * 100.0 * size, entry_fees]).fetchone()[0]

        position_id = con.execute(
            """INSERT INTO paper_positions
               (cycle_id, ticker, strategy, strike, expiration, contracts,
                modelled_fill, actual_fill, slippage, quote_bid, quote_ask, quote_mid,
                entry_date, entry_fees,
                status, collateral, rec_prob_otm, rec_expected_value,
                rec_ev_annualised, rec_iv_rv, rec_sample, rec_rationale,
                entered_by, run_id, notes)
               VALUES (?, ?, 'csp', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?) RETURNING id""",
            [cycle_id, ticker, strike, expiration, size, modelled,
             fill if actual_fill is not None else None, slippage,
             recommendation.get("bid"), recommendation.get("ask"),
             recommendation.get("mid"),
             entry_date, entry_fees,
             collateral, recommendation.get("prob_otm_empirical"),
             recommendation.get("expected_value"),
             recommendation.get("ev_annualised"), recommendation.get("iv_rv_ratio"),
             recommendation.get("sample_label"), recommendation.get("rationale"),
             "manual", run_id, notes]).fetchone()[0]
    finally:
        con.close()

    note = (f"Recorded {size} {ticker} {expiration} ${strike:g} put at ${fill:.2f}"
            + (f" ({slippage:+.3f} vs modelled ${modelled:.2f})"
               if actual_fill is not None else " (modelled fill -- excluded from "
                                               "slippage calibration)"))
    return AcceptResult(position_id, cycle_id, size, fill, modelled, slippage,
                         net_credit, collateral, note)


# --- Lifecycle -------------------------------------------------------------

def close_position(position_id: int, status: str, exit_date: dt.date | None = None,
                    exit_price: float | None = None, notes: str | None = None) -> None:
    """Mark an outcome. `exit_price` is premium paid to close, 0 for expiry."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    exit_date = exit_date or dt.date.today()

    con = _connect()
    try:
        row = con.execute(
            "SELECT cycle_id, ticker, contracts, strike, actual_fill, modelled_fill "
            "FROM paper_positions WHERE id = ?", [position_id]).fetchone()
        if row is None:
            raise ValueError(f"no paper position {position_id}")
        cycle_id, ticker, contracts, strike, actual, modelled = row
        entry_fill = actual if actual is not None else modelled

        if status == "expired_otm":
            exit_fees, exit_price = costs.option_expire(contracts).total, 0.0
        elif status == "assigned":
            exit_fees, exit_price = costs.assignment(contracts).total, 0.0
        else:
            exit_fees = costs.option_close(contracts, "buy").total
            exit_price = float(exit_price or 0.0)

        con.execute(
            "UPDATE paper_positions SET status = ?, exit_date = ?, exit_price = ?, "
            "exit_fees = ?, notes = COALESCE(?, notes) WHERE id = ?",
            [status, exit_date, exit_price, exit_fees, notes, position_id])

        if status == "assigned":
            # Assignment is a capital event, not just a P&L event: cash becomes
            # shares whose basis is the strike less every premium collected
            # against the cycle so far.
            basis = float(strike) - float(entry_fill)
            con.execute(
                "INSERT INTO share_lots (cycle_id, ticker, shares, acquired_date, "
                "acquisition_price, adjusted_basis, source_position_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [cycle_id, ticker, 100 * contracts, exit_date, float(strike),
                 basis, position_id])
            con.execute("UPDATE cycles SET state = 'shares_held' WHERE cycle_id = ?",
                         [cycle_id])
        elif status in ("expired_otm", "closed_early"):
            con.execute(
                "UPDATE cycles SET state = 'closed', closed_date = ? WHERE cycle_id = ?",
                [exit_date, cycle_id])
        con.execute(
            "UPDATE cycles SET total_fees = total_fees + ? WHERE cycle_id = ?",
            [exit_fees, cycle_id])
    finally:
        con.close()


def list_positions(status: str | None = None) -> pd.DataFrame:
    con = _connect()
    try:
        query = "SELECT * FROM paper_positions"
        params: list = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY entry_date DESC, id DESC"
        return con.execute(query, params).fetchdf()
    finally:
        con.close()


def list_share_lots(open_only: bool = True) -> pd.DataFrame:
    con = _connect()
    try:
        query = "SELECT * FROM share_lots"
        if open_only:
            query += " WHERE disposed_date IS NULL"
        return con.execute(query + " ORDER BY acquired_date DESC").fetchdf()
    finally:
        con.close()


# --- Calibration -----------------------------------------------------------

def slippage_report() -> dict:
    """How wrong is the modelled fill?

    Only rows with a real recorded fill count -- an assumed fill would just
    report zero slippage and quietly confirm the assumption.
    """
    frame = list_positions()
    if frame.empty:
        return {"n": 0, "note": "no positions recorded yet"}
    real = frame[frame["actual_fill"].notna()]
    if real.empty:
        return {"n": 0, "note": "no positions with a recorded actual fill -- enter "
                                "real fills to calibrate the slippage assumption"}

    slip = real["slippage"].astype(float)
    modelled = real["modelled_fill"].astype(float)
    relative = (slip / modelled.replace(0, float("nan"))).dropna()
    cfg = load_config().get("costs", {})
    assumed = cfg.get("slippage_fraction_of_half_spread", 0.40)

    return {
        "n": int(len(real)),
        "mean_slippage": float(slip.mean()),
        "median_slippage": float(slip.median()),
        "worst_slippage": float(slip.min()),
        "mean_relative": float(relative.mean()) if len(relative) else float("nan"),
        "assumed_fraction": assumed,
        "note": ("modelled fills are optimistic -- real fills came in below the model"
                 if slip.mean() < -0.005 else
                 "modelled fills are pessimistic -- real fills beat the model"
                 if slip.mean() > 0.005 else
                 "modelled fills track reality closely"),
    }


def performance() -> dict:
    """Realised outcomes over closed positions, net of every fee."""
    frame = list_positions()
    if frame.empty:
        return {"n_closed": 0}
    closed = frame[frame["status"] != "open"].copy()
    if closed.empty:
        return {"n_closed": 0, "n_open": int(len(frame))}

    fill = closed["actual_fill"].fillna(closed["modelled_fill"]).astype(float)
    exit_price = closed["exit_price"].fillna(0.0).astype(float)
    fees = (closed["entry_fees"].fillna(0.0) + closed["exit_fees"].fillna(0.0)).astype(float)
    contracts = closed["contracts"].astype(float)
    closed["realized"] = (fill - exit_price) * 100 * contracts - fees

    days = (pd.to_datetime(closed["exit_date"]) - pd.to_datetime(closed["entry_date"])
            ).dt.days.clip(lower=1)
    collateral = closed["collateral"].astype(float).replace(0, float("nan"))
    closed["annualised"] = (closed["realized"] / collateral) * (365.0 / days)

    return {
        "n_closed": int(len(closed)),
        "n_open": int((frame["status"] == "open").sum()),
        "win_rate": float((closed["status"] == "expired_otm").mean()),
        "assignment_rate": float((closed["status"] == "assigned").mean()),
        "total_realized": float(closed["realized"].sum()),
        "mean_annualised": float(closed["annualised"].mean()),
        "predicted_win_rate": float(closed["rec_prob_otm"].dropna().mean())
        if closed["rec_prob_otm"].notna().any() else float("nan"),
    }


def calibration() -> dict:
    """Did the empirical probabilities actually hold up?

    The question that decides whether any of this works. If the engine says 85%
    and you keep getting 70%, the sampling window or the volatility conditioning
    is wrong -- and you would never find out from P&L alone.
    """
    stats = performance()
    if stats.get("n_closed", 0) < 10:
        return {**stats, "verdict": "not enough closed positions to judge "
                                     "(need ~10, ideally 30+)"}
    predicted = stats.get("predicted_win_rate")
    actual = stats.get("win_rate")
    if not (predicted == predicted):
        return {**stats, "verdict": "no stored predictions to compare against"}
    gap = actual - predicted
    if abs(gap) <= 0.05:
        verdict = f"well calibrated -- predicted {predicted:.0%}, realised {actual:.0%}"
    elif gap < 0:
        verdict = (f"OPTIMISTIC -- predicted {predicted:.0%}, realised {actual:.0%}. "
                   f"The empirical sample is not matching live outcomes; suspect the "
                   f"volatility conditioning window or too short a lookback.")
    else:
        verdict = (f"conservative -- predicted {predicted:.0%}, realised {actual:.0%}. "
                   f"You may be leaving premium on the table at this delta.")
    return {**stats, "calibration_gap": gap, "verdict": verdict}
