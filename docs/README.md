# CSP Wheel Candidate Screener — Universe Narrowing Pipeline

*(All commands below run from the project root, `D:\csp` — not from this
`docs\` folder. See [ARCHITECTURE.md](ARCHITECTURE.md) for the full,
up-to-date system documentation including the application layer built in
Phase 2.)*

This is Phase 0 of the project: narrowing your ~1,050-ticker universe down to
a shortlist worth manual quality review and option-chain confirmation, before
any GUI or app development begins. See `PROJECT_SPEC.md` for the full
project architecture and the brief to hand to Claude Code once you move into
that phase.

## Setup

```powershell
cd D:\csp
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Edit `config.yaml`:
- Confirm `pricing_data_root` points at `D:\pricing_data\stocks_etfs\1m`.
- Confirm `project_root` is `D:\csp`.
- Set `ingest_workers` and `worker_memory_limit_gb` based on your machine's
  RAM -- see the comments in `config.yaml`. **Do not just crank up
  `ingest_workers`**: DuckDB claims memory per-connection, so too many
  parallel workers will OOM the machine regardless of how much RAM you have.
  A safe starting point is `ingest_workers * worker_memory_limit_gb` equal to
  roughly half your total RAM.

## Step 1 — Build the daily summary (scans your full 1,050-ticker universe)

```powershell
python scripts\01_build_daily_summary.py
```

This reads every `*.txt` file under every ticker folder in `pricing_data_root`,
resamples to daily OHLCV bars (regular session only), and writes a single
compact file: `data\universe_daily.duckdb`. It does **not** touch your source
drive and does **not** copy the full 1-minute data anywhere — that would be a
lot of I/O for tickers you're about to exclude.

Expect this to take a while on the first run across 1,050 tickers — it's
bounded mostly by disk read speed on your source drive, not CPU, so the
`ingest_workers` parallelism helps but has diminishing returns past your
drive's throughput limit. If a ticker's folder is empty, malformed, or has no
data in the regular session window, it's skipped with a printed reason —
review those; they usually mean genuinely bad/empty source data rather than
a bug.

## Step 2 — Run the Stage 1 quantitative filter

```powershell
python scripts\02_stage1_screen.py
```

Reads `data\universe_daily.duckdb` and applies the thresholds in
`config.yaml` under `stage1_thresholds`. Produces three files in `output\`:

- `tier1_candidates.csv` — passes every filter, **including** enough history
  to have been backtested through a real drawdown (`min_history_years`)
- `tier2_candidates.csv` — liquid, well-behaved, but with less history than
  `min_history_years` — typically because your source data for that ticker
  only goes back to ~2024. These are *not* excluded, but they also haven't
  been checked for things like leveraged/inverse products the way Tier 1
  has by the time you're reviewing it — give Tier 2 the same Stage 2
  scrutiny as Tier 1, not a lighter version of it.
- `stage1_rejected.csv` — failed a hard filter (liquidity, price,
  volatility, drawdown, or staleness) regardless of history length

**Look at the rejection reason breakdown printed to console first.** If one
threshold is rejecting an unexpectedly large fraction of your universe,
that's a sign to loosen it before doing manual review — better to see a
slightly longer candidate list and reject by eye than to have a threshold
silently hide a name you'd have wanted. Also worth knowing: the
`stale_data` reason tends to double as a delisting/M&A detector — tickers
that get acquired, go private, go bankrupt, or change ticker symbols
naturally stop updating and get flagged here, which is usually correct
behavior rather than a bug to work around.

## Step 3 — Manual quality tagging (Stage 2)

`output\stage2_quality_tags_master.csv` (delivered separately, alongside this
project) is a first-pass draft covering both tiers:

- **Tier 1 (119 names as of this writing):** fully tagged — asset class,
  category/sector, a quality tier (`core` / `aggressive` / `review`), and a
  `leverage_flag`. A few names are flagged `review` with notes explaining
  why (e.g. `SPYI` is an options-income ETF that already writes calls
  itself; `CVS`/`MMM` have business-specific overhangs worth a second look;
  `TSLA`/`NVDA`/`VST`/`UAL`-type names are tagged `aggressive` despite being
  mega-liquid, since their realized vol is well above the rest of the core
  list).
- **Tier 2 (432 names as of this writing):** automated first pass only —
  asset class (ETF vs. stock) and a `leverage_flag` (9 leveraged/inverse
  products were caught this way — e.g. `UPRO`, `TNA`, `SPXL`, `TMF`,
  `YINN` — that Stage 1's liquidity/volatility filters alone didn't catch).
  Individual stock sectors are marked `TBD` — there are simply too many to
  hand-tag reliably in one pass. Ask for a follow-up batch to continue
  sector-tagging Tier 2 in chunks, prioritized by dollar volume.

Review and edit this file — it's a draft, not a final answer. Pay particular
attention to the `leverage_flag == Yes` rows across both tiers; those should
almost certainly be excluded from a wheel strategy regardless of how good
their liquidity numbers look, since leveraged/inverse products decay by
construction and are a bad thing to be assigned and forced to hold.

## Step 4 — Option chain liquidity confirmation (Stage 3, separate tool)

For your Stage 2 survivors, pull an option chain snapshot for each and apply
the liquidity filters described in `PROJECT_SPEC.md` (open interest, bid/ask
spread, strike density near your target delta, weekly expirations available).
This isn't scripted here yet since it depends on your chain snapshot source
— it's the next build step.

## Step 5 — Finalize the universe and copy its 1-minute data into the project

Create `output\final_universe.txt`, one ticker per line (blank lines and `#`
comments are ignored), then run:

