"""
05_stage3_screen.py
=====================
Applies option-chain liquidity filters to the chain snapshots collected by
04_stage3_chain_scan.py, producing the final CSP-candidate universe.

Focuses on the PUT side (what you're selling for a CSP) at the delta band
you'd actually target for entries, within the 5-14 DTE window the scan was
restricted to.

For each symbol, finds the put strike closest to your target delta and
checks:
    - open interest at that strike (liquidity to actually get filled/rolled)
    - bid/ask spread as a percentage of mid (tight enough not to bleed on entry/exit)
    - number of strikes with a live quote inside your delta band (so you
      have real strike choices, not just one thin option)

Output:
    output/stage3_candidates.csv  -- passed every chain-liquidity filter
    output/stage3_rejected.csv    -- failed, with reason(s)

Usage (from D:\\csp):
    python scripts\\05_stage3_screen.py
"""
from pathlib import Path

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


def latest_chain_dir(proj: Path) -> Path:
    base = proj / "data" / "stage3_chains"
    if not base.exists():
        raise FileNotFoundError(
            f"{base} not found. Run 04_stage3_chain_scan.py first.")
    dated_dirs = sorted([d for d in base.iterdir() if d.is_dir()])
    if not dated_dirs:
        raise FileNotFoundError(f"No dated scan folders under {base}.")
    return dated_dirs[-1]


def evaluate_symbol(chain_path: Path, thr: dict) -> dict:
    ticker = chain_path.name.split("_full_chain_")[0]
    df = pd.read_parquet(chain_path)

    if df.empty:
        return {"ticker": ticker, "pass": False, "reasons": "empty_chain"}

    puts = df[df["put_delta"].notna()].copy()
    if puts.empty:
        return {"ticker": ticker, "pass": False, "reasons": "no_put_greeks_returned"}

    lo, hi = thr["delta_band"]  # e.g. [-0.40, -0.10]
    band = puts[(puts["put_delta"] >= lo) & (puts["put_delta"] <= hi)]
    # A strike only "counts" toward strike density if it actually has a
    # live two-sided quote -- a delta value with no bid/ask is a listed
    # but effectively untradeable strike.
    band_quoted = band[band["put_bid"].notna() & band["put_ask"].notna()
                        & (band["put_bid"] > 0)]

    target = thr["target_delta"]
    puts_with_delta = puts[puts["put_delta"].notna()].copy()
    puts_with_delta["delta_dist"] = (puts_with_delta["put_delta"] - target).abs()
    nearest = puts_with_delta.sort_values("delta_dist").iloc[0]

    oi_at_target = nearest.get("put_open_interest")
    bid = nearest.get("put_bid")
    ask = nearest.get("put_ask")
    mark = nearest.get("put_mark")
    if mark is None or pd.isna(mark) or mark == 0:
        if pd.notna(bid) and pd.notna(ask):
            mark = (bid + ask) / 2.0
    spread_dollars = None
    spread_pct = None
    if pd.notna(bid) and pd.notna(ask):
        spread_dollars = ask - bid
        if mark and mark > 0:
            spread_pct = spread_dollars / mark * 100.0

    n_expirations = df["expiration"].nunique()
    strikes_in_band = len(band_quoted)

    reasons = []
    if pd.isna(oi_at_target) or oi_at_target < thr["min_oi_near_target"]:
        reasons.append("oi_too_low_at_target_delta")

    # Hybrid spread check: a short-DTE, ~25-delta put is often a cheap
    # option (a dollar or two), so a perfectly normal penny-wide market
    # can show up as a huge PERCENTAGE spread even when it's genuinely
    # tight and tradeable. Pass on EITHER the percentage being reasonable
    # OR the raw dollar spread being small enough that the percentage math
    # doesn't matter -- only fail if both look bad.
    max_spread_dollars = thr.get("max_spread_dollars_at_target")
    spread_ok = False
    if spread_pct is not None and spread_pct <= thr["max_spread_pct_at_target"]:
        spread_ok = True
    if (max_spread_dollars is not None and spread_dollars is not None
            and spread_dollars <= max_spread_dollars):
        spread_ok = True
    if spread_dollars is None:
        spread_ok = False  # no quote at all -- can't confirm tradeable
    if not spread_ok:
        reasons.append("spread_too_wide_at_target_delta")

    if strikes_in_band < thr["min_strikes_in_band"]:
        reasons.append("insufficient_strike_density")

    return {
        "ticker": ticker,
        "n_expirations_in_window": n_expirations,
        "target_delta": target,
        "nearest_put_delta": round(float(nearest["put_delta"]), 3),
        "nearest_put_strike": nearest.get("strike_price"),
        "oi_at_target_delta": oi_at_target,
        "spread_dollars_at_target_delta": round(spread_dollars, 3) if spread_dollars is not None else None,
        "spread_pct_at_target_delta": round(spread_pct, 2) if spread_pct is not None else None,
        "strikes_in_delta_band": strikes_in_band,
        "pass": len(reasons) == 0,
        "reasons": ";".join(reasons),
    }


def main():
    cfg = load_config()
    proj = Path(cfg["project_root"])
    thr = cfg["stage3_thresholds"]
    out_dir = proj / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    chain_dir = latest_chain_dir(proj)
    chain_files = sorted(chain_dir.glob("*_full_chain_*.parquet"))
    print(f"Evaluating {len(chain_files)} chain snapshots from {chain_dir}")

    results = [evaluate_symbol(f, thr) for f in chain_files]
    out = pd.DataFrame(results)

    passed = out[out["pass"] == True].sort_values(  # noqa: E712
        "oi_at_target_delta", ascending=False)
    rejected = out[out["pass"] == False].sort_values("ticker")  # noqa: E712

    passed.to_csv(out_dir / "stage3_candidates.csv", index=False)
    rejected.to_csv(out_dir / "stage3_rejected.csv", index=False)

    print(f"Passed:   {len(passed)}  -> {out_dir / 'stage3_candidates.csv'}")
    print(f"Rejected: {len(rejected)}  -> {out_dir / 'stage3_rejected.csv'}")
    if not rejected.empty:
        print("\nRejection reason breakdown:")
        print(rejected["reasons"].str.split(";").explode().value_counts().to_string())


if __name__ == "__main__":
    main()
