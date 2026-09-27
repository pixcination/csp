"""
Manual trade log -- not broker-synced, no order placement (see
docs/PROJECT_SPEC.md Page 3 / "Explicitly out of scope"). Tracks real logged
positions and their outcomes so the Trade Log page can show actual win
rate / assignment frequency / realized return alongside the backtest's
theoretical numbers.

Realized P&L accounting matches how a broker actually books option P&L:
closing/expiring an option's P&L is premium collected minus whatever was
paid to close it (0 if it expired worthless or was assigned -- assignment
converts the option into a stock position at the strike, it doesn't itself
create additional option-leg P&L). `exit_price` is therefore "premium paid
to close" and is blank/0 for expired_otm and assigned positions.
"""
import duckdb
import pandas as pd

from analytics.config import project_root

STATUSES = ["open", "expired_otm", "rolled", "assigned", "closed_early"]


def _db_path():
    return project_root() / "data" / "trade_log.duckdb"


def _ensure_schema(con):
    con.execute("CREATE SEQUENCE IF NOT EXISTS trade_log_id_seq START 1")
    con.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY DEFAULT nextval('trade_log_id_seq'),
            ticker VARCHAR, strategy VARCHAR, strike DOUBLE, expiration DATE,
            contracts INTEGER, premium_collected DOUBLE, commission DOUBLE,
            entry_date DATE, status VARCHAR, exit_date DATE, exit_price DOUBLE,
            notes VARCHAR
        )
    """)


def add_position(ticker: str, strategy: str, strike: float, expiration, contracts: int,
                  premium_collected: float, commission: float, entry_date, notes: str = "") -> int:
    con = duckdb.connect(str(_db_path()))
    _ensure_schema(con)
    row = con.execute("""
        INSERT INTO positions (ticker, strategy, strike, expiration, contracts,
                                premium_collected, commission, entry_date, status, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
        RETURNING id
    """, [ticker.upper(), strategy, strike, expiration, contracts,
          premium_collected, commission, entry_date, notes]).fetchone()
    con.close()
    return row[0]


def update_position(position_id: int, status: str, exit_date=None, exit_price=None, notes=None):
    con = duckdb.connect(str(_db_path()))
    _ensure_schema(con)
    con.execute(
        "UPDATE positions SET status = ?, exit_date = ?, exit_price = ?, "
        "notes = COALESCE(?, notes) WHERE id = ?",
        [status, exit_date, exit_price, notes, position_id],
    )
    con.close()


def delete_position(position_id: int):
    con = duckdb.connect(str(_db_path()))
    _ensure_schema(con)
    con.execute("DELETE FROM positions WHERE id = ?", [position_id])
    con.close()


def list_positions() -> pd.DataFrame:
    con = duckdb.connect(str(_db_path()), read_only=False)
    _ensure_schema(con)
    df = con.execute("SELECT * FROM positions ORDER BY entry_date DESC, id DESC").fetchdf()
    con.close()
    return df


def _with_realized_pnl(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    exit_price = df["exit_price"].fillna(0.0)
    df["realized_pnl"] = (df["premium_collected"] - exit_price) * 100 * df["contracts"] - df["commission"].fillna(0.0)
    close_date = df["exit_date"].fillna(df["expiration"])
    days_held = (pd.to_datetime(close_date) - pd.to_datetime(df["entry_date"])).dt.days.clip(lower=1)
    capital_basis = df["strike"] * 100 * df["contracts"]
    df["realized_return_pct"] = df["realized_pnl"] / capital_basis
    df["realized_return_annualized"] = df["realized_return_pct"] * (365.0 / days_held)
    return df


def summary_stats(positions: pd.DataFrame | None = None) -> dict:
    """Overall + per-ticker win rate / assignment frequency / avg realized
    return -- the actual-outcome counterpart to the backtest's theoretical
    numbers, computed over CLOSED positions only (open positions have no
    outcome yet)."""
    positions = positions if positions is not None else list_positions()
    closed = positions[positions["status"] != "open"]
    if closed.empty:
        return {"overall": None, "by_ticker": pd.DataFrame()}

    closed = _with_realized_pnl(closed)

    overall = {
        "n_closed": len(closed),
        "win_rate": float((closed["status"] == "expired_otm").mean()),
        "assignment_frequency": float((closed["status"] == "assigned").mean()),
        "avg_realized_return_annualized": float(closed["realized_return_annualized"].mean()),
        "total_realized_pnl": float(closed["realized_pnl"].sum()),
    }

    by_ticker = closed.groupby("ticker").agg(
        n_closed=("id", "count"),
        win_rate=("status", lambda s: (s == "expired_otm").mean()),
        assignment_frequency=("status", lambda s: (s == "assigned").mean()),
        avg_realized_return_annualized=("realized_return_annualized", "mean"),
        total_realized_pnl=("realized_pnl", "sum"),
    ).reset_index().sort_values("total_realized_pnl", ascending=False)

    return {"overall": overall, "by_ticker": by_ticker}
