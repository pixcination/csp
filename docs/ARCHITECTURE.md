# Architecture

Complete implementation documentation for the CSP Wheel Analysis Tool: what
was built, how it fits together, and how to reproduce it from scratch. This
document describes the system as it actually exists in code — for the
original brief this was built against, see [PROJECT_SPEC.md](PROJECT_SPEC.md).

## Contents

1. [System overview](#1-system-overview)
2. [Repository layout](#2-repository-layout)
3. [Data sources and formats](#3-data-sources-and-formats)
4. [`config.yaml` reference](#4-configyaml-reference)
5. [Screening pipeline (scripts 01-06)](#5-screening-pipeline-scripts-01-06)
6. [Analytics engine (`analytics/`)](#6-analytics-engine-analytics)
7. [Application layer (`app/`)](#7-application-layer-app)
8. [Reproducing this project from scratch](#8-reproducing-this-project-from-scratch)
9. [Known limitations and methodology caveats](#9-known-limitations-and-methodology-caveats)
10. [Extending the app](#10-extending-the-app)

---

## 1. System overview

Three layers, each building on the last:

```
D:\pricing_data\...  (read-only source: 1-minute price history)
TastyTrade API       (source: live option chain snapshots)
        │
        ▼
┌───────────────────────────────────────────────────────────┐
│ Screening pipeline (scripts/01-06)                         │
│ ~1,050 tickers → Stage 1 (quant filter) → Stage 2 (manual   │
│ quality tags) → Stage 3 (chain liquidity confirmation)      │
│ → output/stage3_candidates.csv (the tradable universe)      │
└───────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────┐
│ Analytics engine (analytics/)                               │
│ realized vol · Black-Scholes/Greeks · technicals · IV rank  │
│ · composite scoring · backtest harness                      │
└───────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────┐
│ Streamlit app (app/), launched via `python launch.py`        │
│ Scanner · Ticker Detail · Trade Log                          │
└───────────────────────────────────────────────────────────┘
```

Everything reads from `config.yaml` at the project root rather than
hardcoding paths or thresholds, so the same code runs identically from the
command line or from the GUI. `D:\pricing_data\...` is read-only source data
— nothing in this project ever writes to it.

**Storage:** DuckDB is the default analytical data store throughout (both
the pipeline and the app). Chain snapshots are Parquet files. There is no
separate backend process or database server — `python launch.py` is a thin
wrapper around `streamlit run app/main.py`; add a persistent background
process only if a real need emerges (e.g. scheduled chain scans), not
speculatively.

---

## 2. Repository layout

```
D:\csp\
├── launch.py                    # python launch.py -> streamlit run app/main.py
├── config.yaml                  # all paths, thresholds, and weights
├── requirements.txt
├── .streamlit/config.toml       # custom dark theme
│
├── docs/                        # you are here
│
├── scripts/                     # screening pipeline, run from the command line
│   ├── 01_build_daily_summary.py
│   ├── 02_stage1_screen.py
│   ├── 03_copy_selected_tickers.py
│   ├── 04_stage3_chain_scan.py
│   ├── 05_stage3_screen.py
│   └── 06_build_1m_cache.py
│
├── analytics/                   # the analytics engine (imported by scripts and the app)
│   ├── config.py                # load_config(), project_root() -- shared config loader
│   ├── data_access.py           # unified reads: CSV/DuckDB/Parquet, one place that
│   │                             #   knows the on-disk layout
│   ├── volatility.py            # realized vol estimators
│   ├── options_math.py          # Black-Scholes pricing/Greeks/IV solver
│   ├── technicals.py            # SMA/RSI/52wk-range/drawdown
│   ├── iv_history.py            # IV rank/percentile from accumulated chain snapshots
│   ├── chain_utils.py           # shared "nearest strike to target delta" helper
│   ├── scoring.py                # composite score
│   ├── backtest.py               # historical delta/DTE rule replay
│   ├── strategies.py             # CSP / covered-call strategy definitions
│   └── trade_log.py              # manual position log storage + stats
│
├── app/                         # the Streamlit application
│   ├── main.py                  # entry point: theme, nav (st.navigation/st.Page)
│   ├── theme.py                 # palette constants shared by CSS and Plotly
│   ├── pages/
│   │   ├── 1_Scanner.py
│   │   ├── 2_Ticker_Detail.py
│   │   └── 3_Trade_Log.py
│   └── components/
│       ├── charts.py            # shared Plotly chart builders
│       ├── formatting.py        # shared number/currency/percent formatting
│       └── jobs.py               # background-subprocess runner for refresh buttons
│
├── data/                        # generated/cached, safe to delete and rebuild
│   │                             #   (except trade_log.duckdb -- that's your data)
│   ├── universe_daily.duckdb     # daily OHLCV for the full ~1,050-ticker universe
│   ├── raw_1m/<ticker>/*.txt     # 1-minute history for the finalized universe only
│   ├── raw_1m_cache.duckdb       # normalized/cached 1-minute bars (see scripts/06)
│   ├── stage3_chains/<date>/     # dated option chain snapshot parquet files
│   ├── iv_history.duckdb         # incremental per-symbol IV observations
│   └── trade_log.duckdb          # manually logged positions
│
└── output/                      # pipeline results (CSV) + generated ticker lists
    ├── stage1_candidates.csv / stage1_rejected.csv
    ├── tier1_candidates.csv / tier2_candidates.csv
    ├── stage2_quality_tags_master.csv
    ├── stage3_candidates.csv     # THE tradable universe the app reads
    ├── stage3_rejected.csv / stage3_scan_log.csv
    └── final_universe.txt        # ticker list scripts/03 copies 1-minute data for
```

---

## 3. Data sources and formats

### 1-minute price history

`D:\pricing_data\stocks_etfs\1m\<TICKER>\<TICKER>_<YYYY-MM>_1m.txt`, columns
`Datetime,Open,High,Low,Close,Volume`.

- Rows within a file are **reverse chronological** — always sort explicitly
  on ingestion (both `scripts/01` and `scripts/06` do this).
- Timestamps are naive (no timezone) local wall-clock, spanning extended
  hours (04:00-20:00) in recent (2024+, massive.com-sourced) data; sparser
  extended-hours coverage pre-2024 (AlphaVantage-sourced) is a real feature
  of illiquid extended-hours trading, not a defect.
- Column *types* vary by source era — e.g. `Volume` is `BIGINT` in some
  months' files and `DOUBLE` in others for the same ticker. `scripts/06`
  handles this with an explicit `CAST(... AS DOUBLE/BIGINT)` per column
  rather than trusting DuckDB's schema inference across a glob.
- A handful of tickers exist under more than one folder name (e.g. `BRK.B`
  vs `BRK-B`) — resolved via `alias_overrides` in `config.yaml` and the
  alias logic in `scripts/01_build_daily_summary.py`.

### Option chain snapshots

Written by `scripts/04_stage3_chain_scan.py` to
`data/stage3_chains/<YYYY-MM-DD>/`, one pair of Parquet files per symbol:

- `<TICKER>_full_chain_<HHMMSS>.parquet` — one row per (expiration, strike),
  columns: `expiration, strike_price, call_symbol, put_symbol`, then
  `call_`/`put_`-prefixed `last, bid, ask, mark, volume, open_interest, iv,
  delta, gamma, theta, vega, rho`.
- `<TICKER>_underlying_<HHMMSS>.parquet` — one row: `symbol,
  instrument_type, bid, ask, last, mark, bid_size, ask_size, day_low,
  day_high, prev_close`.

Bid/ask/mark/last come from REST; open interest and all Greeks/IV come from
DXLink streaming — REST alone does not populate OI/Greeks. Only current/live
snapshots are available (no historical chain API), which is why IV rank
(§6) and the backtest (§6) both have to work around that limitation rather
than replaying real historical quotes.

### Pipeline output (CSV, `output/`)

The two files the analytics engine reads directly:

- **`stage3_candidates.csv`** — one row per Stage-3-passing ticker:
  `ticker, n_expirations_in_window, target_delta, nearest_put_delta,
  nearest_put_strike, oi_at_target_delta, spread_dollars_at_target_delta,
  spread_pct_at_target_delta, strikes_in_delta_band, pass, reasons`.
- **`stage2_quality_tags_master.csv`** — one row per ticker: `ticker,
  data_tier, asset_class, category, quality_tier, leverage_flag,
  sector_review_flag, history_years, first_date, adv_90d_dollars,
  last_price, rv_20d_annualized, max_drawdown, notes`.

`analytics/data_access.py::load_scanner_universe()` merges these two on
`ticker` — this merged table is the base every other analytics module and
the Scanner page build on.

---

## 4. `config.yaml` reference

| Section | Keys | Used by |
|---|---|---|
| Paths | `pricing_data_root`, `project_root`, `tastytrade_pipeline_dir` | all scripts, `analytics/config.py` |
| Session window | `session_start`, `session_end` | `scripts/01`, `scripts/06` (regular-session tagging) |
| Alias handling | `alias_overrides` | `scripts/01` |
| Ingestion | `ingest_workers`, `worker_memory_limit_gb` | `scripts/01` |
| `stage1_thresholds` | `min_history_years`, `max_staleness_days`, `min_adv_dollars`, `min_price`, `max_price`, `min_rv`, `max_rv`, `max_drawdown_floor` | `scripts/02` |
| Stage 3 pacing | `stage3_subs_per_minute_budget` | `scripts/04` |
| `stage3_thresholds` | `dte_min`, `dte_max`, `target_delta`, `delta_band`, `min_oi_near_target`, `min_strikes_in_band`, `max_spread_pct_at_target`, `max_spread_dollars_at_target` | `scripts/05`, `analytics/scoring.py`, `analytics/iv_history.py`, `app/pages/2_Ticker_Detail.py` |
| `analytics` | `risk_free_rate`, `rv_windows_days`, `rsi_window_days`, `sma_windows_days`, `technical_health.downtrend_price_below_sma`, `technical_health.downtrend_rsi_below` | `analytics/technicals.py`, `analytics/scoring.py`, `analytics/backtest.py`, Ticker Detail page |
| `scoring.weights` | `yield`, `iv_rank`, `liquidity`, `technical`, `prob_otm` (must sum to 1.0) | `analytics/scoring.py` |
| `backtest` | `vol_risk_premium_multiplier`, `rv_window_days`, `entry_cadence`, `entry_weekday`, `target_dte`, `assignment_rule` | `analytics/backtest.py` |
| `scanner_presets` | list of `{name, universe, strategy}` | Scanner page |

`analytics/config.py::load_config()` reads this file once (`lru_cache`);
`reload_config()` busts the cache if the GUI ever needs to pick up an edit
without restarting.

---

## 5. Screening pipeline (scripts 01-06)

Full narrative walkthrough (setup, what each script does, what to look for
in the output) is in [README.md](README.md) — this is the condensed
reference:

| Script | Input | Output | Notes |
|---|---|---|---|
| `01_build_daily_summary.py` | `pricing_data_root/*` (~1,050 tickers) | `data/universe_daily.duckdb` (`daily_bars`, `ticker_meta` tables) | Resamples 1-minute → daily OHLCV, regular session only. Does not copy raw data. |
| `02_stage1_screen.py` | `universe_daily.duckdb` | `output/{tier1,tier2}_candidates.csv`, `stage1_rejected.csv` | Liquidity/price/RV/drawdown/staleness filter; splits by `min_history_years` into Tier 1 (backtestable) vs Tier 2 (limited history). |
| *(manual)* Stage 2 tagging | — | `output/stage2_quality_tags_master.csv` | Hand-tagged sector/quality/leverage flags; edited directly, not scripted. |
| `03_copy_selected_tickers.py` | `output/final_universe.txt` | `data/raw_1m/<ticker>/*.txt` | Copies (syncs — safe to re-run) 1-minute history for the finalized universe only. |
| `04_stage3_chain_scan.py` | `stage2_quality_tags_master.csv` (leverage-excluded) | `data/stage3_chains/<date>/*.parquet`, `output/stage3_scan_log.csv` | Pulls chains restricted to the 5-14 DTE window via TastyTrade REST + DXLink. ~60-100 min for the full universe; resumable. |
| `05_stage3_screen.py` | latest `data/stage3_chains/<date>/` | `output/stage3_candidates.csv`, `stage3_rejected.csv` | OI/spread/strike-density liquidity filter at the target delta. Independent of the scan — re-run any time to retune thresholds. |
| `06_build_1m_cache.py` | `data/raw_1m/<ticker>/*.txt` | `data/raw_1m_cache.duckdb` (`bars_1m` table) | **New in the app phase.** Normalizes the reverse-chronological, mixed-schema text files into one sorted, regular-session-tagged table for the analytics engine. Incremental (tracks ingested files by size+mtime, mirrors `scripts/03`'s pattern); processes one source file at a time to bound memory (a single-glob `read_csv_auto` + `ORDER BY` across a ticker's full history was found to OOM on a modest-RAM machine — see the comments in the script). No explicit index: DuckDB's per-row-group min/max zone maps already prune ticker/datetime range scans at this row count (~83M rows across 61 tickers). |

`output/final_universe.txt` is generated directly from `stage3_candidates.csv`
(one ticker per line) — regenerate it any time the Stage 3 candidate list
changes, then re-run `03` and `06`.

---

## 6. Analytics engine (`analytics/`)

Pure Python, no Streamlit imports — every module here is independently
testable from a plain Python shell, which is how each was validated against
real data during the build (SPY/AAPL/AAL sanity checks, not synthetic
fixtures).

### `config.py` / `data_access.py`

`load_config()` / `project_root()` — thin shared config loader, same
`config.yaml` the scripts use.

`data_access.py` is the **only** place that knows the on-disk layout:
- `load_stage3_candidates()`, `load_stage2_tags()`, `load_scanner_universe(universe)`
  — CSV reads + merge; `universe` is `"all"` / `"tier1"` / `"tier2"` /
  comma-separated ticker list.
- `load_daily_bars(ticker, start, end)`, `latest_price_and_adv(ticker)` —
  queries `universe_daily.duckdb`.
- `has_1m_cache()`, `load_1m_bars(ticker, start, end, regular_session_only)`
  — queries `raw_1m_cache.duckdb`; returns an empty frame (not an error) if
  the cache hasn't been built yet.
- `list_snapshot_dates()`, `list_snapshot_dates_for_ticker(ticker)`,
  `load_chain_snapshot(ticker, date=None)` (defaults to the latest date),
  `load_all_snapshots_for_ticker(ticker)` — reads `data/stage3_chains/`.

### `volatility.py` — realized volatility

Three estimator families, all annualized with `sqrt(252)`:

- **Close-to-close** (`close_to_close_series`): `std(ln(C_t/C_{t-1}))` over
  a rolling window.
- **Range-based** (`parkinson_series`, `garman_klass_series`): use each
  day's O/H/L/C — tighter than close-to-close because `daily_bars` already
  derives O/H/L/C from the 1-minute data (`scripts/01`), not just the daily
  close.
  - Parkinson: `daily_var = ln(H/L)² / (4·ln2)`, rolling mean, `× 252`, `sqrt`.
  - Garman-Klass: `daily_var = 0.5·ln(H/L)² − (2·ln2−1)·ln(C/O)²`, clipped
    at 0 before the sqrt (a single noisy day can otherwise go negative).
- **Intraday** (`intraday_rv_series`): true high-frequency realized
  variance — sum of squared consecutive 1-minute log-returns *within each
  regular-session day*, then a rolling mean over N *days*, annualized. This
  is the most direct reading of "use the 1-minute data for a tighter
  estimate" — it measures variance from ~390 intraday points per day
  instead of compressing each session down to four OHLC prices first.
  Requires `data/raw_1m_cache.duckdb` (empty result if absent).

`realized_vol_summary(daily, windows, bars_1m=None)` returns the latest
value for every estimator × window (`config.yaml`'s `rv_windows_days`,
default `[10, 20, 30, 60]`) — what the Scanner/scoring read.

### `options_math.py` — Black-Scholes

European-style pricing only (no early-exercise modeling — a reasonable
simplification for short-dated, modest-dividend equity puts at this tool's
scope).

- `bs_price_greeks(spot, strike, dte_days, vol, rate, option_type)` →
  `Greeks(price, delta, gamma, theta, vega, rho)`. `theta`/`vega`/`rho` are
  per-day / per-vol-point / per-1%-rate respectively, matching standard
  broker-platform conventions.
- `implied_vol(target_price, spot, strike, dte_days, rate, option_type)` —
  bisection solve; returns `None` if the target price doesn't bracket a
  root (used when a chain snapshot has bid/ask but a missing/zero IV field
  — not currently wired into the app, but available).
- `probability_otm(spot, strike, dte_days, vol, rate, option_type)` —
  risk-neutral `P(S_T > K)` (put) / `P(S_T < K)` (call) under GBM.
- `expected_move(spot, dte_days, vol, sigmas=1.0)` — `spot · vol ·
  sqrt(dte_years) · sigmas`; the building block for the probability cone.

Validated against hand-computed sanity cases during the build: an ATM
30-DTE put priced at delta ≈ −0.463 (correct direction/magnitude for a
slightly positive rate), a deep-OTM 10-DTE put priced at $0.006 with 99.5%
POP, and an IV round-trip (price at 25% vol → solve IV back) landing at
0.2500 to four decimal places.

### `technicals.py`

- `sma_series`, `rsi_series` (Wilder's smoothing, `ewm(alpha=1/window)`),
  `distance_from_52wk_range` (trailing 252 trading days),
  `current_drawdown` (close vs. running peak).
- `technical_health_flag(daily, cfg)` — the sanity-check gate from
  PROJECT_SPEC.md ("filter, not primary ranking signal"): flags a
  **downtrend** only when price is below the long SMA (`sma_windows_days`'
  200-day by default) *and* RSI is below `technical_health.downtrend_rsi_below`
  (30 by default) — both conditions, not just "below average." Returns a
  `health_score` of `1.0` (healthy), `0.5` (below SMA but RSI not weak —
  partial credit), or `0.0` (both conditions met) for the composite score's
  technical component.

### `iv_history.py` — IV rank / percentile

Built from the user's *own* accumulated chain snapshots, not a vendor feed
— per PROJECT_SPEC.md, this is the main differentiator vs. reference sites.

- For a given ticker/date, `_extract_iv_point` finds the put nearest
  `stage3_thresholds.target_delta` within the `dte_min`-`dte_max` window (at
  that date's DTE, matching what Stage 3 itself would have selected) via
  `chain_utils.nearest_target_delta_put`, and records its IV, delta, DTE,
  strike, and the underlying spot.
- `ingest_new_snapshots(tickers=None)` scans `data/stage3_chains/` for
  ticker/date pairs not yet cached into `data/iv_history.duckdb`
  (`iv_points` + `ingested_ticker_dates` tables) — a no-op if nothing's new,
  mirroring the skip-if-unchanged pattern in `scripts/03`/`06`.
- `iv_rank_and_percentile(ticker)` — calls `ingest_new_snapshots` first
  (cheap), then computes `iv_rank = (current − min) / (max − min)` and
  `iv_percentile = P(historical IV ≤ current)` **only when at least 10
  distinct snapshot dates exist** for that ticker; otherwise returns `None`
  values with an explanatory `note` rather than a misleadingly precise
  number from 1-2 data points. As of this build, only one scan date
  (2026-07-10) has been run, so every ticker reports "insufficient history"
  — this is expected and resolves as Stage 3 scans are run over time.

### `chain_utils.py`

`nearest_target_delta_put(chain, snapshot_date, target_delta, dte_min,
dte_max)` — the one shared implementation of "find the put nearest the
target delta within the DTE window," used by both `iv_history.py` and
`scoring.py` so this logic exists in exactly one place.

### `scoring.py` — composite score

`candidate_components(ticker, cfg)` computes, per ticker:
- **Annualized yield**: `(mark / strike) × (365 / real_dte)` at the
  nearest-target-delta put, where `real_dte` is calendar days from *today*
  to the contract's expiration (not from the snapshot date — see the
  in-code comment on why these two DTEs are deliberately different: strike
  *selection* uses the DTE-as-of-the-snapshot window, since that's what was
  actually scanned; yield/probability-OTM then use DTE-as-of-today, since
  real time has passed since a possibly stale snapshot).
- **Probability-OTM (theoretical)**: `options_math.probability_otm` using
  the chain's own IV at that strike.
- **IV rank/percentile**: from `iv_history.py`.
- **Technical health**: from `technicals.py`.
- Raw liquidity inputs (OI, spread %, strikes-in-band) carried through for
  the next step.

`compute_composite_scores(tickers)`:
1. **Yield** and **liquidity** (OI, spread, strike density) are normalized
   by **percentile rank within the scanned universe**, not against a fixed
   constant — this adapts to whatever tickers are actually being compared
   (Tier 1 vs. Tier 2 vs. a hand-picked list) without an arbitrary "good OI"
   number baked in.
2. **IV rank**, **probability-OTM**, and **technical health** are already
   0-1 by construction and used directly.
3. Any missing component (most commonly IV rank, pre-10-scans) is
   **neutral-filled at 0.5** rather than zeroed or dropped, so one missing
   input doesn't tank an otherwise-strong candidate.
4. `composite_score = 100 × Σ(component × config weight)`, weights from
   `config.yaml`'s `scoring.weights` (yield 30% / IV rank 20% / liquidity
   20% / technical 15% / prob-OTM 15%, by default — a starting point, not a
   claimed-optimal methodology).

### `backtest.py` — historical delta/DTE rule replay

**The one place synthetic data is unavoidable**, and it's flagged as such
throughout: TastyTrade has no historical chain API, so this simulates
historical option pricing rather than replaying real quotes.

Methodology:
1. On each entry date (weekly, `entry_weekday` — Friday by default), take
   the actual historical close as spot.
2. Compute a **trailing** realized vol (`close_to_close_series`, backward-
   looking only — no lookahead bias) over `rv_window_days`, scaled by
   `vol_risk_premium_multiplier` (1.15 default — short-dated equity puts
   have historically priced richer than trailing RV) as the IV proxy.
3. **Solve for the strike** that would have priced at `target_delta` under
   Black-Scholes, via closed-form inversion of the put-delta formula
   (`solve_strike_for_delta`: `delta = N(d1) − 1` ⟹ `d1 = N⁻¹(delta+1)` ⟹
   solve for K algebraically) rather than an iterative search — this was
   verified to round-trip exactly (solving for −0.25 delta and re-pricing
   the resulting strike reproduces delta = −0.2500).
4. Price the simulated entry premium at that strike via Black-Scholes.
5. Walk forward to the real historical close at (or nearest before)
   `entry_date + target_dte` calendar days and check
   `assignment_rule: close_below_strike_at_expiration`.
6. `pnl_per_share = premium − max(strike − exit_close, 0)`.

Reports: `n_trades`, `win_rate_expired_otm`, `assignment_frequency`,
`avg_realized_return` (per-trade, and annualized ×`365/target_dte`),
`worst_drawdown_dollars` (max drawdown of the cumulative per-contract P&L
curve), `total_pnl_dollars`.

Validated during the build against SPY and AAPL: ~1,331 weekly trades each
over the full ~26-year history, 81-83% win rates at the default −0.25
target delta (directionally consistent with theory — a 25-delta put's
naive POP is ~75%, and the 1.15× risk-premium multiplier pushes strikes
slightly further OTM, so real-world win rates landing a bit above that
raw figure is expected). For AAL specifically, the backtest-calibrated win
rate (74.2%) came in *below* the theoretical Black-Scholes POP (82.2%) —
exactly the correction PROJECT_SPEC.md describes: BS tends to overstate
real-world win rates for short premium strategies (fat tails, vol
clustering), and the backtest is what catches that per-ticker.

### `strategies.py`

A small registry, not a plugin framework: `STRATEGIES = {"csp":
StrategyDef(option_type="put", ...), "covered_call": StrategyDef(option_type=
"call", ...)}`. Exists so "put" / "cash-secured" isn't hardcoded through the
Scanner presets and Trade Log — adding a third strategy later is a
dictionary entry, not a rebuild.

### `trade_log.py`

Manual, local-only position log (`data/trade_log.duckdb`, `positions`
table) — no broker sync, no order placement. Realized P&L accounting
matches how a broker actually books an option position: `realized_pnl =
(premium_collected − exit_price) × 100 × contracts − commission`, where
`exit_price` is "premium paid to close" (0/blank for `expired_otm` and
`assigned`, since assignment converts the option into a stock position
without an additional option-leg cash event — it doesn't itself create
extra P&L in this log). `summary_stats()` computes win rate, assignment
frequency, and annualized realized return over **closed** positions only,
both overall and grouped by ticker.

---

## 7. Application layer (`app/`)

### Entry point and navigation

`launch.py` → `subprocess.run([sys.executable, "-m", "streamlit", "run",
"app/main.py"], cwd=project_root)`. `app/main.py` inserts the project root
onto `sys.path` (so `analytics.*` imports resolve regardless of Streamlit's
own working-directory handling), sets page config, and defines navigation
explicitly with `st.navigation([st.Page(...), ...])` (title/icon/order
controlled in code, not filename-based auto-discovery).

Every page file repeats the same `sys.path` bootstrap at its own top,
defensively, since `st.Page` may execute page scripts in contexts where
that isn't otherwise guaranteed.

### Theme (`app/theme.py`, `.streamlit/config.toml`)

A single Python module (`theme.py`) is the source of truth for every color
used in both the Streamlit theme and every Plotly chart, so they can't
drift apart. Palette validated with the `dataviz` skill's contrast/CVD-
separation checker against the dark chart surface (`#1a1a19`) — all 8
categorical checks pass; the CVD separation sits in the 8-12 "floor band,"
which is legal but means charts with 4+ series lean on direct labels rather
than color alone (applied in `greeks_over_time_chart`, for example).
`.streamlit/config.toml` sets `base = "dark"` with the same hex values
(`primaryColor`, `backgroundColor`, `secondaryBackgroundColor`, `textColor`)
so the chrome and the charts match.

### `app/components/`

- `charts.py` — every Plotly figure builder (`price_vol_chart`,
  `iv_vs_rv_chart`, `pnl_at_expiration_chart`, `pnl_over_time_chart`,
  `greeks_over_time_chart`, `probability_cone_chart`,
  `chain_volume_oi_chart`), all pulling colors from `theme.py` and sharing
  one layout helper (`_apply_layout`). Note: Plotly's `add_vline`/`add_hline`
  break when given a datetime x-value together with `annotation_text` (an
  internal `sum()` over a one-element list of `Timestamp`/`datetime`
  objects) — worked around in `price_vol_chart` by adding the line
  (`add_shape`) and its label (`add_annotation`) as two separate calls
  instead of relying on `add_vline`'s built-in annotation placement.
- `formatting.py` — `fmt_currency`, `fmt_dollars_compact` (e.g.
  `$9.57B`), `fmt_pct`, `fmt_delta`, `fmt_number` — all `None`/NaN-safe
  (`"--"` fallback), used everywhere so every page renders numbers
  identically.
- `jobs.py` — background subprocess runner for the Scanner page's refresh
  buttons. A Stage 3 scan takes 60-100 minutes, so this launches a detached
  `subprocess.Popen` (stdout redirected to `data/.job_logs/<key>.log`) and
  tracks the handle in `st.session_state` rather than blocking the page
  behind `st.spinner`. Scoped intentionally to a single-user local app —
  no separate job queue/database.

### Page 1 — Scanner (`app/pages/1_Scanner.py`)

- Preset selector from `config.yaml`'s `scanner_presets`; universe radio
  (All / Tier 1 / Tier 2 / Custom ticker list).
- Data-refresh expander with three buttons (sync 1-minute data → `scripts/03`;
  run Stage 3 scan → `scripts/04`; re-apply liquidity filter → `scripts/05`),
  each launched via `app.components.jobs`, with a live log tail and
  running/done/failed status badge.
- Builds the table via `load_scanner_universe()` then
  `analytics.scoring.compute_composite_scores()` (wrapped in
  `st.cache_data(ttl=300)` keyed by the sorted ticker tuple, so repeated
  reruns from widget interaction don't re-score the whole universe every
  time). Columns: ticker, composite score, quality tier, category, ADV,
  last price, annualized yield, IV rank, 20d RV, theoretical prob-OTM, put
  strike/delta, current DTE, OI, spread %, and a downtrend warning icon.

### Page 2 — Ticker Detail (`app/pages/2_Ticker_Detail.py`)

Everything on this page recomputes live from the strike/expiration
dropdowns (the "what-if explorer" — no separate mode, it's just the normal
control flow of the page):

1. Ticker + commission-per-contract inputs.
2. Expiration/strike dropdowns, defaulting to the latest snapshot's
   nearest-target-delta contract (`chain_utils.nearest_target_delta_put`).
   Vol input prefers the chain's own IV at the selected strike, falling
   back to 20-day realized vol if absent (both cases labeled in a caption
   so it's never ambiguous which was used). Premium prefers the chain's
   live mark price, falling back to the Black-Scholes theoretical price.
3. Header metrics: spot, DTE (as of *today*, not the snapshot date), put
   delta, annualized yield, theoretical prob-OTM.
4. Price chart with a Bollinger-style ±kσ band (SMA20 ± 2·rolling std) and
   the selected strike/expiration marked.
5. IV vs. realized-vol chart (from `iv_history.py` + `volatility.py`) side
   by side with the net-Greeks-over-time chart (Greeks recomputed at each
   day from 0 to DTE, spot/vol held constant, so the chart isolates pure
   time decay) and numeric Greek metrics (negated to show the *short-put
   seller's* exposure, not the long-put convention `bs_price_greeks`
   returns).
6. P&L at expiration (standard short-put payoff, breakeven marked) and P&L
   over time (day-by-day under flat / +1σ / −1σ price scenarios, Brownian-
   scaled for intermediate days — shows the theta-decay shape of the
   return path, not just the terminal payoff).
7. Probability cone (1σ/2σ expected-move fan from today to expiration)
   next to this ticker's backtest-calibrated stats
   (`analytics.backtest.run_backtest`, cached 1 hour per ticker) —
   win rate / assignment frequency / annualized return / worst drawdown,
   directly comparable to the theoretical prob-OTM shown above it.
8. A "nearby strikes" table across the configured delta band for the
   selected expiration (yield/prob-OTM per strike, current selection
   marked) — the ranked-alternatives part of the what-if explorer.
9. Volume/OI bar chart plus the full raw chain table for the selected
   expiration.

### Page 3 — Trade Log (`app/pages/3_Trade_Log.py`)

- A form to log a new position (ticker, strategy from `STRATEGIES`, strike,
  expiration, contracts, premium, commission, entry date, notes) →
  `analytics.trade_log.add_position`.
- Open positions listed with an inline outcome-marking control (status +
  exit date + "premium paid to close") → `update_position`, plus a delete
  option for mis-logged entries.
- Closed positions table, then overall + per-ticker realized stats
  (`summary_stats`) shown alongside each ticker's backtest win rate for
  direct comparison — the actual-outcome counterpart to the backtest's
  theoretical numbers, per PROJECT_SPEC.md.

---

## 8. Reproducing this project from scratch

1. **Environment**
   ```powershell
   cd D:\csp
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```
2. **Configure** `config.yaml`: `pricing_data_root`, `project_root`,
   `tastytrade_pipeline_dir` (point this at wherever your
   `tastytrade_common.py`/`snapshot_loop.py` live — patched per
   `tastytrade_patch/` if that delivery is present).
3. **Phase 0 — build the universe** (full walkthrough in
   [README.md](README.md)):
   ```powershell
   python scripts\01_build_daily_summary.py
   python scripts\02_stage1_screen.py
   # manually review/edit output\stage2_quality_tags_master.csv
   python scripts\04_stage3_chain_scan.py
   python scripts\05_stage3_screen.py
   ```
4. **Populate the 1-minute cache the analytics engine needs**:
   ```powershell
   # output/final_universe.txt is generated from stage3_candidates.csv,
   # one ticker per line (# comments / blank lines ignored)
   python scripts\03_copy_selected_tickers.py
   python scripts\06_build_1m_cache.py
   ```
5. **Launch**:
   ```powershell
   python launch.py
   ```
6. **Accumulate IV history** by running `scripts/04`+`05` periodically
   (e.g. weekly) — the Scanner page's "Run Stage 3 chain scan" button does
   this without leaving the browser. IV rank needs 10+ distinct scan dates
   per ticker before it reports a number instead of "insufficient history."

### Verifying a from-scratch build

- `python -c "from streamlit.testing.v1 import AppTest; at = AppTest.from_file('app/main.py'); at.run(); print(at.exception)"`
  should print an empty list for each of `app/main.py`,
  `app/pages/2_Ticker_Detail.py`, and `app/pages/3_Trade_Log.py` — this
  headless harness actually executes each page's Python and surfaces any
  exception, unlike a plain `curl` against the running server (Streamlit is
  a client-rendered SPA, so `curl` alone can't see page-level exceptions).
- Spot-check `analytics.options_math.bs_price_greeks` and
  `analytics.backtest.solve_strike_for_delta` round-trip to the target
  delta (see §6) as a sanity check that the Black-Scholes implementation
  wasn't broken by a dependency upgrade.

---

## 9. Known limitations and methodology caveats

- **Backtest pricing is simulated, not historical.** TastyTrade has no
  historical chain API, so `analytics/backtest.py` prices historical trades
  via Black-Scholes off a trailing-realized-vol proxy rather than replaying
  real historical quotes. Treat its output as a real-world-shaped
  correction to Black-Scholes' theoretical POP, not a claim of exact
  historical fills. Revisit `vol_risk_premium_multiplier` once real
  historical option data (e.g. CBOE DataShop) is purchased, per
  PROJECT_SPEC.md.
- **IV rank needs accumulated history.** Only current/live chain snapshots
  are available from TastyTrade; IV rank/percentile is only meaningful once
  10+ Stage 3 scans have run for a given ticker. As of this build, one scan
  date exists, so every ticker shows "insufficient history" — expected, not
  a bug.
- **No American-style early exercise** in `options_math.py` — a reasonable
  simplification for short-dated equity puts at this tool's scope, not
  appropriate if extended to instruments where early exercise materially
  matters.
- **Composite score weights, technical-health thresholds, and the backtest's
  entry cadence/target DTE are starting defaults**, not fitted/optimal
  values — all live in `config.yaml` specifically so they're easy to
  retune without touching code.
- **This is a research tool, not a trading tool.** No order placement, no
  broker execution, by design — keep that boundary explicit if extending
  the UI.

---

## 10. Extending the app

- **A new strategy** (e.g. a credit spread): add an entry to
  `analytics/strategies.py::STRATEGIES`; most of `analytics/` already takes
  `option_type`/strategy as a parameter rather than assuming puts, so this
  should not require touching `volatility.py`, `options_math.py`, or
  `technicals.py`.
- **A new analytics signal**: add a module to `analytics/` following the
  existing pattern (pure functions, `config.yaml`-driven parameters, no
  Streamlit imports) and wire its output into `scoring.py`'s component
  table and weights.
- **A new page**: add a file to `app/pages/` and an `st.Page(...)` entry in
  `app/main.py`; reuse `app/components/charts.py`, `formatting.py`, and
  `analytics/data_access.py` rather than re-implementing data access
  per-page.
- **A persistent background process** (e.g. scheduled chain scans without
  the Scanner page open): only add this if a real need emerges — the
  current one-command `launch.py` design is deliberate, per
  PROJECT_SPEC.md's "Look, feel, and extensibility."
