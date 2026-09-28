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

MULTI-LEG (Phase 15)
--------------------
`paper_positions` is the position (package) table: one row per trade, its
net credit, size, buying power and the recommendation it came from. `strike`
is the SHORT strike, `long_strike` / `width` are set for a spread, and
`collateral` is the buying-power reduction (strike x 100 for a cash-secured
put, max loss for a defined-risk spread). Each position owns its legs in
`paper_legs` -- the single-leg rows recorded before Phase 15 are migrated to
one short-put leg each, idempotently, on every connect.

Two more tables feed calibration:

    paper_predictions   what the engine claimed at entry (POP, P(reach X%),
                        P(max loss)...), one row per metric and model
    paper_marks         marks taken while the trade is open (the pipeline
                        records one per run; you can add your own), so
                        "did it reach 50% of max profit?" has an answer

TRACKED AND TAKEN (Phase 18)
----------------------------
`book` separates a real trade (`taken`: your fill, counted by capacity,
exposure and correlation limits) from a forward test of a recommendation
(`tracked`: logged at the modelled fill, never counted against the account,
never opening a wheel cycle or a share lot). `sample` says why a tracked row
was logged (`top` of a ranking, a random `control`, or `manual`), which is
what lets the accuracy log measure the model rather than the choices.
`promoted_from` links a taken trade to the tracked row it started as.
Tracking itself (observations, hourly marks, attribution, outcomes) lives in
`analytics/tracking.py`.

The schema stays wheel-shaped: CSPs link to cycles, and cycles own share lots.
A spread does not open a cycle at entry; one that is physically assigned
(short leg in the money, long leg not) starts one and becomes a share lot,
just as an assigned CSP does. Nothing here writes an order anywhere. When live execution is
eventually built, it fills in `broker_order_id` and stops depending on
`entered_by = 'manual'`; the ledger does not otherwise change.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass

import duckdb
import pandas as pd

from analytics import costs
from core.paths import db_trade_log, load_config

BOOKS = ("taken", "tracked")
SAMPLES = ("top", "control", "manual")
STATUSES = ["open", "expired_otm", "closed_early", "rolled", "assigned", "settled"]
CYCLE_STATES = ["put_open", "shares_held", "call_open", "closed"]
STRATEGIES = ("csp", "pcs")
# Outcomes that observe the whole path to expiry: a P(reach X%) prediction can
# be scored as a miss only on these. A position closed early without reaching
# X never showed what the rest of the path would have done.
HELD_TO_EXPIRY = {"expired_otm", "assigned", "settled"}

SCHEMA = [
    "CREATE SEQUENCE IF NOT EXISTS cycle_id_seq START 1",
    "CREATE SEQUENCE IF NOT EXISTS paper_position_id_seq START 1",
    "CREATE SEQUENCE IF NOT EXISTS share_lot_id_seq START 1",
    "CREATE SEQUENCE IF NOT EXISTS paper_leg_id_seq START 1",
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
    # --- Phase 15 ---
    """
    CREATE TABLE IF NOT EXISTS paper_legs (
        leg_id INTEGER PRIMARY KEY DEFAULT nextval('paper_leg_id_seq'),
        position_id INTEGER, leg_index INTEGER,
        option_type VARCHAR, side VARCHAR, strike DOUBLE, expiration DATE,
        qty INTEGER,
        entry_price DOUBLE, modelled_price DOUBLE,
        quote_bid DOUBLE, quote_ask DOUBLE, quote_mid DOUBLE,
        iv DOUBLE, delta DOUBLE, exit_price DOUBLE, root_symbol VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_marks (
        position_id INTEGER, mark_date DATE, spot DOUBLE, mark DOUBLE,
        profit_pct DOUBLE, source VARCHAR, recorded_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_predictions (
        position_id INTEGER, metric VARCHAR, model VARCHAR, value DOUBLE
    )
    """,
]

# Columns added to paper_positions in Phase 15 (existing books are altered).
POSITION_COLUMNS = {
    "long_strike": "DOUBLE", "width": "DOUBLE", "max_loss": "DOUBLE",
    "settlement_type": "VARCHAR", "root_symbol": "VARCHAR",
    "quote_net_mid": "DOUBLE", "quote_half_spread": "DOUBLE",
    "rec_pop": "DOUBLE", "rec_headline_policy": "VARCHAR",
    "rolled_from": "INTEGER", "rolls_used": "INTEGER",
    # Phase 16: positions from strategy specs (condors, calendars, ...)
    "max_profit_share": "DOUBLE", "legs_label": "VARCHAR",
    "max_profit_pct_seen": "DOUBLE", "settlement_price": "DOUBLE",
    # Phase 18: tracked vs taken, the sample, the entry context and outcomes
    "book": "VARCHAR", "sample": "VARCHAR", "promoted_from": "INTEGER",
    "dedupe_key": "VARCHAR", "preset": "VARCHAR", "rank_at_log": "INTEGER",
    "trade_id": "VARCHAR", "entry_spot": "DOUBLE", "entry_context": "VARCHAR",
    "source_row": "VARCHAR", "logged_at": "TIMESTAMP",
    "hold_status": "VARCHAR", "hold_pnl": "DOUBLE",
    "managed_pnl": "DOUBLE", "managed_exit_date": "DATE", "managed_rule": "VARCHAR",
}


