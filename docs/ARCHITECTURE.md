# Architecture

How the CSP / wheel screener is built **as of Phase 20** (2026-09-28). This
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
 pipeline/run.py  (one orchestrator, staleness-aware, run lock)   ◀── ScanRequest (--request x.json)
   preflight → universe → reference → daily → earnings → metrics → events
             → stage1 → technicals → rank_underlyings → chains (top N only) → analyse
                                    (--data-only stops after technicals)
                                                   │
          ┌────────────────────────────────────────┘
          ▼
 analytics/  (pure functions; no Streamlit)
   moves · costs · sizing · candidates · strategies/{csp,pcs} · prob_engine · expected_move
   liquidity · portfolio · regime · vrp · skew · surface · gaps · exit_rules
   roll_engine · covered_call · paper · book · pcs_backtest · …
          │
          ▼
 data/runs/<id>/  manifest.json · candidates.parquet · positions.parquet
          │
          ▼
 app/  (Streamlit): Screener · Trade Detail · Strategies · Symbol Lookup · Tracking · Command Center
                    Decisions · Wheel · Validation · Portfolio · Signals · Universe · Settings
```

Research and analysis only. **No order placement** anywhere; the paper book
records fills you entered by hand in another account.

---

## 2. Repository layout

```
D:\csp\
├── launch.py              python launch.py -> scheduler worker (Phase 19) + streamlit run app/main.py
├── config.yaml            every threshold, weight and limit (section 7)
├── requirements.txt       pinned floor versions; install into .venv
├── .env / .env.example    credentials -- the ONLY .env read (core/env.py)
├── examples/              saved ScanRequest JSON files (Phase 11)
├── config/                versioned hand-maintained inputs (Phase 9)
│   ├── macro_calendar.yaml  FOMC / CPI / NFP dates, sourced
│   ├── user_settings.yaml   account profiles (roth_ira / traditional_ira / taxable since Phase 17, placeholder values until entered), weight presets (Settings page) and saved scan requests (Screener)
│   └── universe.csv         snapshot of the registry, rewritten on every change
├── core/                  cross-cutting infrastructure
│   ├── paths.py           every path; no module builds one from a literal
│   ├── env.py             credentials, TastyTrade token rotation
│   ├── market_calendar.py sessions, holidays, session blocks
│   ├── progress.py        stage reporters (console + Streamlit)
│   ├── freshness.py       cache ages vs config thresholds (Phase 8)
│   └── user_settings.py   user account profiles, ranking-weight presets, saved scan requests (config/user_settings.yaml)
├── data_sources/          everything that talks to an external API
│   ├── universe.py        the universe registry + symbol mapping (Phase 9)
│   ├── yfinance_sync.py   daily bars (raw, batched), earnings, dividends
│   ├── tasty_metrics.py   TastyTrade /market-metrics (Phase 9)
│   ├── events.py          events table + event policy check (Phase 9)
│   ├── reference.py       Treasury rates, VIX complex
│   ├── chains.py          TastyTrade chain capture
│   ├── chain_archive.py   daily full-universe snapshot into data/chain_archive (Phase 18)
│   ├── tastytrade_client.py  the one entry into vendor/tastytrade
│   └── massive_sync.py    1-minute archive sync (nightly only)
├── analytics/             pure analysis (section 5)
├── pipeline/
│   ├── run.py             the activation button
│   ├── results.py         persisted run tables; latest_run() (Phase 8)
│   ├── retention.py       run / chain-block pruning plan (Phase 18; scripts/prune.py)
│   └── scheduler.py       the worker: timed mark / scan_and_log / observe / archive / nightly (Phase 19)
├── app/
│   ├── main.py            navigation (st.navigation)
│   ├── theme.py           palette shared by CSS and Plotly
│   ├── components/        charts.py, formatting.py, run_state.py, status.py
│   └── pages/             8_Screener (landing), 9_Trade_Detail, 0_Command_Center … 7_Settings
├── scripts/               CLI entry points (section 8)
├── tests/                 test_foundation, test_phase3 … test_phase18 (+ fixtures/csp_golden)
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
| same → `market_metrics` | `data_sources/tasty_metrics.py` | One row per symbol per day: IV index, IVR (tos and tw), IVP, 30-day IV/HV, liquidity rating, beta, expected earnings, per-expiration IV with settlement type. Stored raw (the tos IVR is unbounded, e.g. 1.11); consumers clamp to [0, 1] (Phase 17). `previous()` gives each symbol's prior snapshot (regime hysteresis). |
| same → `events` | `data_sources/events.py` | Derived each run: earnings (merged sources), ex-dividend (history + projection), splits, FOMC/CPI/NFP, OPEX, quad witching. |
| `data/universe_daily.duckdb` → `daily_bars_raw` | `yfinance_sync.sync_daily` | Raw daily OHLCV from yfinance `auto_adjust=False` (Close is split-adjusted, not dividend-adjusted), Yahoo `adj_close`, dividends, splits. One row per ticker/date, under the canonical symbol (XSP is stored as ^SPX × 0.1). |
| same → `daily_bars`, `ticker_meta` | `scripts/01` | Legacy daily bars resampled from the 1-minute archive (split-adjusted). Fallback only. |
| `data/technicals.duckdb` → `indicator_latest`, `level_stats`, `oscillator_stats`, `support_latest` | `analytics/technical_study.py` (pipeline `technicals` stage) | Derived nightly: the last row of every daily and weekly indicator plus trend state (long format); the level-respect event study per symbol × level × horizon × slope regime; RSI-extreme forward returns; the support map at study time (Phase 11, read by the underlying ranking). Safe to delete. |
| `data/raw_1m_cache.duckdb` → `bars_1m` | `scripts/06` | Normalised 1-minute bars for the universe; intraday RV, and `gaps.py` only with `signals.gap_source: 1m`/`auto` (Phase 17: gaps default to daily bars; the archive ends 2026-06-30). |
| `data/chains/<session_block>/` | `data_sources/chains.py` | Chain snapshots: `<T>_chain.parquet` + `<T>_underlying.parquet`, keyed by session block (`2026-09-25_rth_14`, `_pre`, `_post`, `_closed`; a whole weekend shares one `_closed` block). Since Phase 11 rows carry `root_symbol`, `settlement_type`, `expiration_type` (SPX has SPXW PM and SPX AM roots; `Regular` = the monthly, read by `chain_utils.monthly_expirations`), only strikes inside the strike window, and the underlying file records the `dte_min`/`dte_max` captured, the IV that set the window, the expirations the subscription cap dropped (a later request needing one re-pulls) and, since Phase 17, any `extra_call_bands` of the spec widening. |
| `data/stage3_chains/<date>/` | `scripts/04` | Legacy chain snapshots; still read by `iv_history.py`. |
| `data/iv_history.duckdb` | `analytics/iv_history.py` | IV observations per ticker/block, rolled up from snapshots. |
| `data/reference/*.parquet` | `reference.py`, `yfinance_sync.py` | `treasury_rates`, `vol_indices`, `earnings`, `dividends`. |
| `data/trade_log.duckdb` → `tracking_observations`, `position_marks` | `analytics/tracking.py` (Phase 18) | Every re-sighting of a logged trade (run, rank, price, probabilities); every tracking mark (time, session block, spot, per-leg quotes/IV/IV-from-mid/Greeks as JSON, mark/natural, P&L, % of max, best/worst, DTE, probabilities from now, verdict + reason, IV rank/pct, VIX ratio, trend, RSI, P&L change split into delta/gamma/theta/vega/residual). |
| `data/outlook/` | `analytics/outlook.py` (Phase 20) | `model.json` (the pooled logistic fit per horizon and event), `latest.parquet` and dated copies (the live dials per symbol x horizon: probabilities per model, scores, bands, confidence, the Direction explanation, IV vs forecast RV, G vs H/T downside); `data/validation/outlook_skill.parquet` (walk-forward Brier skill per symbol, horizon, event and component, with the pooled-shrunk skill). |
| `data/scheduler/` | `pipeline/scheduler.py` (Phase 19) | `heartbeat.json` (pid, time, state, next slots), `history.jsonl` (one line per slot: ok / failed / missed / skipped / refused, message, run id), `worker.log`, `worker.lock`. |
| `data/chain_archive/<date>/` | `data_sources/chain_archive.py` (Phase 18) | The daily full-universe chain + underlying snapshot (0–60 DTE, strike-filtered) and a `manifest.json`; kept forever -- our own option-price history. |
| `data/trade_log.duckdb` | `analytics/paper.py` | The paper book: `cycles`, `paper_positions` (one row per position/package: short `strike`, `long_strike`/`width` for a spread, `collateral` = BPR, package quote, `rolled_from`/`rolls_used`, best profit seen), `paper_legs` (Phase 15: one row per leg; pre-Phase-15 rows migrated to one short-put leg on connect), `paper_marks` (marks while open: pipeline and manual), `paper_predictions` (what the engine claimed at entry, per metric and model), `share_lots`. Phase 18 columns on `paper_positions`: `book` (`taken` = a real trade, counted by capacity/exposure/correlation; `tracked` = a forward test at the modelled fill, never counted, no wheel cycle or share lot), `sample` (top / control / manual), `dedupe_key`, `trade_id`, `rank_at_log`, `preset`, `entry_spot`, `entry_context` (JSON), `source_row` (JSON, for promote), `promoted_from`, `logged_at`, and the outcomes `hold_status` / `hold_pnl` / `managed_pnl` / `managed_exit_date` / `managed_rule`. Phase 19: `account_profile` / `profile_nlv` (the profile a row was sized against), `sized_contracts`, `dollar_pnl_valid` (False for tracked rows sized against the research default or a placeholder profile: out of dollar reports, in probability and %-of-max reports), `flags` (e.g. `pre_fix_bars`), `hold_pnl_per_contract` / `managed_pnl_per_contract`; `position_marks.pnl_per_contract`. (The legacy `positions` table is left in place, unused.) |
| `data/runs/<id>/` | `pipeline/run.py`, `pipeline/results.py` | `manifest.json` (with the scan request), `candidates.parquet`, `positions.parquet`, `underlyings.parquet` (the ranking, Phase 11), `prob_policies` / `prob_metrics` / `prob_curves.parquet` (the probability engine per trade_id and model, Phase 13), `strategies.parquet` / `strategy_conditions.parquet` (the Phase 16 recommender, when the request has `specs` or `recommend`). |
| `strategies/*.yaml` | hand-written, versioned | Phase 16 strategy specs (legs, selectors, expirations, entry conditions, exit policy, margin class); format in `analytics/strategy_spec.py`. |
| `data/validation/` | `scripts/validate_prob_engine.py`, `scripts/backtest_pcs.py` | Walk-forward calibration of the probability engine: trades, calibration bins, summary JSON (per DTE; spreads as `prob_engine_pcs_*`, Phase 15). The PCS rule backtest: `pcs_backtest_sweep` / `_folds.parquet`, `pcs_backtest_summary.json` (Phase 15). |

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

