"""
debug_stage3_dte.py
=====================
One-symbol diagnostic for the "0 rows / skipped" issue in Stage 3.
Prints the raw expiration dates TastyTrade actually returned, and what
select_expirations("between_5_14_dte") does with them -- pinpoints whether
the bug is in chain parsing (empty/malformed expiration list) or in the
DTE-window token logic itself.

Usage (from D:\\csp):
    python scripts\\debug_stage3_dte.py SPY
"""
import sys
from datetime import date
from pathlib import Path

import yaml

with open("config.yaml") as f:
    cfg = yaml.safe_load(f)

pipeline_dir = cfg["tastytrade_pipeline_dir"]
sys.path.insert(0, pipeline_dir)
import tastytrade_common as ttc  # noqa: E402

symbol = sys.argv[1] if len(sys.argv) > 1 else "SPY"

print(f"today (this machine, per date.today()): {date.today()}")
print(f"pipeline dir: {pipeline_dir}")
print()

ttc.get_access_token(force=True)
chain = ttc.fetch_equity_chain(symbol)

items = chain.get("data", {}).get("items", [])
print(f"chain['data']['items'] length: {len(items)}")
if not items:
    print("!! EMPTY 'items' list -- chain response shape is likely different")
    print("   than expected. Raw response keys:", list(chain.get("data", {}).keys()))
    print("   Full response (truncated):", str(chain)[:1000])
    sys.exit(0)

flat_exps = []
for item in items:
    flat_exps.extend(item.get("expirations", []))
print(f"total expiration entries across all items: {len(flat_exps)}")

if not flat_exps:
    print("!! No expirations found under items[].expirations -- print first item's keys:")
    print("  ", list(items[0].keys()))
    sys.exit(0)

# Show the raw dates and field names as TastyTrade actually sent them
print("\nFirst 5 raw expiration entries (full dict, to check field names):")
for e in flat_exps[:5]:
    print(" ", e)

dates_found = sorted({e.get("expiration-date") or e.get("expiration_date") for e in flat_exps})
print(f"\nAll distinct expiration dates in chain ({len(dates_found)} total):")
print(" ", dates_found[:20], "..." if len(dates_found) > 20 else "")

today = date.today()
print("\nDTE for first 10 dates:")
for ds in dates_found[:10]:
    try:
        d = date.fromisoformat(ds)
        print(f"  {ds}  DTE={ (d - today).days }")
    except Exception as e:
        print(f"  {ds}  !! could not parse as date: {e}")

print("\n--- Now testing select_expirations directly ---")
result = ttc.select_expirations(flat_exps, ["between_5_14_dte"])
print(f"select_expirations(flat_exps, ['between_5_14_dte']) -> {result}")

if not result:
    print("\n!! Confirmed: token matched nothing. Check the DTE values printed")
    print("   above against 5-14 -- if none fall in that range, something is")
    print("   off with 'today' on this machine, or the chain genuinely has a")
    print("   gap (unlikely for a liquid name). If DTEs in range ARE listed")
    print("   above but this still returned [], the bug is inside")
    print("   select_expirations itself -- send this full output back.")
else:
    print("\nToken matched fine for this symbol -- if the batch scan still")
    print("shows 0 rows for everything, the bug may be in how")
    print("04_stage3_chain_scan.py builds/passes the token. Check that")
    print("config.yaml's stage3_thresholds.dte_min/dte_max are plain")
    print("integers (5 and 14), not quoted strings or something unexpected.")