def _migrate(con) -> None:
    for column, kind in POSITION_COLUMNS.items():
        con.execute(f"ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS {column} {kind}")
    # Phase 18: everything recorded before tracking existed was a real trade.
    con.execute("UPDATE paper_positions SET book = 'taken' WHERE book IS NULL")
    con.execute("UPDATE paper_positions SET sample = 'manual' WHERE sample IS NULL")
    # Single-leg rows from before Phase 15 get their one leg. Idempotent: only
    # positions without any leg are touched.
    con.execute("""
        INSERT INTO paper_legs (position_id, leg_index, option_type, side, strike,
                                expiration, qty, entry_price, modelled_price,
                                quote_bid, quote_ask, quote_mid)
        SELECT p.id, 0,
               CASE WHEN lower(coalesce(p.strategy, '')) LIKE '%call%' THEN 'call'
                    ELSE 'put' END,
               'short', p.strike, p.expiration, 1,
               coalesce(p.actual_fill, p.modelled_fill), p.modelled_fill,
               p.quote_bid, p.quote_ask, p.quote_mid
        FROM paper_positions p
        WHERE coalesce(p.strategy, 'csp') <> 'pcs'
          AND NOT EXISTS (SELECT 1 FROM paper_legs l WHERE l.position_id = p.id)
    """)


def _connect(read_only: bool = False):
    con = duckdb.connect(str(db_trade_log()), read_only=read_only)
    if not read_only:
        for statement in SCHEMA:
            con.execute(statement)
        _migrate(con)
        try:
            from analytics import tracking
            for statement in tracking.SCHEMA:
                con.execute(statement)
        except ImportError:
            pass
    return con


def ensure_schema() -> None:
    con = _connect()
    con.close()


def migrate_legacy_trade_log() -> int:
    """Copy rows from the retired Trade Log's `positions` table into the book.

    Phase 8 retired the legacy stack (`legacy/analytics/trade_log.py`), which
    kept a separate hand-entered ledger in the same database. Idempotent: each
    migrated row carries `legacy trade_log id N` in its notes and is skipped
    on a re-run. The legacy table is left in place, untouched. Returns the
    number of rows copied.
    """
    con = _connect()
    try:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        if "positions" not in tables:
            return 0
        legacy = con.execute("SELECT * FROM positions ORDER BY id").fetchdf()
        copied = 0
        for _, row in legacy.iterrows():
            tag = f"legacy trade_log id {int(row['id'])}"
            if con.execute("SELECT count(*) FROM paper_positions WHERE notes LIKE ?",
                           [f"%{tag}%"]).fetchone()[0]:
                continue
            premium = float(row["premium_collected"])
            contracts = int(row["contracts"])
            con.execute(
                "INSERT INTO paper_positions (ticker, strategy, strike, expiration, "
                "contracts, actual_fill, entry_date, entry_fees, status, exit_date, "
                "exit_price, collateral, entered_by, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'migrated', ?)",
                [str(row["ticker"]).upper(), row["strategy"], float(row["strike"]),
                 row["expiration"], contracts, premium, row["entry_date"],
                 float(row["commission"] or 0.0), row["status"], row["exit_date"],
                 row["exit_price"], float(row["strike"]) * 100 * contracts,
                 "; ".join(x for x in (tag, row["notes"]) if isinstance(x, str) and x)])
            copied += 1
        _migrate(con)
        return copied
    finally:
        con.close()


# --- Legs from a recommendation --------------------------------------------

def _num(value) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _legs_from_json(rec: dict) -> list[dict]:
    """Phase 16: a strategy-spec row carries its legs as JSON."""
    import json
    out = []
    for raw in json.loads(rec["legs_json"]):
        if raw.get("option_type") == "stock":
            raise ValueError("the paper book records option legs only; buy the shares in "
                             "the executing account and write the call against them on "
                             "the Wheel page")
        out.append({"option_type": raw["option_type"], "side": raw["side"],
                    "strike": float(raw["strike"]),
                    "expiration": pd.Timestamp(raw["expiration"]).date(),
                    "qty": int(raw.get("qty") or 1), "quote_bid": _num(raw.get("bid")),
                    "quote_ask": _num(raw.get("ask")), "quote_mid": _num(raw.get("mid")),
                    "iv": _num(raw.get("iv")), "delta": _num(raw.get("delta")),
                    "root_symbol": rec.get("root_symbol")})
    return out