**Completed sessions only** (Phase 18 fix): the sync's watermark is the last completed session (16:15 ET cutoff) and later bars are never stored -- an RTH run used to store today's partial bar and skip the ticker afterwards.

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

`python pipeline/run.py [--quick] [--tickers A,B] [--force-chains] [--request x.json]`,
or the **Run** button on Command Center, which calls the same `run()` with
the default request.

**Scan request (Phase 11).** Every run answers an
`analytics.scan_request.ScanRequest`: strategies (csp/pcs), DTE range or
targets, risk mode (`delta_range` | `min_pop` | `max_loss_per_trade` |
`max_pct_capital`), spread widths, profit targets, account profile,
universe, top N, event-policy overrides, strike rule. `--request` loads one
from JSON (see `examples/`); otherwise `ScanRequest.default()` reproduces
the config entry window (CSP, `management.entry` DTE and delta band). The
request is embedded in `manifest.json`. `--tickers` narrows both the data
stages and the request's universe.

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
| technicals | indicators, trend state, level-respect and RSI studies, support map → `data/technicals.duckdb` (~2.4 s per symbol) | symbol already studied through its latest bar |
| probabilities (inside analyse) | `probabilities.run_sheet`: models G/H/T on every candidate row, blend, per-policy EV net of fees; re-ranks the sheet by blended EV per day on BPR (~20 s for 100-330 trades) | — |
| rank_underlyings | `underlying_rank.rank`: every symbol in the request's universe scored without chains; top N + held names become the chain targets (~3 s) | — (runs in `--quick` too) |
| chains | TastyTrade capture for the chain targets only: DTE [request min, request max + roll buffer] (held names [0, ≥60]), strikes inside the EM window, index chains included; Phase 17: for a request with specs/recommend, `chain_capture.widen_for_specs` (PMCC) adds a deep-ITM call band at the back-leg expirations for names whose entry trend holds (`extra_subscriptions` per ticker in the manifest) | snapshot current for this session block AND covering the requested DTE window |
| analyse | regime, capacity, open-position management, `evaluate_universe(request=…)` over the analysable targets (CSP rows for physically settled names; PCS rows, indices included, when the request asks for PCS) (events check with request overrides, IVR/IVP on every row) → `select_sheet` (every accepted strike, `best_per_ticker`) → `portfolio.select` over the best rows, stress, wheel (covered calls, rolls). Roadmap stages construct → probabilities → rank_trades live here for CSP until Phases 12–13. A request without CSP skips trade construction. | — |

