"""
Stage 1 quantitative screen: reads the daily summary built by
01_build_daily_summary.py and applies liquidity / volatility / drawdown
filters to shrink the ~1000+ ticker universe down to a shortlist worth
manual quality review (Stage 2 tagging) and eventual option-chain
confirmation (Stage 3).

Output is split into two tiers plus a hard-reject list:
    output/tier1_candidates.csv  -- passes every filter INCLUDING enough
                                     history to have been backtested through
                                     a real drawdown
    output/tier2_candidates.csv  -- liquid, well-behaved, but with less
                                     history than min_history_years (usually
                                     because your source data for that ticker
                                     only goes back to ~2024). NOT pre-checked
                                     for leveraged/inverse products the way
                                     Tier 1 typically has been by review time
                                     -- treat these as needing full Stage 2
                                     scrutiny, not a lighter version of it.
    output/stage1_rejected.csv   -- failed on liquidity, price, volatility,
                                     drawdown, or staleness regardless of
                                     history length -- use this to sanity-
                                     check your thresholds

Usage (from D:\\csp):
    python scripts\\02_stage1_screen.py
"""
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml


def load_config(path=None):
    """Load config.yaml, resolved relative to this file rather than the shell's
    working directory.

    The old default (`path="config.yaml"`) meant this script only worked when
    launched from the project root, and silently loaded a *different* config if
    one happened to exist wherever you were standing. Portability fix -- see
    core/paths.py.
    """
    import sys
    _root = Path(__file__).resolve().parent.parent
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
    if path is None:
        from core.paths import load_config as _load
        return _load()
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def reject_row(ticker, reason):
    return {
        "ticker": ticker, "history_years": None, "first_date": None, "last_date": None,
        "days_since_last": None, "adv_90d_dollars": None, "last_price": None,
        "rv_20d_annualized": None, "rv_60d_annualized": None, "max_drawdown": None,
        "pct_off_peak_now": None, "tier": "rejected", "reject_reasons": reason,
    }


def main():
    cfg = load_config()
    proj = Path(cfg["project_root"])
    db_path = proj / "data" / "universe_daily.duckdb"
    out_dir = proj / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not db_path.exists():
        print(f"ERROR: {db_path} not found. Run 01_build_daily_summary.py first.")
        return

    thr = cfg["stage1_thresholds"]

    con = duckdb.connect(str(db_path), read_only=True)
    df = con.execute("""
        SELECT ticker, date, open, high, low, close, volume
        FROM daily_bars
        ORDER BY ticker, date
    """).fetchdf()
    con.close()

    if df.empty:
        print("No data found in daily_bars table.")
        return

    df["date"] = pd.to_datetime(df["date"])
    universe_last_date = df["date"].max()

    results = []
    for ticker, g in df.groupby("ticker", sort=False):
        g = g.sort_values("date").reset_index(drop=True)
        n = len(g)
        if n < 30:
            results.append(reject_row(ticker, "insufficient_data"))
            continue

        first_date, last_date = g["date"].iloc[0], g["date"].iloc[-1]
        history_years = (last_date - first_date).days / 365.25
        days_since_last = (universe_last_date - last_date).days

        dollar_vol = g["close"] * g["volume"]
        adv_90 = dollar_vol.tail(90).mean()
        last_price = g["close"].iloc[-1]

        log_ret = np.log(g["close"] / g["close"].shift(1)).dropna()
        rv_20 = log_ret.tail(20).std() * np.sqrt(252) if len(log_ret) >= 20 else np.nan
        rv_60 = log_ret.tail(60).std() * np.sqrt(252) if len(log_ret) >= 60 else np.nan

        running_max = g["close"].cummax()
        dd = g["close"] / running_max - 1.0
        max_dd = dd.min()
        pct_off_peak_now = g["close"].iloc[-1] / running_max.iloc[-1] - 1.0

        # "hard" reasons always exclude a ticker regardless of history length.
        # history_too_short is tracked separately -- it demotes a ticker to
        # Tier 2 (liquid/quality but limited backtest window) rather than
        # rejecting it outright, since a short data window reflects your
        # data source coverage, not the underlying's actual quality.
        hard_reasons = []
        if days_since_last > thr["max_staleness_days"]:
            hard_reasons.append("stale_data")
        if pd.isna(adv_90) or adv_90 < thr["min_adv_dollars"]:
            hard_reasons.append("adv_too_low")
        if not (thr["min_price"] <= last_price <= thr["max_price"]):
            hard_reasons.append("price_out_of_range")
        if pd.isna(rv_20) or not (thr["min_rv"] <= rv_20 <= thr["max_rv"]):
            hard_reasons.append("volatility_out_of_range")
        # "unrecovered drawdown": fell hard at some point AND is still near that low today
        if max_dd < thr["max_drawdown_floor"] and pct_off_peak_now < thr["max_drawdown_floor"] * 0.5:
            hard_reasons.append("unrecovered_drawdown")

        history_short = history_years < thr["min_history_years"]

        if hard_reasons:
            tier = "rejected"
        elif history_short:
            tier = "tier2_limited_history"
        else:
            tier = "tier1_backtestable"

        results.append({
            "ticker": ticker,
            "history_years": round(history_years, 1),
            "first_date": first_date.date(),
            "last_date": last_date.date(),
            "days_since_last": days_since_last,
            "adv_90d_dollars": round(adv_90, 0) if pd.notna(adv_90) else None,
            "last_price": round(last_price, 2),
            "rv_20d_annualized": round(rv_20, 3) if pd.notna(rv_20) else None,
            "rv_60d_annualized": round(rv_60, 3) if pd.notna(rv_60) else None,
            "max_drawdown": round(max_dd, 3),
            "pct_off_peak_now": round(pct_off_peak_now, 3),
            "tier": tier,
            "reject_reasons": ";".join(hard_reasons) if hard_reasons else "",
        })

    out = pd.DataFrame(results)
    tier1 = out[out["tier"] == "tier1_backtestable"].sort_values("adv_90d_dollars", ascending=False)
    tier2 = out[out["tier"] == "tier2_limited_history"].sort_values("adv_90d_dollars", ascending=False)
    rejected = out[out["tier"] == "rejected"].sort_values("ticker")

    tier1.to_csv(out_dir / "tier1_candidates.csv", index=False)
    tier2.to_csv(out_dir / "tier2_candidates.csv", index=False)
    rejected.to_csv(out_dir / "stage1_rejected.csv", index=False)

    print(f"Universe scanned:                    {len(out)}")
    print(f"Tier 1 (backtestable, full history):  {len(tier1)}  -> {out_dir / 'tier1_candidates.csv'}")
    print(f"Tier 2 (liquid, limited history):     {len(tier2)}  -> {out_dir / 'tier2_candidates.csv'}")
    print(f"Rejected (fails a hard filter):        {len(rejected)}  -> {out_dir / 'stage1_rejected.csv'}")
    print(f"\nNOTE: Tier 2 has NOT been checked for leveraged/inverse products or other")
    print(f"quality issues the way Tier 1 typically is by the time you review it -- run")
    print(f"Stage 2 tagging on both tiers before treating Tier 2 names as tradable.")

    if not rejected.empty:
        print("\nRejection reason breakdown (hard rejects only):")
        reason_counts = rejected["reject_reasons"].str.split(";").explode().value_counts()
        print(reason_counts.to_string())

    if not tier1.empty:
        print("\nTop 15 Tier 1 by 90-day dollar volume:")
        cols = ["ticker", "adv_90d_dollars", "rv_20d_annualized", "history_years"]
        print(tier1.head(15)[cols].to_string(index=False))


if __name__ == "__main__":
    main()