def legs_from_recommendation(rec: dict) -> list[dict]:
    """The legs a sheet row describes, in the paper_legs shape (per contract)."""
    if rec.get("legs_json"):
        return _legs_from_json(rec)
    strategy = rec.get("strategy") or "csp"
    expiration = pd.Timestamp(rec["expiration"]).date()
    root = rec.get("root_symbol")
    short = {"option_type": "put", "side": "short", "strike": float(rec["strike"]),
             "expiration": expiration, "qty": 1,
             "quote_bid": _num(rec.get("bid")), "quote_ask": _num(rec.get("ask")),
             "quote_mid": _num(rec.get("mid")), "iv": _num(rec.get("implied_vol")),
             "delta": _num(rec.get("delta")), "root_symbol": root}
    if strategy == "csp":
        return [short]
    if strategy == "pcs":
        long = {"option_type": "put", "side": "long", "strike": float(rec["long_strike"]),
                "expiration": expiration, "qty": 1,
                "quote_bid": _num(rec.get("long_bid")), "quote_ask": _num(rec.get("long_ask")),
                "quote_mid": _num(rec.get("long_mid")),
                "iv": _num(rec.get("long_iv")) or _num(rec.get("implied_vol")),
                "delta": _num(rec.get("long_delta")), "root_symbol": root}
        return [short, long]
    raise ValueError(f"the paper book records {', '.join(s.upper() for s in STRATEGIES)}, "
                     f"and strategy-spec rows that carry their legs; got {strategy!r}")


def _package_quote(legs: list[dict]) -> tuple[float | None, float | None]:
    """Net mid and summed half-spread of the package at entry -- what the
    fill calibration needs to recover the realised slippage fraction."""
    net_mid, half = 0.0, 0.0
    for leg in legs:
        bid, ask = leg.get("quote_bid"), leg.get("quote_ask")
        if bid is None or ask is None or ask < bid:
            return None, None
        sign = -1.0 if leg["side"] == "short" else 1.0
        net_mid -= sign * leg["qty"] * (bid + ask) / 2.0
        half += leg["qty"] * (ask - bid) / 2.0
    return net_mid, half


def _fee_sides(legs: list[dict], contracts: int, opening: bool) -> list[tuple[str, int]]:
    out = []
    for leg in legs:
        short = leg["side"] == "short"
        side = ("sell" if short else "buy") if opening else ("buy" if short else "sell")
        out.append((side, contracts * int(leg.get("qty") or 1)))
    return out


PREDICTION_KEYS = re.compile(
    r"^(pop|p_hit_\d+|median_days_\d+|p_max_loss|p_touch|p_assign|p_short_itm|p_roll)_blend$")


def predictions_from_recommendation(rec: dict) -> list[tuple[str, str, float]]:
    """(metric, model, value) triples the engine claimed at entry."""
    out = []
    for key, value in rec.items():
        match = PREDICTION_KEYS.match(str(key))
        number = _num(value)
        if match and number is not None:
            out.append((match.group(1), "blend", number))
    if _num(rec.get("prob_otm_empirical")) is not None:
        out.append(("pop", "empirical", float(rec["prob_otm_empirical"])))
    if _num(rec.get("prob_touch")) is not None:
        out.append(("p_touch", "empirical", float(rec["prob_touch"])))
    if _num(rec.get("prob_max_loss")) is not None:
        out.append(("p_max_loss", "empirical", float(rec["prob_max_loss"])))
    return out


# --- Accepting a recommendation -------------------------------------------

@dataclass
class AcceptResult:
    position_id: int
    cycle_id: int | None
    contracts: int
    actual_fill: float
    modelled_fill: float
    slippage: float
    net_credit: float
    collateral: float
    message: str


def describe(strategy: str, ticker: str, expiration, strike: float,
             long_strike: float | None = None) -> str:
    if strategy == "pcs" and long_strike is not None:
        return f"{ticker} {expiration} ${strike:g}/${long_strike:g} put spread"
    return f"{ticker} {expiration} ${strike:g} put"


