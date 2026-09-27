# Architecture

How the CSP / wheel screener is built **as of Phase 8** (2026-09-27). This
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
 yfinance ───────────┐  daily bars (raw), earnings, dividends
 FRED / CBOE (VIX) ──┤  Treasury rates, VIX complex
 TastyTrade ─────────┤  option chains: REST quotes + DXLink Greeks/OI
 Massive ────────────┘  1-minute archive (nightly, never interactive)
          │
          ▼
 data_sources/  ── writes ──▶  data/ (DuckDB + Parquet)
          │
          ▼
 pipeline/run.py  (one orchestrator, staleness-aware, run lock)
   preflight → reference → daily → earnings → chains → analyse
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
                    Portfolio · Signals
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
├── core/                  cross-cutting infrastructure
│   ├── paths.py           every path; no module builds one from a literal
│   ├── env.py             credentials, TastyTrade token rotation
│   ├── market_calendar.py sessions, holidays, session blocks
│   ├── progress.py        stage reporters (console + Streamlit)
│   └── freshness.py       cache ages vs config thresholds (Phase 8)
├── data_sources/          everything that talks to an external API
│   ├── yfinance_sync.py   daily bars (raw), earnings, dividends
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
│   └── pages/             0_Command_Center … 5_Signals
├── scripts/               CLI entry points (section 8)
├── tests/                 test_foundation, test_phase3 … test_phase8
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
| `data/universe_daily.duckdb` → `daily_bars_raw` | `yfinance_sync.sync_daily` | Raw daily OHLCV from yfinance `auto_adjust=False` (Close is split-adjusted, not dividend-adjusted), Yahoo `adj_close`, dividends, splits. One row per ticker/date. |
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

### 3.3 Universe

`core.paths.load_universe()` reads `output/final_universe.txt` (61 names),
which scripts 01–05 produce from the 1-minute archive (section 8.4). Phase 9
replaces this with a registry table.

---

## 4. The pipeline run

`python pipeline/run.py [--quick] [--tickers A,B] [--force-chains]`, or the
**Run** button on Command Center, which calls the same `run()`.

| Stage | Does | Skips when |
|---|---|---|
| preflight | credentials, TastyTrade connectivity, session banner | never |
| reference | Treasury rates, VIX complex | fresh (`reference.py` windows) |
| daily | `sync_daily` over the universe | ticker already has the last completed session |
| earnings | yfinance earnings dates | file under 20 h old |
| chains | TastyTrade capture (≤21 DTE; ≤60 for tickers with open paper positions) | snapshot current for this session block |
| analyse | regime, capacity, open-position management, `evaluate_universe` → `select_sheet` → `portfolio.select`, stress, wheel (covered calls, rolls) | — |

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
| Risk context | `regime.py` (VIX term-structure gate), `gaps.py` (overnight gap risk, corporate-action seam filter), `technicals.py` (SMA/RSI/52-week) |
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
| `stage1_thresholds`, `stage3_thresholds` | universe rebuild screens | `scripts/02`, `04`, `05`, `iv_history.py` |
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
| `freshness` | cache-age warning thresholds | `core/freshness.py` (Phase 8) |

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
```

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
- **IV rank from own captures** needs 10+ capture dates per ticker. Phase 9
  adds TastyTrade `/market-metrics` IVR/IVP.
- **CSP only.** No multi-leg pricing, fees or book yet (Phases 12 and 15).
- **European Black-Scholes** throughout; no early-exercise modelling.
- **Earnings dates** come from free yfinance data (roughly 90% reliable). An
  unknown date fails safe (blocks) unless the calendar as a whole is
  unhealthy.