`--data-only` runs up to and including technicals, then stops. It is meant to be
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
  Since Phase 11 the sheet is not cut to one row per ticker: `selected` =
  `best_per_ticker` (the top accepted strike of each ticker, no run cap),
  and only those rows go to portfolio construction.
  `latest_run()` returns the newest *finished* run. Runs from before Phase 8
  have only the manifest; their proposals are rebuilt from it and
  `has_full_sheet` is False.

---

## 5. Analytics engine

Pure Python, config-driven, no Streamlit imports. Grouped by job:

| Job | Modules |
|---|---|
| Probability engine (Phase 13) | `prob_engine.py` (G: GBM at the short leg's IV; H: vol-conditioned 5-day block bootstrap; T: H from days in a similar trend/RSI/support state, relaxed then flagged; daily Black-Scholes repricing, sticky strike, optional IV reversion, earnings crush; P(reach X% by day d) curves, touch / assignment / max loss / roll trigger, per-policy EV, days and annualised return net of fees; blend. Phase 17: loss-stop policies (`stop_kx`, `close_X_stop_kx[_or_21dte]`, `p_stopped`) triggered on each day's low/high -- bootstrapped real extremes for H/T, a Brownian bridge for G -- filled at the stop level or at a close already through it; H/T leg IVs follow spot by the spot-vol beta (index d ln VIX / d ln SPY scaled by rho x vol ratio); `managed_policy_name` + `headline_policy: shipped`), `probabilities.py` (runs it over a sheet, `shipped_rules` per row, headline policy, ranking, `SORTS`) |
| Probability of outcome | `moves.py` (empirical P(breach)/P(touch), vol-conditioned, effective n), `options_math.py` (Black-Scholes, Greeks, IV solve), `volatility.py` (close-to-close, Parkinson, Garman-Klass, intraday RV) |
| Market pricing | `vrp.py` (IV/RV), `skew.py` (put skew, term structure), `surface.py` (per-snapshot vol surface, forward), `iv_history.py` (own IV rank, needs 10+ captures) |
| Risk context | `regime.py` (VIX term-structure gate), `gaps.py` (overnight gap risk from daily bars since Phase 17, `signals.gap_source`; corporate-action seam filter for the 1-minute path), `technicals.py` (SMA/RSI/52-week), `earnings_history.py` (past report reactions: gap, close-to-close, two-session, ATR multiple; implied move once snapshots exist) |
| Universe | `universe_screen.py` (Stage 1 on yfinance; drawdown over `drawdown_lookback_years`), `bars.py` (weekly resample, no-lookahead alignment via `weekly_positions`) |
| Scan (Phase 11) | `scan_request.py` (`ScanRequest`: validation, JSON, DTE window/targets, chain window, `resolve_universe`), `underlying_rank.py` (chain-free score: IV rank, IV/RV at the request DTE, liquidity, trend, strong support in EM units, drawdown; event and capital gates), `rank_calibration.py` (Phase 14: point-in-time IC of each testable component vs the share of premium a 1-EM put kept; `scripts/calibrate_rank_weights.py`) |
| Technicals (Phase 10) | `indicators.py` (SMA/EMA 9–200, RSI, ATR, Bollinger, MACD, ADX, 52-week, volume ratio; daily and weekly as `w_*`), `trend_state.py` (uptrend/range/downtrend per day), `level_respect.py` (MA test events → held/bounced/broke, pierce depth, Wilson CIs, **block-bootstrap placebo**, slope split, `support_map`), `oscillator_study.py` (RSI-extreme episodes vs baseline), `technical_study.py` (cache and pipeline stage) |
| Outlook (Phase 20) | `outlook.py` (Direction / Range / Volatility dials on a 3-60 day grid from the engine's H/T paths and a pooled ridge logistic on point-in-time price features; walk-forward Brier skill vs each stock's base rate; skill-shrunk scores, bands, confidence; `annotate` / `filter_rows` for the Screener; `trend_class` for the recommender). Display and filter only. |
| Trade construction | `candidates.py` (the sheet: `evaluate_universe(request=…)` builds CSP and PCS rows per ticker, `trade_id`, `select_sheet`, census), `strategies/` package (Phase 12: `base.py` Leg/Position -- payoff, value, max profit/loss, breakevens, BPR, net Greeks; `csp.py` the CSP evaluation moved verbatim from candidates, numbers pinned by a golden test; `pcs.py` put credit spreads -- strike rules, width ladder, tiers, empirical POP/P(max loss)/EV; Phase 17: also built at the nearest monthly for targets from `prefer_monthly_from_dte`, long leg snapped to open interest within `long_leg_oi_snap_band`; `context.py` EM, support, liquidity and premium flags on every row), `costs.py` (tastytrade fees, single- and multi-leg fill models, vertical exit fees per outcome), `sizing.py` (capital and liquidity caps; `max_contracts_for_position` caps every leg, with `liquidity_limits.spread_legs` floors for multi-leg positions since Phase 17; account profiles) |
| Multi-strategy (Phase 16) | `strategy_spec.py` (YAML specs: validation, `premium` credit/debit, `applies`, `condition_matrix`; Phase 17 IV regime on the clamped IV percentile with hysteresis, soft except at the extremes, `regime_fit`), `strategies/resolver.py` (spec + chain -> priced, margined, sized positions with empirical POP/EV and gates; model IVs implied from each leg's mid; Phase 17: roles from 30 DTE take the nearest monthly, spread-leg liquidity floors), `strategies/base.py` (stock legs; several expirations, later legs at the forward vol), `margin.py` (BPR per class: cash-secured, defined risk, covered, naked broker formula; profile permissions), `recommender.py` (conditions -> applicable specs -> resolve -> probability engine -> rank by blended EV/day/BPR of each spec's own exit rules; in-regime rows first; `research_only` for naked specs on a `naked_research_only` profile), `costs.multi_leg_fill`, `exit_rules.evaluate_spec_position` |
| Options analytics (Phase 12) | `expected_move.py` (tastytrade platform formula 0.6 straddle + 0.3 / 0.1 strangles, IV method, 0.85 x straddle; bands; EM distance; historical containment), `liquidity.py` (per-leg OI / volume / spread, fillability 0-1, weakest leg, OI walls) |
| Tracking (Phase 18) | `tracking.py` (log rows as tracked with dedupe + observations; the C.3 sample -- top K passing, M random passing below, M near-miss rejects; promote; `update`: targeted chain refresh of the positions' expirations, marks, probabilities from now, `verdict` (the one management decision function, also used by the pipeline), Greeks attribution between marks with IV implied from each leg's mid; `expire_due`: settle from the expiry close, hold and managed outcomes; `entry_vs_now`) |
| Book | `portfolio.py` (correlation clusters, marginal risk, simultaneous-assignment stress), `paper.py` (multi-leg paper book: accept CSP/PCS with net or leg fills, close / settle / roll, marks, stored predictions, slippage, performance by strategy), `book.py` (Phase 15: the open book marked on the latest chains -- mark, natural, unrealised, Greeks from the chain or Black-Scholes, 1-year beta on SPY, beta-weighted delta, theta/day, vega, BPR utilisation, event calendar) |
| Management | `exit_rules.py` (CSP: hold/close/roll/accept, net of fees; Phase 15 PCS: value floor, loss stop at k x credit, breach/delta roll trigger, profit target above the hold horizon net of fees, optional time stop, the empirical expected-value test; `spread_roll_candidates` for net-credit rolls), `roll_engine.py`, `covered_call.py` |
| Trade Detail (Phase 14) | `trade_detail.py` (no Streamlit: the persisted row as plain python, plain-English thesis/verdict/risk flags, payoff at expiry and T+n, net Greeks over time, price × IV scenario grid, empirical vs lognormal terminal distributions, EM cones, chain window around the legs, management plan, the Screener grid, filters and Excel export) |
| Validation | `wheel_backtest.py` (full wheel cycles, synthetic BS pricing; since Phase 15 on the price basis with explicit dividend credits while shares are held), `walkforward.py`, `regimes.py`, `calibration.py` (POP reliability per strategy, P(reach X%) hit/miss/censored scoring from marks, package-quote fill calibration), `pcs_backtest.py` (Phase 15: one spread at a time on real paths, synthetic skewed BS prices, the exit_rules order of rules, grid sweep and walk-forward against a fixed baseline) |
| Shared | `chain_utils.py`, `strategies/__init__.py` (`STRATEGIES` label registry, import-compatible), `config.py` (shim over `core.paths`) |