def accept(recommendation: dict, contracts: int | None = None,
           actual_fill: float | None = None, entry_date: dt.date | None = None,
           run_id: str | None = None, notes: str = "",
           leg_fills: list[float] | None = None,
           _cycle_id: int | None = None, _rolled_from: int | None = None,
           _rolls_used: int = 0, book: str = "taken", sample: str = "manual",
           tracking: dict | None = None) -> AcceptResult:
    """Record an accepted trade -- a cash-secured put or a put credit spread.

    `actual_fill` is the NET credit you really got in the executing account
    (per share). Leave it None and the modelled fill is used, but the row is
    still marked so calibration can exclude it -- an assumed fill is not
    evidence about slippage. For a spread you can give `leg_fills` instead
    (one price per leg, in leg order: short, long); the net is derived.

    `contracts` overrides the recommended size; you may have taken less because
    the fill dried up, or more because you disagreed with the cap. Either way,
    what is recorded is what happened.

    `book` (Phase 18): `taken` (a real trade) or `tracked` (a forward test at
    the modelled fill: no wheel cycle, not counted against the account).
    `tracking` holds the extra Phase 18 columns (dedupe_key, trade_id,
    entry_spot, entry_context, source_row, preset, rank_at_log, promoted_from).
    """
    if book not in BOOKS:
        raise ValueError(f"book must be one of {BOOKS}")
    if sample not in SAMPLES:
        raise ValueError(f"sample must be one of {SAMPLES}")
    cfg = load_config().get("execution", {})
    if not cfg.get("allow_manual_fill_override", True) and (
            actual_fill is not None or leg_fills):
        raise ValueError("manual fill override is disabled in config")

    strategy = recommendation.get("strategy") or "csp"
    legs = legs_from_recommendation({**recommendation, "strategy": strategy})
    ticker = str(recommendation["ticker"]).upper()
    strike = float(recommendation["strike"])
    long_strike = float(recommendation["long_strike"]) if strategy == "pcs" else None
    expiration = pd.Timestamp(recommendation["expiration"]).date()
    modelled = float(recommendation["modelled_fill"])
    size = int(contracts if contracts is not None else recommendation["contracts"])
    if size < 1:
        raise ValueError("contracts must be at least 1")

    if leg_fills:
        if len(leg_fills) != len(legs):
            raise ValueError(f"{len(legs)} leg fill(s) needed, got {len(leg_fills)}")
        for leg, price in zip(legs, leg_fills):
            leg["entry_price"] = float(price)
        if actual_fill is None:
            actual_fill = sum((1 if l["side"] == "short" else -1) * l["qty"] * l["entry_price"]
                              for l in legs)
    elif len(legs) == 1:
        legs[0]["entry_price"] = float(actual_fill) if actual_fill is not None else modelled

    fill = float(actual_fill) if actual_fill is not None else modelled
    generic = bool(recommendation.get("legs_json"))
    if strategy == "pcs" and not 0 < fill < abs(strike - long_strike):
        raise ValueError(f"a put credit spread's net credit must be between 0 and the "
                         f"width ${abs(strike - long_strike):g}; got ${fill:.2f}")
    slippage = fill - modelled
    entry_date = entry_date or dt.date.today()
    entry_fees = costs.legs_open(_fee_sides(legs, size, opening=True)).total
    width = abs(strike - long_strike) if long_strike is not None else None
    max_profit_share = fill
    if generic:
        # A strategy spec's risk comes from its own Position at the actual fill.
        from analytics import margin
        from analytics.strategies import resolver
        position = resolver.position_from_row({**recommendation, "modelled_fill": fill})
        max_loss = position.max_loss * 100.0 * size
        collateral = margin.bpr_per_contract(position, recommendation.get("margin_class")
                                             or "defined_risk",
                                             float(recommendation.get("spot") or strike)) * size
        max_profit_share = position.max_profit
    elif strategy == "pcs":
        max_loss = (width - fill) * 100.0 * size
        collateral = max_loss
    else:
        max_loss = (strike - fill) * 100.0 * size
        collateral = strike * 100.0 * size
    net_credit = fill * 100.0 * size - entry_fees
    net_mid, half = _package_quote(legs)
    predictions = predictions_from_recommendation(recommendation)
    pop = next((v for m, model, v in predictions if m == "pop" and model == "blend"),
               _num(recommendation.get("prob_otm_empirical")))

    con = _connect()
    try:
        cycle_id = _cycle_id
        if book == "tracked":
            cycle_id = None                     # a forward test never opens a wheel cycle
        elif strategy == "csp" and cycle_id is None:
            cycle_id = con.execute(
                "INSERT INTO cycles (ticker, state, opened_date, total_premium, total_fees) "
                "VALUES (?, 'put_open', ?, ?, ?) RETURNING cycle_id",
                [ticker, entry_date, fill * 100.0 * size, entry_fees]).fetchone()[0]
        elif cycle_id is not None:
            con.execute("UPDATE cycles SET state = 'put_open', "
                        "total_premium = total_premium + ?, total_fees = total_fees + ? "
                        "WHERE cycle_id = ?", [fill * 100.0 * size, entry_fees, cycle_id])

        position_id = con.execute(
            """INSERT INTO paper_positions
               (cycle_id, ticker, strategy, strike, expiration, contracts,
                modelled_fill, actual_fill, slippage, quote_bid, quote_ask, quote_mid,
                entry_date, entry_fees,
                status, collateral, rec_prob_otm, rec_expected_value,
                rec_ev_annualised, rec_iv_rv, rec_sample, rec_rationale,
                entered_by, run_id, notes,
                long_strike, width, max_loss, settlement_type, root_symbol,
                quote_net_mid, quote_half_spread, rec_pop, rec_headline_policy,
                rolled_from, rolls_used)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            [cycle_id, ticker, strategy, strike, expiration, size, modelled,
             fill if actual_fill is not None else None, slippage,
             _num(recommendation.get("bid")), _num(recommendation.get("ask")),
             _num(recommendation.get("mid")),
             entry_date, entry_fees,
             collateral, _num(recommendation.get("prob_otm_empirical")),
             _num(recommendation.get("expected_value")),
             _num(recommendation.get("ev_annualised")),
             _num(recommendation.get("iv_rv_ratio")),
             recommendation.get("sample_label"), recommendation.get("rationale"),
             "manual", run_id, notes,
             long_strike, width, max_loss,
             _settlement(recommendation, ticker),
             recommendation.get("root_symbol"),
             net_mid, half, pop, recommendation.get("headline_policy"),
             _rolled_from, int(_rolls_used)]).fetchone()[0]

        con.execute("UPDATE paper_positions SET max_profit_share = ?, legs_label = ?, "
                    "book = ?, sample = ?, logged_at = ? WHERE id = ?",
                    [max_profit_share, recommendation.get("legs") if generic else None,
                     book, sample, dt.datetime.now(), position_id])
        extra = {k: v for k, v in (tracking or {}).items()
                 if k in ("dedupe_key", "preset", "rank_at_log", "trade_id", "entry_spot",
                          "entry_context", "source_row", "promoted_from")}
        for column, value in extra.items():
            con.execute(f"UPDATE paper_positions SET {column} = ? WHERE id = ?",
                        [value, position_id])
        for index, leg in enumerate(legs):
            con.execute(
                "INSERT INTO paper_legs (position_id, leg_index, option_type, side, strike, "
                "expiration, qty, entry_price, modelled_price, quote_bid, quote_ask, "
                "quote_mid, iv, delta, root_symbol) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [position_id, index, leg["option_type"], leg["side"], leg["strike"],
                 leg["expiration"], leg["qty"], leg.get("entry_price"),
                 leg.get("quote_mid"), leg.get("quote_bid"), leg.get("quote_ask"),
                 leg.get("quote_mid"), leg.get("iv"), leg.get("delta"),
                 leg.get("root_symbol")])
        for metric, model, value in predictions:
            con.execute("INSERT INTO paper_predictions VALUES (?, ?, ?, ?)",
                        [position_id, metric, model, value])
    finally:
        con.close()

    what = (f"{ticker} {recommendation.get('label') or strategy} {recommendation.get('legs')}"
            if generic else describe(strategy, ticker, expiration, strike, long_strike))
    note = (f"Recorded {size} {what} at ${fill:.2f}"
            + (f" ({slippage:+.3f} vs modelled ${modelled:.2f})"
               if actual_fill is not None else " (modelled fill -- excluded from "
                                               "slippage calibration)"))
    return AcceptResult(position_id, cycle_id, size, fill, modelled, slippage,
                        net_credit, collateral, note)


# --- Lifecycle -------------------------------------------------------------

def _position(con, position_id: int) -> dict:
    frame = con.execute("SELECT * FROM paper_positions WHERE id = ?", [position_id]).fetchdf()
    if frame.empty:
        raise ValueError(f"no paper position {position_id}")
    return frame.iloc[0].to_dict()


def _legs(con, position_id: int) -> pd.DataFrame:
    return con.execute("SELECT * FROM paper_legs WHERE position_id = ? ORDER BY leg_index",
                       [position_id]).fetchdf()


def _max_profit_share(row: dict, fill) -> float | None:
    """Max profit per share: the credit for a CSP or PCS, the spec
    position's own max profit for a Phase 16 strategy (a calendar is a
    debit, so its credit is no yardstick)."""
    value = row.get("max_profit_share")
    if value is not None and pd.notna(value) and float(value) > 0:
        return float(value)
    return float(fill) if fill else None


def settlement_value(legs: pd.DataFrame, price: float) -> tuple[float, list[float], int]:
    """Net debit per share to settle the package at `price` (short intrinsic
    minus long intrinsic), each leg's intrinsic, and how many legs are ITM."""
    values, net, itm = [], 0.0, 0
    for _, leg in legs.iterrows():
        k = float(leg["strike"])
        value = max(k - price, 0.0) if leg["option_type"] == "put" else max(price - k, 0.0)
        values.append(value)
        itm += value > 0
        net += (1 if leg["side"] == "short" else -1) * int(leg["qty"] or 1) * value
    return net, values, itm


