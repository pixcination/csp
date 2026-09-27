# Architecture

How the CSP / wheel screener is built **as of Phase 9** (2026-09-27). This
file describes the code as it is. Where the system is going is in
[SCREENER_ROADMAP.md](SCREENER_ROADMAP.md); what each phase changed is in
`docs/PHASE{N}_SUMMARY.md` (Phase 1: [PHASE1_NOTES.md](PHASE1_NOTES.md)). The
original brief is [PROJECT_SPEC.md](PROJECT_SPEC.md).

The *reasoning* behind most design choices lives in each module's docstring
and is not repeated here. Read the docstring before changing a module.

## Contents

1. [System overview](#1-system-overview)
2. [Repository layout](#2-repository-layout)
3. [Data layer](#3-data-layer)
4. [The pipeline run](#4-the-pipeline-run)
5. [Analytics engine](#5-analytics-engine)
6. [Application](#6-application)
7. [config.yaml reference](#7-configyaml-reference)
8. [Operating it](#8-operating-it)
9. [Conventions](#9-conventions)
10. [Known limitations](#10-known-limitations)

---

## 1. System overview

```
 yfinance ───────────┐  daily bars (raw, batched), earnings (+bmo/amc), dividends
 FRED / CBOE (VIX) ──┤  Treasury rates, VIX complex
 TastyTrade ─────────┤  option chains (REST + DXLink); /market-metrics (IVR, IVP...)
 Massive ────────────┤  1-minute archive (nightly, never interactive)
 config/*.yaml ──────┘  macro calendar (FOMC/CPI/NFP), hand-maintained
          │
          ▼
 data_sources/  ── writes ──▶  data/ (DuckDB + Parquet)
          │
          ▼
 universe registry (data/universe.duckdb)  ◀── Universe page, config/universe.csv
          │
 pipeline/run.py  (one orchestrator, staleness-aware, run lock)
   preflight → universe → reference → daily → earnings → metrics → events
             → stage1 → chains → analyse          (--data-only stops before chains)
                                                   │
          ┌────────────────────────────────────────┘
          ▼
 analytics/  (pure functions; no Streamlit)
   moves · costs · sizing · candidates · portfolio · regime · vrp · skew
   surface · gaps · exit_rules · roll_engine · covered_call · paper · …
          │
          ▼
 data/runs/<id>/  manifest.json · candidates.parquet · positions.parquet
          │
          ▼
 app/  (Streamlit): Command Center · Decisions · Wheel · Validation
                    Portfolio · Signals · Universe
```

Research and analysis only. **No order placement** anywhere; the paper book
records fills you entered by hand in another account.

---

## 2. Repository layout

```
D:\csp\
├── launch.py              python launch.py -> streamlit run app/main.py
├── config.yaml            every threshold, weight and limit (section 7)
├── requirements.txt       pinned floor versions; install into .venv
├── .env / .env.example    credentials -- the ONLY .env read (core/env.py)
├── config/                versioned hand-maintained inputs (Phase 9)
│   ├── macro_calendar.yaml  FOMC / CPI / NFP dates, sourced
│   └── universe.csv         snapshot of the registry, rewritten on every change
├── core/                  cross-cutting infrastructure
│   ├── paths.py           every path; no module builds one from a literal
│   ├── env.py             credentials, TastyTrade token rotation
│   ├── market_calendar.py sessions, holidays, session blocks
│   ├── progress.py        stage reporters (console + Streamlit)
│   └── freshness.py       cache ages vs config thresholds (Phase 8)
├── data_sources/          everything that talks to an external API
│   ├── universe.py        the universe registry + symbol mapping (Phase 9)
│   ├── yfinance_sync.py   daily bars (raw, batched), earnings, dividends
│   ├── tasty_metrics.py   TastyTrade /market-metrics (Phase 9)
│   ├── events.py          events table + event policy check (Phase 9)
│   ├── reference.py       Treasury rates, VIX complex
│   ├── chains.py          TastyTrade chain capture
│   ├── tastytrade_client.py  the one entry into vendor/tastytrade
│   └── massive_sync.py    1-minute archive sync (nightly only)
├── analytics/             pure analysis (section 5)
├── pipeline/
│   ├── run.py             the activation button
│   └── results.py         persisted run tables; latest_run() (Phase 8)
├── app/
│   ├── main.py            navigation (st.navigation)
│   ├── theme.py           palette shared by CSS and Plotly
│   ├── components/        charts.py, formatting.py, run_state.py
│   └── pages/             0_Command_Center … 5_Signals, 6_Universe
├── scripts/               CLI entry points (section 8)
├── tests/                 test_foundation, test_phase3 … test_phase9
├── vendor/tastytrade/     vendored client (tastytrade_common, snapshot_loop)
├── legacy/                the retired Phase-2 stack; not imported (Phase 8)
├── weekly_move_analysis/  the original move study, kept as a script
├── docs/
├── output/                screening CSVs; stage2 tags + final_universe.txt are versioned
└── data/                  generated; not versioned (except .gitkeep)
```

---

## 3. Data layer

### 3.1 Stores

| Store | Written by | Contents |
|---|---|---|
| `data/universe.duckdb` → `universe` | `data_sources/universe.py`, Universe page | The registry: symbol, `yf_symbol`, `tt_symbol`, `price_scale`, `asset_class` (stock/etf/index), category/sector/industry, optionable, weeklies, leverage flag, settlement (physical/cash), exercise (american/european), AM/PM settlement times, active, tags, notes, Stage 1 verdict. Mirrored to `config/universe.csv`. |
| same → `market_metrics` | `data_sources/tasty_metrics.py` | One row per symbol per day: IV index, IVR (tos and tw), IVP, 30-day IV/HV, liquidity rating, beta, expected earnings, per-expiration IV with settlement type. |
| same → `events` | `data_sources/events.py` | Derived each run: earnings (merged sources), ex-dividend (history + projection), splits, FOMC/CPI/NFP, OPEX, quad witching. |
| `data/universe_daily.duckdb` → `daily_bars_raw` | `yfinance_sync.sync_daily` | Raw daily OHLCV from yfinance `auto_adjust=False` (Close is split-adjusted, not dividend-adjusted), Yahoo `adj_close`, dividends, splits. One row per ticker/date, under the canonical symbol (XSP is stored as ^SPX × 0.1). |
| same → `daily_bars`, `ticker_meta` | `scripts/01` | Legacy daily bars resampled from the 1-minute archive (split-adjusted). Fallback only. |
| `data/raw_1m_cache.duckdb` → `bars_1m` | `scripts/06` | Normalised 1-minute bars for the universe; used by `gaps.py` and intraday RV. |
| `data/chains/<session_block>/` | `data_sources/chains.py` | Chain snapshots: `<T>_chain.parquet` + `<T>_underlying.parquet`, keyed by session block (`2026-09-25_rth_14`, `_pre`, `_post`, `_closed`; a whole weekend shares one `_closed` block). |
| `data/stage3_chains/<date>/` | `scripts/04` | Legacy chain snapshots; still read by `iv_history.py`. |
| `data/iv_history.duckdb` | `analytics/iv_history.py` | IV observations per ticker/block, rolled up from snapshots. |
| `data/reference/*.parquet` | `reference.py`, `yfinance_sync.py` | `treasury_rates`, `vol_indices`, `earnings`, `dividends`. |
| `data/trade_log.duckdb` | `analytics/paper.py` | The paper book: `cycles`, `paper_positions`, `share_lots`. (The legacy `positions` table is left in place, unused.) |
| `data/runs/<id>/` | `pipeline/run.py`, `pipeline/results.py` | `manifest.json`, `candidates.parquet`, `positions.parquet`. |

### 3.2 The price-basis rule (Phase 8)

`load_daily(ticker, start=None, end=None, basis="price"|"total")`:

- **`price`**: split-adjusted *traded* prices. The only correct basis for
  anything a strike, level or probability is computed from, because options
  settle on the traded price, and that price drops by the dividend on the
  ex-date. Used by `candidates`, `moves` (via its callers), `roll_engine`,
  `covered_call`, `gaps`, `portfolio`, the pipeline's open-position
  evaluation, and `weekly_move_analysis`.
- **`total`**: dividend-adjusted, **derived on read** from stored dividends
  (`total_return_factor`, CRSP convention, anchored at the last returned bar).
  Used only for long-run holder returns: `wheel_backtest` and its
  buy-and-hold comparison, `walkforward`, `regimes`, `sweep_universe`.
  `load_daily_total_return` is a thin wrapper.

The basis delivered is always on `frame.attrs["price_basis"]`. If a ticker
has no raw rows, the loader falls back to the legacy tables and labels the
result `split_adjusted_legacy` / `total_return_legacy`.
`tests/test_phase8.py` pins which modules use which basis.

**Incremental sync.** Each run pulls from the last stored date minus 7 days
and compares that overlap with what is stored. A new dividend, a new split, a
late-posted action, or a restated close re-pulls that ticker's **full**
history (`repull_reason`). A partial re-pull is what created mixed-vintage
seams before Phase 8. `adjustment_check(ticker)` compares the local
total-return factor with Yahoo's `adj_close`. At the Phase 8 migration it
agreed to within 0.0033% on all 61 tickers.

### 3.3 Universe registry (Phase 9)

`core.paths.load_universe(scope="csp")` returns active registry symbols a
cash-secured put can trade (physically settled). `scope="all"` adds the
cash-settled indices; the data stages use it. The registry seeds itself from
`config/universe.csv` if present, else from `output/final_universe.txt` +
`stage2_quality_tags_master.csv` + SPY/QQQ/IWM/DIA/SPX/XSP/NDX/RUT, and falls
back to the text file if it cannot be read.

**Symbol mapping.** Canonical `BRK.B` / `SPX`. Yahoo `BRK-B` / `^SPX`.
TastyTrade `BRK/B` / `SPX`. XSP uses `^SPX` × 0.1, because Yahoo's `^XSP`
only starts in 2021; the two agreed within 0.02% on all 1,397 overlapping
days.

**Weekly bars** are never stored. `analytics/bars.py` resamples daily (W-FRI).
`weekly_on_daily` exposes a week only from its scheduled last session (from
the exchange calendar, so holiday weeks complete on Thursday), which prevents
lookahead.

**Market metrics.** The live field names and units are in the
`tasty_metrics.py` docstring. Every value arrives as a string. IVR and IVP are
fractions, while `implied-volatility-30-day` and the HV fields are percentages.
`dividend-next-date` is stale and ignored.

**Events and policy.** `events.check(symbol, start, end, strategy)` applies
`config.yaml → event_policy` over the window `[start − days_before,
end + days_after]` and returns the most severe action (block / warn / ok)
with the hits. The unknown-earnings fail-safe applies to stocks only, and
degrades to warn if under half the stocks have a forward date.

---

## 4. The pipeline run

`python pipeline/run.py [--quick] [--tickers A,B] [--force-chains]`, or the
**Run** button on Command Center, which calls the same `run()`.

| Stage | Does | Skips when |
|---|---|---|
| preflight | credentials, TastyTrade connectivity, session banner | never |
| universe | ensure/seed the registry | never |
| reference | Treasury rates, VIX complex | fresh (`reference.py` windows) |
| daily | batched `sync_daily` over **all** active symbols; rebuilds `dividends.parquet` | ticker already has the last completed session |
| earnings | yfinance earnings dates + time of day, stocks only, merged into the file | file under 20 h old AND every stock present |
| metrics | TastyTrade `/market-metrics`, 50 per request; updates registry sector/weeklies | today's snapshot stored |
| events | rebuild the events table; report earnings coverage and date disagreements | — |
| stage1 | Stage 1 thresholds on yfinance bars → registry (advisory) | — |
| chains | TastyTrade capture (≤21 DTE; ≤60 for tickers with open paper positions), CSP-tradable symbols only | snapshot current for this session block |
| analyse | regime, capacity, open-position management, `evaluate_universe` (events check, IVR/IVP on every row) → `select_sheet` → `portfolio.select`, stress, wheel (covered calls, rolls) | — |

`--data-only` runs up to and including stage1, then stops. It is meant to be
scheduled nightly (Windows Task Scheduler running
`.venv\Scripts\python.exe pipeline\run.py --data-only`), so the interactive
run only pulls chains. `latest_run()` skips runs without an analyse stage, so
the nightly job never blanks the results pages.

- **Run lock** (`data/runs/run.lock`, holds the owning PID). A second run is
  refused. On Windows, liveness is checked with
  `OpenProcess`/`GetExitCodeProcess`: `os.kill(pid, 0)` *terminates* the
  process there, and killed a live run until Phase 8.
- **Stage isolation.** A failing stage is recorded in the manifest and the
  run continues.
- **Persisted results** (`pipeline/results.py`). `candidates.parquet` holds
  every evaluated strike, accepted *and* rejected, with its rejection reasons
  and the flags `accepted` (passed gates), `selected` (best per ticker within
  `max_new_positions_per_run`) and `proposed` (survived portfolio limits).
  `latest_run()` returns the newest *finished* run. Runs from before Phase 8
  have only the manifest; their proposals are rebuilt from it and
  `has_full_sheet` is False.

---

## 5. Analytics engine

Pure Python, config-driven, no Streamlit imports. Grouped by job:

| Job | Modules |
|---|---|
| Probability of outcome | `moves.py` (empirical P(breach)/P(touch), vol-conditioned, effective n), `options_math.py` (Black-Scholes, Greeks, IV solve), `volatility.py` (close-to-close, Parkinson, Garman-Klass, intraday RV) |
| Market pricing | `vrp.py` (IV/RV), `skew.py` (put skew, term structure), `surface.py` (per-snapshot vol surface, forward), `iv_history.py` (own IV rank, needs 10+ captures) |
| Risk context | `regime.py` (VIX term-structure gate), `gaps.py` (overnight gap risk, corporate-action seam filter), `technicals.py` (SMA/RSI/52-week), `earnings_history.py` (past report reactions: gap, close-to-close, two-session, ATR multiple; implied move once snapshots exist) |
| Universe | `universe_screen.py` (Stage 1 on yfinance; drawdown over `drawdown_lookback_years`), `bars.py` (weekly resample, no-lookahead alignment) |
| Trade construction | `candidates.py` (EV-ranked CSP sheet with hard gates, `evaluate_universe` / `select_sheet`), `costs.py` (tastytrade fees + fill model), `sizing.py` (capital and liquidity caps) |
| Book | `portfolio.py` (correlation clusters, marginal risk, simultaneous-assignment stress), `paper.py` (paper book, slippage, calibration inputs) |
| Management | `exit_rules.py` (hold/close/roll/accept, net of fees), `roll_engine.py`, `covered_call.py` |
| Validation | `wheel_backtest.py` (full wheel cycles, synthetic BS pricing), `walkforward.py`, `regimes.py`, `calibration.py` |
| Shared | `chain_utils.py`, `strategies.py` (label registry; becomes a package in Phase 12), `config.py` (shim over `core.paths`) |

Gates are **rejections, not penalties**. Every probability reports its
sample size.

---

## 6. Application

`app/main.py` defines navigation explicitly:

| Page | Shows | Run data from |
|---|---|---|
| Command Center (default) | session banner, **Run** button with stage progress, open-position decisions, capacity | `active_run()` |
| Decisions | proposed trades, accept with actual fill → paper book; expander with **every evaluated strike** and why each was rejected | `active_run()` + `candidates.parquet` |
| Wheel | defensive rolls, covered calls against assigned lots, wheel backtest (total basis) | `active_run()` |
| Validation | calibration, slippage, IV coverage, walk-forward | disk |
| Portfolio | exposure, clusters, stress, correlation | `active_run()` |
| Signals | gap risk, skew, term structure | disk |
| Universe | registry editor (add / deactivate / tag), IVR/IVP, next earnings and disagreements, Stage 1 verdicts, per-symbol weekly bars and earnings reactions, 45-day market-event calendar with policy | registry, metrics, events |

`app/components/run_state.py`: `active_run()` prefers the run made in this
browser session and otherwise loads `pipeline.results.latest_run()`, so a
refresh or restart keeps the last run. `run_caption()` always names the run
shown, its age, and whether it came from disk.

Headless check: `python scripts/check_pages.py` runs every page through
`streamlit.testing.v1.AppTest`.

---

## 7. config.yaml reference

| Section | Governs | Read by |
|---|---|---|
| `pricing_data_root`, `pricing_data_required`, `tastytrade_pipeline_dir` | external paths (optional archive, vendored client) | `core/paths.py` |
| `session_start/end`, `alias_overrides`, `ingest_*` | 1-minute ingestion | `scripts/01`, `06` |
| `stage1_thresholds` | Stage 1 screen; `drawdown_lookback_years` (Phase 9) | `scripts/02`, `analytics/universe_screen.py` |
| `stage3_thresholds` | chain-liquidity screen | `scripts/04`, `05`, `iv_history.py` |
| `analytics` | risk-free fallback, RV windows, RSI/SMA | `technicals.py`, several |
| `scoring`, `backtest`, `scanner_presets` | legacy stack only | `legacy/` |
| `account` | capital, cash-secured, per-position/ticker/sector caps, max positions | `sizing.py` |
| `liquidity_limits` | % of OI / volume / ADV, OI and volume floors | `sizing.py`, `candidates.py` |
| `execution` | signal_only mode, manual fill override | Decisions, `paper.py` |
| `costs` | tastytrade fee schedule, slippage fraction | `costs.py` |
| `chain_capture` | DTE windows, RTH refresh interval | `chains.py` |
| `massive` | archive pacing | `massive_sync.py` |
| `management.entry/exit/defense/covered_call` | entry gates and DTE band, exit test, roll limits, call rules | `candidates.py`, `exit_rules.py`, `roll_engine.py`, `covered_call.py` |
| `signals` | skew / gap / backwardation gates | `candidates.py` |
| `portfolio` | cluster and correlation limits, stress horizon | `portfolio.py` |
| `regime` | VIX term-structure thresholds and size multiplier | `regime.py` |
| `move_analysis` | horizons, lookbacks, min observations | `moves.py`, `weekly_move_analysis` |
| `paper_trading` | paper book defaults | `paper.py` |
| `freshness` | cache-age warning thresholds, incl. market metrics and events | `core/freshness.py` |
| `event_policy` | per event type: action, days before/after, strategies, asset classes | `data_sources/events.py` |
| `events` | OPEX horizon, earnings disagreement tolerance, calendar-health minimum | `data_sources/events.py` |
| `market_metrics` | request batch size | `data_sources/tasty_metrics.py` |

---

## 8. Operating it

### 8.1 Setup

```powershell
cd D:\csp
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      # then fill in
.venv\Scripts\python scripts\preflight.py
```

### 8.2 Every day

```powershell
.venv\Scripts\python launch.py              # app; press Run on Command Center
# or headless:
.venv\Scripts\python pipeline\run.py
# nightly (schedule it): bars, earnings, metrics, events, Stage 1 -- no chains
.venv\Scripts\python pipeline\run.py --data-only
```

Keep `config/macro_calendar.yaml` current. BLS publishes the next year's CPI
and payrolls dates late in the year, and the Fed confirms each FOMC date at
the meeting before it.

### 8.3 Checks

| Command | What |
|---|---|
| `python scripts/preflight.py` | interpreter, packages, credentials, calendar, **data freshness with ages**, portability, capacity. Exits non-zero on anything blocking. |
| `python -m pytest tests -q` | main suite |
| `python -m pytest legacy/tests -q` | retired stack |
| `python scripts/check_pages.py` | headless run of every page |
| `python scripts/validate.py` | engine validation (calibration, IV coverage, walk-forward, signals) |

### 8.4 Universe rebuild from the 1-minute archive (scripts 01–06)

Kept and working. Needs `D:\pricing_data` connected:
`01_build_daily_summary` → `02_stage1_screen` → *(hand-edit
`output/stage2_quality_tags_master.csv`)* → `04_stage3_chain_scan` →
`05_stage3_screen` → regenerate `output/final_universe.txt` → `03_copy_selected_tickers`
→ `06_build_1m_cache`. Details are in [README.md](README.md). Phase 9 moves Stage 1 onto
yfinance data so the archive becomes optional for this too.

### 8.5 One-off

`scripts/migrate_phase8.py`: backs up `universe_daily.duckdb`, fully
re-pulls `daily_bars_raw`, runs the adjustment check, drops
`daily_bars_tr`, and migrates legacy Trade Log rows. Safe to re-run.
`scripts/consolidate.py`: vendors external dependencies into the project.

---

## 9. Conventions

1. Every path goes through `core.paths`. Every threshold lives in `config.yaml`.
2. No Streamlit imports in `analytics/`, `data_sources/` or `core/`.
3. Gates reject; they do not quietly down-weight. Every probability carries its n.
4. Validate on real data on disk; synthetic fixtures only for formula tests.
5. Each phase adds `tests/test_phase{N}.py` and `docs/PHASE{N}_SUMMARY.md`,
   updates this file, and is committed and pushed
   (`https://github.com/pixcination/csp`).
6. `data/`, `.env`, caches and `*.duckdb`/`*.parquet` are never committed.

---

## 10. Known limitations

- **Option prices in backtests are synthetic** (Black-Scholes on a trailing
  RV proxy). TastyTrade has no historical chain API.
- **The wheel backtest does not credit dividends explicitly.** It runs on the
  total basis, so dividends arrive through the adjusted path, but strikes are
  placed against adjusted rather than traded prices. The correct model (price
  basis plus an explicit dividend credit while shares are held) is deferred to
  Phase 15.
- **IV rank from own captures** needs 10+ capture dates per ticker. The
  TastyTrade IVR/IVP is informational on every candidate and is not yet part
  of any gate or score; ranking weights come in Phase 11.
- **Indices are data-only.** Index chains are not captured until Phase 11
  verifies index underlyings in the chain client. CSP does not apply to them.
- **Implied-move history** for earnings reactions starts accumulating with the
  first daily metrics snapshot (2026-09-27).
- **CSP only.** No multi-leg pricing, fees or book yet (Phases 12 and 15).
- **European Black-Scholes** throughout; no early-exercise modelling.
- **Earnings dates** come from free yfinance data (roughly 90% reliable). An
  unknown date fails safe (blocks) unless the calendar as a whole is
  unhealthy.
