"""Phase 8 evidence: P(breach) for high-yield names on each price basis.

    python docs/phase8/pbreach_compare.py before   # reads legacy daily_bars_tr
    python docs/phase8/pbreach_compare.py after    # reads daily_bars_raw via load_daily

Both runs truncate history at END so the only difference is the basis.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from analytics.moves import breach_probabilities  # noqa: E402

END = "2026-08-21"
TICKERS = ["MO", "T", "KO", "PBR"]
HORIZONS = [7, 21]              # trading days: a weekly and a monthly put
OTM = [0.05, 0.10]
OUT = Path(__file__).with_name("pbreach_{}.csv")


def loaders(mode):
    if mode == "before":
        import duckdb
        from core.paths import db_universe_daily
        con = duckdb.connect(str(db_universe_daily()), read_only=True)
        def load(t):
            f = con.execute("SELECT date, open, high, low, close, volume FROM daily_bars_tr "
                            "WHERE ticker = ? AND date <= ? ORDER BY date", [t, END]).fetchdf()
            f["date"] = pd.to_datetime(f["date"])
            return f
        return {"total_legacy": load}
    from data_sources.yfinance_sync import load_daily
    return {b: (lambda t, b=b: load_daily(t, basis=b, end=END)) for b in ("price", "total")}


def main(mode):
    rows = []
    for basis, load in loaders(mode).items():
        for t in TICKERS:
            daily = load(t)
            spot = float(daily["close"].iloc[-1])
            for h in HORIZONS:
                for otm in OTM:
                    r = breach_probabilities(daily, t, spot, spot * (1 - otm), h,
                                             lookback_years=10, vol_conditioned=False)
                    rows.append({"basis": basis, "ticker": t, "horizon": h, "otm": otm,
                                 "p_breach": r.prob_terminal, "p_touch": r.prob_touch,
                                 "n": r.n_observations})
    frame = pd.DataFrame(rows)
    frame.to_csv(str(OUT).format(mode), index=False)
    print(frame.to_string(index=False))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "after")