def close_position(position_id: int, status: str, exit_date: dt.date | None = None,
                   exit_price: float | None = None, notes: str | None = None,
                   settlement_price: float | None = None) -> None:
    """Mark an outcome.

    `exit_price` is the NET debit per share paid to close (0 for expiry). For
    a spread that finishes in the money use status `settled` with the
    underlying's `settlement_price`: the debit and the exercise/assignment
    fees follow from it. A physically settled spread with only the short leg
    in the money is recorded as `assigned`: the shares become a share lot at
    the short strike with basis lowered by the net credit, in a new wheel
    cycle -- exactly as an assigned CSP (Tom, 2026-09-28).
    """
    if status not in STATUSES or status == "open":
        raise ValueError(f"status must be one of {STATUSES[1:]}")
    exit_date = exit_date or dt.date.today()

    con = _connect()
    try:
        row = _position(con, position_id)
        if row["status"] != "open":
            raise ValueError(f"position {position_id} is already {row['status']}")
        strategy = row.get("strategy") or "csp"
        cycle_id, ticker = row.get("cycle_id"), row["ticker"]
        cycle_id = None if cycle_id is None or pd.isna(cycle_id) else int(cycle_id)
        contracts, strike = int(row["contracts"]), float(row["strike"])
        entry_fill = row["actual_fill"] if pd.notna(row["actual_fill"]) else row["modelled_fill"]
        legs = _legs(con, position_id)
        leg_exits: list[float | None] = [None] * len(legs)
        extra_note = None
        assign_shares, economic_exit = False, None

        generic = strategy not in STRATEGIES
        if generic:
            if status == "assigned":
                raise ValueError("a strategy-spec position that finishes in the money is "
                                 "'settled' at the underlying's settlement price")
            if status == "expired_otm":
                exit_fees, exit_price = 0.0, 0.0
                leg_exits = [0.0] * len(legs)
            elif status == "settled":
                if settlement_price is None:
                    raise ValueError("settling needs the underlying's settlement price")
                if legs["expiration"].nunique() > 1:
                    raise ValueError("a calendar or diagonal outlives its front expiry: "
                                     "record 'closed_early' with the net debit instead")
                exit_price, leg_exits, itm = settlement_value(legs, float(settlement_price))
                exit_fees = sum(costs.assignment(contracts * int(leg["qty"] or 1)).total
                                for (_, leg), v in zip(legs.iterrows(), leg_exits) if v > 0)
            else:
                exit_fees = costs.legs_close(_fee_sides(
                    legs.to_dict("records"), contracts, opening=False)).total
                exit_price = float(exit_price or 0.0)
        elif strategy == "pcs":
            if status == "assigned":
                raise ValueError("a spread that finishes in the money is 'settled' "
                                 "(give the settlement price), not 'assigned'")
            if status == "expired_otm":
                exit_fees, exit_price = 0.0, 0.0
                leg_exits = [0.0] * len(legs)
            elif status == "settled":
                if settlement_price is None:
                    raise ValueError("settling a spread needs the underlying's settlement price")
                exit_price, leg_exits, itm = settlement_value(legs, float(settlement_price))
                cash = str(row.get("settlement_type") or "") == "cash"
                fees = costs.vertical_exit_fees(contracts, cash)
                exit_fees = fees["max_loss"] if itm >= 2 else fees["short_itm"] if itm else 0.0
                if itm == 1 and not cash:
                    # Only the short leg is in the money and it settles in shares:
                    # the spread becomes a wheel position exactly as an assigned
                    # CSP does -- shares at the short strike, basis lowered by the
                    # net credit, the long put expiring worthless. The loss is in
                    # the lot, not in the option P&L.
                    economic_exit = exit_price
                    status, exit_price, assign_shares = "assigned", 0.0, True
                    extra_note = (f"short leg assigned at settlement ${settlement_price:,.2f}: "
                                  f"{100 * contracts} shares held as a share lot")
            else:
                exit_fees = costs.legs_close(_fee_sides(
                    legs.to_dict("records"), contracts, opening=False)).total
                exit_price = float(exit_price or 0.0)
        else:
            if status == "settled":
                raise ValueError("a cash-secured put that finishes in the money is 'assigned'")
            if status == "expired_otm":
                exit_fees, exit_price = costs.option_expire(contracts).total, 0.0
                leg_exits = [0.0]
            elif status == "assigned":
                exit_fees, exit_price = costs.assignment(contracts).total, 0.0
            else:
                exit_fees = costs.option_close(contracts, "buy").total
                exit_price = float(exit_price or 0.0)
                leg_exits = [exit_price]

        # A spread assigned into shares is judged on its value at settlement.
        tracked = str(row.get("book") or "taken") == "tracked"
        final_exit = economic_exit if assign_shares else exit_price
        best = _max_profit_share(row, entry_fill)
        final_pct = ((float(entry_fill) - final_exit) / best
                     if best and (status != "assigned" or assign_shares) else None)
        seen = row.get("max_profit_pct_seen")
        seen = None if seen is None or pd.isna(seen) else float(seen)
        if final_pct is not None:
            seen = final_pct if seen is None else max(seen, final_pct)
        note = "; ".join(x for x in (notes, extra_note) if x) or None

        con.execute(
            "UPDATE paper_positions SET status = ?, exit_date = ?, exit_price = ?, "
            "exit_fees = ?, notes = COALESCE(?, notes), settlement_price = ?, "
            "max_profit_pct_seen = ? WHERE id = ?",
            [status, exit_date, exit_price, exit_fees, note, settlement_price, seen,
             position_id])
        for (_, leg), value in zip(legs.iterrows(), leg_exits):
            if value is not None:
                con.execute("UPDATE paper_legs SET exit_price = ? WHERE leg_id = ?",
                            [value, int(leg["leg_id"])])

        if tracked:
            return                              # forward tests never touch the wheel
        if assign_shares and cycle_id is None:
            # A spread opened no wheel cycle; its assignment starts one, so the
            # covered-call side manages the shares like any assigned CSP's.
            cycle_id = con.execute(
                "INSERT INTO cycles (ticker, state, opened_date, total_premium, total_fees) "
                "VALUES (?, 'put_open', ?, ?, ?) RETURNING cycle_id",
                [ticker, row.get("entry_date"), float(entry_fill) * 100.0 * contracts,
                 float(row.get("entry_fees") or 0.0)]).fetchone()[0]
            con.execute("UPDATE paper_positions SET cycle_id = ? WHERE id = ?",
                        [cycle_id, position_id])
        if cycle_id is None:
            return
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


