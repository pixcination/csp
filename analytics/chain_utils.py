"""Shared chain-snapshot helper used by iv_history.py (and, before Phase 8,
the now-retired legacy/analytics/scoring.py) --
factored out so "find the put nearest the target delta within the DTE
window" has one implementation instead of being copied into each caller."""
import pandas as pd


def nearest_target_delta_put(chain: pd.DataFrame, snapshot_date: str,
                              target_delta: float, dte_min: int, dte_max: int) -> dict | None:
    puts = chain[chain["put_delta"].notna()].copy()
    if puts.empty:
        return None

    puts["expiration"] = pd.to_datetime(puts["expiration"])
    snap_dt = pd.to_datetime(snapshot_date)
    puts["dte"] = (puts["expiration"] - snap_dt).dt.days
    puts = puts[(puts["dte"] >= dte_min) & (puts["dte"] <= dte_max)]
    if puts.empty:
        return None

    puts["delta_dist"] = (puts["put_delta"] - target_delta).abs()
    row = puts.sort_values("delta_dist").iloc[0]

    mark = row.get("put_mark")
    if pd.isna(mark) or mark == 0:
        bid, ask = row.get("put_bid"), row.get("put_ask")
        mark = (bid + ask) / 2.0 if pd.notna(bid) and pd.notna(ask) else None

    return {
        "strike": float(row["strike_price"]),
        "expiration": row["expiration"],
        "dte": int(row["dte"]),
        "put_delta": float(row["put_delta"]) if pd.notna(row["put_delta"]) else None,
        "put_iv": float(row["put_iv"]) if pd.notna(row.get("put_iv")) else None,
        "put_mark": float(mark) if mark is not None and pd.notna(mark) else None,
        "put_bid": float(row["put_bid"]) if pd.notna(row.get("put_bid")) else None,
        "put_ask": float(row["put_ask"]) if pd.notna(row.get("put_ask")) else None,
        "put_open_interest": row.get("put_open_interest"),
    }


def monthly_expirations(chain) -> set:
    """Dates (datetime.date) of the standard monthly expirations in a chain
    (Phase 17). TastyTrade labels them `Regular` in `expiration_type`; without
    that column, the third Friday of the month (or the Thursday before it,
    when a holiday moves it) counts as monthly."""
    import datetime as _dt

    import pandas as _pd
    if chain is None or len(chain) == 0 or "expiration" not in chain:
        return set()
    dates = _pd.to_datetime(chain["expiration"]).dt.date
    if "expiration_type" in chain and chain["expiration_type"].notna().any():
        kinds = chain["expiration_type"].astype(str).str.lower()
        return set(dates[kinds == "regular"])
    out = set()
    for d in set(dates):
        first = _dt.date(d.year, d.month, 1)
        third_friday = first + _dt.timedelta(days=(4 - first.weekday()) % 7 + 14)
        if d in (third_friday, third_friday - _dt.timedelta(days=1)):
            out.add(d)
    return out