```powershell
python scripts\03_copy_selected_tickers.py
```

This copies (or syncs — safe to re-run) each finalized ticker's full raw
1-minute history from `pricing_data_root` into `data\raw_1m\<ticker>\` inside
the project folder. Re-run this script periodically (e.g. monthly) once you
add fresh 1-minute files to the source drive — it only copies files that are
new or changed, so it's fast after the first run.

## Step 4 — Stage 3: option chain liquidity confirmation (TastyTrade)

### Setup (one-time)

1. Copy `tastytrade_patch/tastytrade_common.py` over the existing file in
   your TastyTrade pipeline folder (the one `snapshot_loop.py` imports).
   This is a **purely additive** change — see `tastytrade_patch/CHANGES.diff`
   for the exact diff — it adds one new expiration-selection token
   (`between_M_N_dte`) alongside the existing ones (`under_N_dte`,
   `next_monthly`, etc.) and doesn't touch anything else. Your continuous
   `snapshot_loop.py` collection keeps working exactly as before.
2. Set `tastytrade_pipeline_dir` in `config.yaml` to that folder (wherever
   `tastytrade_common.py` and `snapshot_loop.py` actually live).

### Why this is a separate tool from snapshot_loop.py

`snapshot_loop.py` is built for narrow-and-deep collection — 1-2 symbols,
continuously, at 5-min cadence, for your analytical engine's development
data. Stage 3 is broad-and-shallow — one chain pull per candidate ticker
(up to ~550), restricted to your actual 5-14 DTE trade window, run once
(or periodically), not continuously. Reusing the continuous loop's
approach for this would either blow TastyTrade's per-session subscription
limit or take far too long paced safely under the per-minute limit.

### Run the scan

```powershell
python scripts\04_stage3_chain_scan.py
```

Universe defaults to everything in `output\stage2_quality_tags_master.csv`
with `leverage_flag != Yes` (currently 542 tickers). Override with
`--tickers SPY,QQQ,AAPL` for a quick test, or create
`output\stage3_universe.txt` (one ticker per line) to hand-curate the scan
list permanently.

This pulls each symbol's chain restricted to `stage3_thresholds.dte_min`-
`dte_max` (5-14 DTE by default) via REST (bid/ask/mark) + one DXLink
streaming collection (open interest, Greeks/IV) per symbol, reusing
`pull_equity()` from your own `snapshot_loop.py` unchanged. A rolling
60-second rate limiter paces symbol-to-symbol so the aggregate subscription
rate across the whole run stays under `stage3_subs_per_minute_budget`
(default 8,000/min, ~20% under TastyTrade's enforced 10,000/min limit).
**Expect roughly 60-100 minutes for the full universe** — this is a batch
job, not something to run continuously. It's resumable: if interrupted,
just re-run and it skips symbols already scanned today (`--force` to
re-pull everything anyway).

Output: `data\stage3_chains\<today's date>\<TICKER>_full_chain_HHMMSS.parquet`
plus a per-symbol underlying quote sibling file, and a scan log at
`output\stage3_scan_log.csv`.

### Apply the liquidity filter

```powershell
python scripts\05_stage3_screen.py
```

For each scanned symbol, finds the put strike closest to
`stage3_thresholds.target_delta` (-0.25 by default) and checks open
interest, bid/ask spread, and strike density within `delta_band`.
Produces `output\stage3_candidates.csv` and `output\stage3_rejected.csv`.
This step is independent of the scan — re-run it any time you want to
retune thresholds without re-pulling chain data.

**Spread check uses a hybrid rule.** A ~25-delta put at 5-14 DTE is often a
cheap option (a dollar or two), so a perfectly normal penny-wide market can
show up as a huge *percentage* spread even when it's genuinely tight and
tradeable — this was confirmed against real scan data, where 97 tickers
were being rejected on percentage-spread alone despite passing every other
filter. A symbol now passes if *either* the percentage is under
`max_spread_pct_at_target` *or* the raw dollar spread is under
`max_spread_dollars_at_target` — only fails if both look bad. Tested
against synthetic cases mimicking the real failure pattern (cheap option
with wide % but tight $ → passes; wide on both → still correctly fails;
normal tight-% option → unaffected).

**Validated against your real SPY/XSP chain samples before delivery:** SPY
correctly passed (2,849 OI, 1.18% spread at the -0.25-delta put, 129
strikes in the delta band); XSP correctly failed on thin OI (17 vs. the
100 threshold) — sanity-checks in line with SPY being a vastly higher-volume
product than XSP.

## Phase 2 — the application

Phase 0 above hands off to an analytics engine (`analytics/`) and a
three-page Streamlit app (`app/`) built on top of this universe. That layer
is fully built — see **[ARCHITECTURE.md](ARCHITECTURE.md)** for the complete
implementation documentation: module-by-module internals, formulas, config
reference, page-by-page walkthrough, and step-by-step reproduction
instructions. Quick start: `python launch.py` from the project root.