# Index roots whose options settle in cash (no shares change hands), for rows
# that do not carry the registry's `settlement`.
_CASH_SETTLED_HINT = {"SPX", "SPXW", "XSP", "NDX", "NDXP", "RUT", "RUTW", "VIX", "DJX"}


def _settlement(rec: dict, ticker: str) -> str:
    """'cash' or 'physical' -- decides the fees when a spread settles ITM."""
    value = str(rec.get("settlement") or rec.get("settlement_type") or "").lower()
    if value in ("cash", "physical"):
        return value
    root = str(rec.get("root_symbol") or ticker).upper().lstrip("^")
    cash = root in _CASH_SETTLED_HINT or ticker.lstrip("^") in _CASH_SETTLED_HINT
    return "cash" if cash else "physical"


@dataclass
class RollResult:
    closed_id: int
    opened: AcceptResult
    close_debit: float
    new_credit: float
    net_per_share: float
    message: str


def roll_position(position_id: int, close_debit: float, new_expiration,
                  new_strike: float, new_credit: float,
                  new_long_strike: float | None = None, contracts: int | None = None,
                  roll_date: dt.date | None = None, notes: str = "") -> RollResult:
    """Roll: buy the position back for `close_debit` (net, per share) and open
    the replacement at `new_credit`. Recorded as two positions -- the old one
    `rolled`, the new one linked by `rolled_from` -- so each keeps its own
    fill, fees and outcome. A CSP roll stays in the same wheel cycle.

    Rolls are meant to be for a net credit (`management.*.require_net_credit_to_roll`);
    a debit roll is recorded, because it happened, and the message says so.
    """
    con = _connect()
    try:
        old = _position(con, position_id)
    finally:
        con.close()
    if old["status"] != "open":
        raise ValueError(f"position {position_id} is already {old['status']}")
    strategy = old.get("strategy") or "csp"
    if strategy == "pcs" and new_long_strike is None:
        width = float(old.get("width") or 0.0)
        new_long_strike = float(new_strike) - width
    roll_date = roll_date or dt.date.today()
    close_position(position_id, "rolled", roll_date, close_debit,
                   notes=f"rolled; {notes}".strip("; "))
    cycle = old.get("cycle_id")
    rec = {"ticker": old["ticker"], "strategy": strategy, "strike": float(new_strike),
           "long_strike": new_long_strike, "expiration": new_expiration,
           "modelled_fill": float(new_credit),
           "contracts": int(contracts or old["contracts"]),
           "settlement": old.get("settlement_type"),
           "root_symbol": old.get("root_symbol")}
    rolls = int(old.get("rolls_used") or 0) + 1 if pd.notna(old.get("rolls_used")) else 1
    opened = accept(rec, actual_fill=float(new_credit), entry_date=roll_date,
                    run_id=old.get("run_id"), notes=f"roll of #{position_id}",
                    _cycle_id=None if cycle is None or pd.isna(cycle) else int(cycle),
                    _rolled_from=position_id, _rolls_used=rolls)
    net = float(new_credit) - float(close_debit)
    message = (f"Rolled #{position_id} into #{opened.position_id}: paid ${close_debit:.2f}, "
               f"took in ${new_credit:.2f}, net {'credit' if net > 0 else 'DEBIT'} "
               f"${abs(net):.2f}/share (roll {rolls})")
    return RollResult(position_id, opened, float(close_debit), float(new_credit), net, message)


