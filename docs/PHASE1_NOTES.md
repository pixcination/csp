# Phase 1 build notes

What landed, why, and what to do next. Companion to `docs/ARCHITECTURE.md`,
which still describes the original screener accurately — nothing in the
existing `analytics/`, `app/` or `scripts/01`–`06` was modified.

## New layout

```
core/            cross-cutting infrastructure (paths, credentials, calendar, progress)
data_sources/    anything that talks to an external API
analytics/       pure analysis — existing modules plus five new ones
scripts/         command-line entry points
tests/           behavioural tests tied to specific defects
```

`core/paths.py` is the new rule: **no module constructs a path from a string
literal or from the current working directory.** That single convention is
what prevents the credential drift described below from recurring in another
form.

## The credential fix (finding F-01)

`tastytrade_common.py` resolved `.env` against the current working directory
and rewrote the rotating refresh token there. Two copies existed and had
drifted apart on `CLIENT_SECRET`, `REFRESH_TOKEN` and `FRED_API_KEY`.

`D:\csp\.env` is now the single authoritative file, resolved as an absolute
path from the project root. Before any TastyTrade call:

```python
from core import env
import tastytrade_common as ttc
env.bind_tastytrade_client(ttc)   # repoints ENV_PATH, re-seeds cached creds
```

**Do this on your side:** rename `D:\tastytrade\.env` to `.env.retired`.
Nothing reads it now, but while it exists a stray run from that folder can
still rotate the live token into it.

## Massive vs TastyTrade — the split

The free Massive tier is 5 requests/minute, end-of-day only. A single
full-universe refresh is ~14 minutes; a three-month backfill is ~45. That
cannot sit on the activation button's path.

    Massive     →  deep 1-minute history, nightly, never blocks a decision
    TastyTrade  →  live quotes and Greeks over DXLink, on demand, drives trades

`data_sources/massive_sync.py` refuses to run interactively unless explicitly
overridden, precisely so this boundary cannot erode by accident.

## Running it

```powershell
cd D:\csp
powershell -ExecutionPolicy Bypass -File setup.ps1   # venv + deps + preflight
.venv\Scripts\activate
python scripts\preflight.py
python -m pytest tests\ -q
```

`preflight.py` is the thing to run whenever something behaves oddly. It
checks the interpreter, dependencies, credentials, calendar backend, data
freshness and account capacity, and exits non-zero on anything blocking.

## What the numbers now say

Two findings fell out of wiring real costs and real capacity together:

**Half your universe is untradable.** At $50,000 with a 15% per-position cap,
the highest strike you can cash-secure is $75. Thirty of the sixty-one
confirmed names — including AAPL, MSFT, GOOG, AMZN and COST — need more
collateral than that for a single contract. The scanner should filter on
capital feasibility before it ranks anything.

**A percentage profit target is the wrong instrument at 7 DTE.** Opening
costs $1.12/contract and closing costs $0.12. On a weekly cadence, closing at
50% earns half the premium over the same seven days, because the freed
collateral has nowhere to go until the next Friday. `analytics/exit_rules.py`
replaces the target with a test that compares the market's price for the
*remaining* risk against the empirical estimate of that risk.

## Next

Phase 2 is the orchestrator and the live-quote client: `data_sources/quotes.py`
(DXLink), `data_sources/chains.py` (21/60 DTE capture with session stamping),
`data_sources/yfinance_sync.py` (daily bars, earnings, dividends),
`pipeline/run.py`, and `app/pages/0_Command_Center.py`.