Gates are **rejections, not penalties**. Every probability reports its
sample size.

---

## 6. Application

`app/main.py` defines navigation explicitly:

| Page | Shows | Run data from |
|---|---|---|
| **Screener** (default, Phase 14) | session banner + credentials, regime and a data-freshness line; the full scan-request form (Phase 17: the account profile must be chosen explicitly -- no silent $3M default; placeholder profiles warn) (strategy, DTE range or targets, risk mode + value, widths, profit targets, profile, universe, top N, ranking weights, event overrides, strike rule) with **saved requests** (`scan_presets`) and the example files; **Run** with stage progress; the **results grid** (every candidate, blended probabilities as progress bars, filters, best-per-ticker and group-by-ticker toggles, CSV/Excel); selecting a row opens Trade Detail with `?run=&trade=` | `active_run()` + `candidates.parquet` |
| **Trade Detail** (Phase 14) | one trade from a persisted run, by query parameters (else the Screener's selection, else the top row): Summary (thesis, verdict, why this strike, risk flags) · Chart (daily/weekly candles, MAs, respected levels, strike lines, IV and straddle EM cones, events) · Expected move (three EM methods, containment, empirical vs lognormal terminal distribution, P(below) table) · Payoff (expiry + T+n, any legs) · Probabilities (target × model, curves, policy comparison under any model) · Greeks (now, over time, price × IV heatmap) · Chain & liquidity · Management plan (+ CSP roll and covered-call previews) · Context (IV, regime, earnings reactions, gap risk, correlation with the book) · Accept (CSP or PCS → paper book, net credit or leg fills; "Send to Decisions") | `load_run(run)` + prob tables + chains, bars, levels |
| Command Center | session banner, **Run** button with a scan-request form (strategies, DTE, profile, ranking weights, top N or all) and stage progress, open-position decisions (CSPs and spreads; spread roll candidates for a net credit), capacity | `active_run()` |
| Decisions | the **Screener selection first** (Phase 14); the run's scan request; proposed trades with a **probability panel** (G/H/T/blend table, management policies net of fees, P(reach X% by day) curves) and a **sort selector** on the full sheet (CSP and PCS cards with legs, max loss, POP, P(max loss), credit/width, each with an accept form), accept with actual fill → paper book; the book by strategy with forms to mark an outcome (expired / closed / assigned / settled at a price), roll (two linked positions) and record a mark; expander with **every evaluated strike** and why each was rejected, with a **best per ticker** toggle; the **underlying ranking** with component scores and exclusions The paper book's own sections (ledger, performance, outcome / roll / mark forms, share lots, calibration) moved to Tracking on 2026-09-29; a button links there. | `active_run()` + `candidates.parquet` + `underlyings.parquet` |
| Wheel | defensive rolls, covered calls against assigned lots, wheel backtest (price basis + dividends, Phase 15) | `active_run()` |
| Validation | calibration (POP by strategy, **P(reach X%) predicted vs observed** from the book, Phase 15), slippage, IV coverage, walk-forward, **probability engine predicted vs observed** (Phase 13; spreads too), **ranking-component ICs** (Phase 14), **PCS rule backtest** (Phase 15) | disk |
| Portfolio | **open book** (Phase 15: marks, P&L, beta-weighted delta, theta/day, vega, BPR utilisation, event calendar), exposure, clusters, stress, correlation, capital recycling | `paper` + chains, `active_run()` |
| Signals | **Levels** (universe chance check, support map with %/ATR/EM distances, every level ranked by edge CI, chart of tests, RSI extremes), gap risk, skew, term structure | technicals cache, disk |
| **Strategies** (Phase 16) | Recommend (latest run's recommender tables or a scan of stored chains: grid, payoff, legs, probabilities, record to the book), Library (specs and profile permissions), Condition matrix | `strategies.parquet`, chains |
| **Tracking** (Phase 18) | book filter (tracked / taken / both); **Update now** (targeted chain pull + a mark per open position), **Expire due**, **Archive chains**; open positions with mark, P&L, % of max, best/worst, P(target) at entry vs now, POP now, verdict; per position: entry vs now, marks and probabilities over time, P&L attribution, observations, **Promote** to taken; closed positions with hold-to-expiry vs managed P&L; the archive list. The Screener's **Log mode** (multi-row: Log selected / every passing row shown / Log all = top K + control) and the Strategies grid (multi-row, Log selected) feed it Since 2026-09-29 also the **paper book** (performance with dollar totals over real-profile rows only, the ledger, **Mark an outcome / Roll a position / Record a mark**, share lots) and **Is the model telling the truth?** (fill quality, probability calibration), from `app/components/paper_book.py`. | `trade_log.duckdb`, chains |
| **Symbol Lookup** (Phase 20B) | any ticker: checked at Yahoo and TastyTrade, registered tag `adhoc` (outside `universe: all`, the nightly jobs, the archive and every auto preset until **Add to universe**), then bars / metrics / events, technicals, the Outlook (pooled skill noted), a targeted chain and CSP + put-spread candidates plus the recommender (`pipeline/lookup.py`); gauges with a horizon selector, trend, RSI, IV rank / percentile, IV / forecast ratio, the support map, events, the trade grid (a row opens Trade Detail; Track rows logs sample `lookup`). Refused while a scheduled job runs or is due within 15 min. | a `kind: lookup` run folder (skipped by `latest_run`) |
| Settings | user account profiles (capital, caps, permissions incl. account type, naked approval, naked research-only, placeholder flag) and ranking-weight presets; **Schedule** tab (Phase 19): worker status with Start/Stop, today's slots, job history, the timetable, auto presets (K, M, daily cap, observe hourly; explicit and placeholder checks); saved to `config/user_settings.yaml` | disk, `data/scheduler/` |
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
| `liquidity_limits` | % of OI / volume / ADV, OI and volume floors; `spread_legs` floors (Phase 17) | `sizing.py`, `candidates.py`, `pcs.py`, `resolver.py` |
| `execution` | signal_only mode, manual fill override | Decisions, `paper.py` |
| `costs` | tastytrade fee schedule, slippage fraction | `costs.py` |
| `chain_capture` | DTE windows, RTH refresh interval; roll buffer, put/call strike windows in EM, minimum window %, per-symbol subscription cap (Phase 11); `widen_for_specs`, `widen_delta_pad` (Phase 17) | `chains.py`, `scan_request.py` |
| `massive` | archive pacing | `massive_sync.py` |
| `management.entry/exit/defense/covered_call` | entry gates and DTE band, exit test, roll limits, call rules | `candidates.py`, `exit_rules.py`, `roll_engine.py`, `covered_call.py` |
| `management.spread` | Phase 15 PCS rules: profit target, loss-stop multiple, 21-DTE time stop, roll triggers, max rolls, roll-out window, value floor | `exit_rules.py`, `trade_detail.management_plan` |
| `signals` | skew / gap / backwardation gates; `gap_source` (Phase 17: daily / 1m / auto) | `candidates.py`, `gaps.py` |
| `portfolio` | cluster and correlation limits, stress horizon | `portfolio.py` |
| `regime` | VIX term-structure thresholds and size multiplier | `regime.py` |
| `move_analysis` | horizons, lookbacks, min observations | `moves.py`, `weekly_move_analysis` |
| `paper_trading` | paper book defaults | `paper.py` |
| `freshness` | cache-age warning thresholds, incl. market metrics, events and technicals | `core/freshness.py` |
| `indicators` | MA lengths, RSI/ATR/ADX windows, Bollinger, MACD, volume window | `analytics/indicators.py` |
| `levels` | studied MA lengths, lookback, band / tolerance / bounce / re-arm (ATR multiples), horizons, min n, placebo replicates and block length, slope lookback, recency half-life, "strong" floor, EM days | `analytics/level_respect.py` |
| `trend_state` | ADX floor, EMA50 slope window | `analytics/trend_state.py` |
| `oscillator_study` | RSI thresholds, horizons, lookback | `analytics/oscillator_study.py` |
| `event_policy` | per event type: action, days before/after, strategies, asset classes | `data_sources/events.py` |
| `events` | OPEX horizon, earnings disagreement tolerance, calendar-health minimum | `data_sources/events.py` |
| `market_metrics` | request batch size | `data_sources/tasty_metrics.py` |
| `expected_move` | default EM method (tastytrade / iv / straddle), containment lookback | `analytics/expected_move.py` |
| `liquidity_score` | fillability scale (spread %, $ floor, OI and volume log scales), OI-wall multiple | `analytics/liquidity.py` |
| `pcs` | minimum credit, credit/width floor (a warning), short-leg IV/RV gate, tier labels, `long_leg_oi_snap_band` (Phase 17) | `analytics/strategies/pcs.py` |
| `prob_engine` | paths, seed, block length, lookback, vol band, minimum matching days, blend weights, IV reversion, earnings crush, time stop, headline policy (`shipped` since Phase 17), T buckets; Phase 17 `intraday_stops`, `spot_vol`, `spot_vol_index_beta`, lookbacks, `iv_clip` | `analytics/prob_engine.py` |
| `premium_flags` | IVP and IV/RV thresholds for the premium-opportunity flag | `analytics/strategies/context.py` |
| `scan_defaults` | the default `ScanRequest` (null DTE/delta = `management.entry`), DTE-target tolerance; Phase 15: spreads 4% of spot wide (`spread_width_pct`) at the expiration nearest 45 DTE (`pcs_dte_targets`, `pcs_dte_tolerance_days`); Phase 17 `prefer_monthly_from_dte`. Only `ScanRequest.default()` reads these -- a JSON request's missing fields take the dataclass defaults | `analytics/scan_request.py` |
| `underlying_rank` | weight presets and the default preset (`calibrated` since Phase 17; users add more on Settings), trend scores, IV/RV scale, liquidity-value log scale, support EM band, drawdown floor, metrics age | `analytics/underlying_rank.py` |
| `account_profiles` | shipped profiles (`default`; `naked_approval` since Phase 16); user profiles live in `config/user_settings.yaml` | `core/user_settings.py`, `analytics/sizing.py` |
| `recommender` | IV regime (Phase 17: measure `ivp`, thresholds, hysteresis, soft, credit_min / debit_max extremes), engine paths per trade | `analytics/recommender.py`, `strategy_spec.py` |
| `stage1_thresholds.min_rv_broad_index` | RV floor for `broad_index_*` ETFs (Phase 17: 0.08) | `analytics/universe_screen.py` |
| `margin` | naked requirement percentages | `analytics/margin.py` |
| `tracking` | top K, control M, engine paths per update (Phase 18) | `analytics/tracking.py` |
| `archive` | DTE window and time of the daily chain archive (Phase 18) | `data_sources/chain_archive.py` |
| `retention` | days before run prob tables go and chain blocks thin (Phase 18) | `pipeline/retention.py` |
| `outlook` | horizon grid, walk-forward window, ridge penalty, skill shrink thresholds and prior, Volatility scale, engine paths, refit age, what the recommender's trend condition reads (Phase 20) | `analytics/outlook.py` |
| `schedule` | the worker's timetable (ET), grace, poll; auto presets live in `user_settings.yaml -> schedule.auto_presets` (Phase 19) | `pipeline/scheduler.py` |
| `liquidity_limits.spread_legs.warn_option_volume` | a spread leg under this day volume is a row warning, not a gate (Phase 19) | `analytics/sizing.py` |

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
| `python scripts/validate_prob_engine.py [--strategy pcs]` | probability engine walk-forward, puts or spreads |
| `python scripts/archive_chains.py` | the daily chain archive (Phase 18; 15:45 ET) |
| `python scripts/prune.py [--apply]` | run / chain retention plan (Phase 18) |
| `python scripts/validate_outlook.py` | Outlook walk-forward skill, final fit and live dials (Phase 20; ~1 min) |
| `python pipeline/scheduler.py [--plan DATE / --status / --run JOB --preset NAME]` | the scheduler worker, or one job now (Phase 19) |
| `python scripts/scheduler_task.py install / status / remove` | Task Scheduler entry that starts the worker at logon (Phase 19) |
| `python scripts/launchd_agent.py install / status / remove` | macOS: the LaunchAgent counterpart (docs/MACOS.md) |
| `python scripts/health_check.py [--days 7]` | weekly scheduler health: per-job status and durations, the mark split into chains (per ticker) and marks (per position), jobs over 15 min, a proposed fix for a slow mark |
| `python pipeline/lookup.py SYMBOL [--profile P]` | Symbol Lookup from the command line (Phase 20B) |
| `python scripts/flag_positions.py FLAG --runs-before ISO / --ids ...` | tag logged positions with a data-quality flag (Phase 19) |
| `python scripts/backtest_pcs.py` | PCS rule backtest and walk-forward (~6 min for 4 ETFs x 768 rule sets) |

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
- **Backtest option prices ignore the real surface.** The PCS backtest adds
  a linear put skew to the RV proxy, but a spread's credit is a difference of
  two synthetic prices, so its return LEVEL is indicative only; compare rule
  sets, not absolute numbers. Rules fire on daily closes; no rolls, no early
  assignment (PHASE15_SUMMARY.md §4).
- **IV rank from own captures** needs 10+ capture dates per ticker. The
  TastyTrade IVR/IVP is informational on every candidate, and since Phase 11
  a component of the underlying ranking; it is not a trade gate.
- **Underlying-ranking weights are only partly validated** (Phase 14). Only
  trend, drawdown and proxies for iv_rank and support can be rebuilt
  point-in-time; iv_rv and liquidity cannot. Only the iv_rank proxy (and
  weakly drawdown) predicted outcomes; the `calibrated` preset follows that
  and is the default since Phase 17. The default request still pulls chains
  for every eligible name.
- **The default CSP window (5-10 DTE) holds no Friday on a Monday** (Phase 17
  live run): Oct 2 is 4 DTE, Oct 9 is 11, so only names with Monday/Wednesday
  expirations (SPY, QQQ, IWM, a few mega-caps) produce rows. Open decision
  (PHASE17_SUMMARY.md).
- **Stops in the engine are approximations.** Intraday extremes are daily
  highs/lows (H/T) or a Brownian bridge (G); a stop fills at its level unless
  the close is through it (no open-gap fill); the spot-vol beta scales the
  whole leg IV (no skew dynamics) and applies to H/T only.
- **Moving-average "support" is mostly chance in this universe** (Phase 10:
  median edge over the bootstrap placebo +0.4 pts; 32 strong levels vs ~26
  expected by chance). Treat a strong level as a hypothesis; the Levels tab
  says so. Trend state and level stats are informational until Phases 11–13.
- **Indices take PCS only.** Index chains (SPX, XSP, NDX, RUT) are captured
  and spreads built for PCS requests; CSP never applies to them.
- **P(reach X%) calibration from the book is a lower bound.** Marks are
  sampled (one per pipeline run plus manual marks), so a target touched
  between marks is missed; positions closed early without reaching a target
  are censored, not misses.
- **Tracking marks are sampled.** A rule that would have fired between two
  marks is caught at the next one, so managed outcomes and P(reach X%)
  scoring are approximate until the Phase 19 scheduler marks hourly.
  dxFeed's Greeks events refresh less often than quotes (vega attribution
  therefore uses IV implied from each leg's mid). Auto-expiry settles on the
  daily close (an AM-settled index expiry's real settlement differs), and a
  calendar or diagonal whose front leg expired is left for a manual close.
- **Account profiles are placeholders** until Tom enters the real values
  (Settings; the Screener warns while `placeholder: true`).
- **Probabilities are model outputs, not guarantees.** The engine's blend
  was within ~2 points of observed frequencies in a synthetic-price
  walk-forward (PHASE13_SUMMARY.md §3); G is a zero-edge baseline by
  construction; touch is measured on daily closes. The Phase 12 empirical
  POP/EV columns remain on every row for comparison.
- **Request widths are dollars.** On SPX's 25-point strikes a $1-$10 ladder
  snaps to one 25-wide spread (warned on the row); index requests need
  index-sized widths.
- **Implied-move history** for earnings reactions starts accumulating with the
  first daily metrics snapshot (2026-09-27).
- **The paper book records CSPs, put credit spreads and option-only strategy-spec
  positions.** Specs with a stock leg (buy-write) are priced but not recorded.
- **Multi-strategy probabilities are unvalidated** beyond short puts and put
  spreads; calendars and diagonals rest on a forward-vol term-structure
  assumption (PHASE16_SUMMARY.md §3), flagged on their rows. A physically settled spread
  that finishes between its strikes becomes a share lot in a new wheel cycle,
  like an assigned CSP.
- **European Black-Scholes** throughout; no early-exercise modelling.
- **Earnings dates** come from free yfinance data (roughly 90% reliable). An
  unknown date fails safe (blocks) unless the calendar as a whole is
  unhealthy.