def record_mark(position_id: int, mark: float, spot: float | None = None,
                mark_date: dt.date | None = None, source: str = "manual") -> float | None:
    """Store a mark (net debit to close, per share) for an open position and
    return its profit as a fraction of max profit. The best one seen is kept
    on the position: it decides whether a P(reach X%) prediction came true."""
    mark_date = mark_date or dt.date.today()
    con = _connect()
    try:
        row = _position(con, position_id)
        fill = row["actual_fill"] if pd.notna(row["actual_fill"]) else row["modelled_fill"]
        best = _max_profit_share(row, fill)
        pct = (float(fill) - float(mark)) / best if best else None
        con.execute("DELETE FROM paper_marks WHERE position_id = ? AND mark_date = ? "
                    "AND source = ?", [position_id, mark_date, source])
        con.execute("INSERT INTO paper_marks VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [position_id, mark_date, spot, float(mark), pct, source,
                     dt.datetime.now()])
        if pct is not None:
            con.execute("UPDATE paper_positions SET max_profit_pct_seen = "
                        "greatest(coalesce(max_profit_pct_seen, -1e9), ?) WHERE id = ?",
                        [pct, position_id])
        return pct
    finally:
        con.close()


def _query(sql: str, params: list | None = None) -> pd.DataFrame:
    con = _connect()
    try:
        return con.execute(sql, params or []).fetchdf()
    finally:
        con.close()


def list_positions(status: str | None = None, book: str | None = None) -> pd.DataFrame:
    """Positions, optionally by status and book (Phase 18: capacity, exposure
    and correlation use `book="taken"`; tracked forward tests never count)."""
    query = "SELECT * FROM paper_positions"
    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(status)
    if book:
        where.append("coalesce(book, 'taken') = ?")
        params.append(book)
    if where:
        query += " WHERE " + " AND ".join(where)
    return _query(query + " ORDER BY entry_date DESC, id DESC", params)


def list_legs(position_ids: list[int] | None = None) -> pd.DataFrame:
    if position_ids is not None and not len(position_ids):
        return _query("SELECT * FROM paper_legs WHERE false")
    query = "SELECT * FROM paper_legs"
    if position_ids is not None:
        query += f" WHERE position_id IN ({', '.join(str(int(i)) for i in position_ids)})"
    return _query(query + " ORDER BY position_id, leg_index")


def list_marks(position_id: int | None = None) -> pd.DataFrame:
    if position_id is None:
        return _query("SELECT * FROM paper_marks ORDER BY position_id, mark_date")
    return _query("SELECT * FROM paper_marks WHERE position_id = ? ORDER BY mark_date",
                  [position_id])


def list_predictions(position_id: int | None = None) -> pd.DataFrame:
    if position_id is None:
        return _query("SELECT * FROM paper_predictions ORDER BY position_id, metric")
    return _query("SELECT * FROM paper_predictions WHERE position_id = ? ORDER BY metric",
                  [position_id])


def list_share_lots(open_only: bool = True) -> pd.DataFrame:
    query = "SELECT * FROM share_lots"
    if open_only:
        query += " WHERE disposed_date IS NULL"
    return _query(query + " ORDER BY acquired_date DESC")


def leg_text(position: dict | pd.Series, legs: pd.DataFrame | None = None) -> str:
    """'$748/$738 put spread' or '$61p' for display."""
    label = position.get("legs_label")
    if isinstance(label, str) and label:
        return label
    long = position.get("long_strike")
    if position.get("strategy") == "pcs" and long is not None and pd.notna(long):
        return f"${float(position['strike']):g}/${float(long):g} put spread"
    return f"${float(position['strike']):g}p"


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


def realized(frame: pd.DataFrame) -> pd.Series:
    """Dollar P&L of closed positions, net of every fee. The exit price is the
    net debit paid, so the same formula serves a put and a spread."""
    fill = frame["actual_fill"].fillna(frame["modelled_fill"]).astype(float)
    exit_price = frame["exit_price"].fillna(0.0).astype(float)
    fees = (frame["entry_fees"].fillna(0.0) + frame["exit_fees"].fillna(0.0)).astype(float)
    return (fill - exit_price) * 100 * frame["contracts"].astype(float) - fees


def performance() -> dict:
    """Realised outcomes over closed positions, net of every fee."""
    frame = list_positions()
    if frame.empty:
        return {"n_closed": 0}
    closed = frame[frame["status"] != "open"].copy()
    if closed.empty:
        return {"n_closed": 0, "n_open": int(len(frame))}

    closed["realized"] = realized(closed)
    days = (pd.to_datetime(closed["exit_date"]) - pd.to_datetime(closed["entry_date"])
            ).dt.days.clip(lower=1)
    collateral = closed["collateral"].astype(float).replace(0, float("nan"))
    closed["annualised"] = (closed["realized"] / collateral) * (365.0 / days)
    closed["strategy"] = closed["strategy"].fillna("csp")

    by_strategy = {}
    for name, group in closed.groupby("strategy"):
        by_strategy[name] = {
            "n_closed": int(len(group)),
            "profit_rate": float((group["realized"] > 0).mean()),
            "total_realized": float(group["realized"].sum()),
            "mean_annualised": float(group["annualised"].mean()),
        }

    return {
        "n_closed": int(len(closed)),
        "n_open": int((frame["status"] == "open").sum()),
        "win_rate": float((closed["status"] == "expired_otm").mean()),
        "profit_rate": float((closed["realized"] > 0).mean()),
        "assignment_rate": float((closed["status"] == "assigned").mean()),
        "total_realized": float(closed["realized"].sum()),
        "mean_annualised": float(closed["annualised"].mean()),
        "predicted_win_rate": float(closed["rec_prob_otm"].dropna().mean())
        if closed["rec_prob_otm"].notna().any() else float("nan"),
        "by_strategy": by_strategy,
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
