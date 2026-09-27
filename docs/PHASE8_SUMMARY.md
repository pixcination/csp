# Phase 8 summary — baseline, version control, consolidation, data-basis fixes

Date: 2026-09-27 (Sunday; market closed, marks from the Fri 2026-09-25 close).
Roadmap: [SCREENER_ROADMAP.md §C.0](SCREENER_ROADMAP.md). Architecture as it now
stands: [ARCHITECTURE.md](ARCHITECTURE.md).

## Decisions (confirmed by Tom)

| Question | Decision |
|---|---|
| GitHub remote | `https://github.com/pixcination/csp` (private), branch `main` |
| Legacy files | **moved** to `legacy/` (not deleted); their tests to `legacy/tests/` |
| `scripts/01–06` | **kept**, documented as "universe rebuild from the 1-minute archive" |
| Wheel backtest basis | stays on **total** return; explicit dividend credit deferred to Phase 15 |
| `daily_bars_tr` | **dropped** after migration; a backup copy is at `data/universe_daily.pre_phase8.duckdb` |
| Virtual environment | **created** at `.venv` (Python 3.13.1) |

## 1. Baseline (before any change)

| Check | Result |
|---|---|
| `scripts/preflight.py` | exit 0; warnings: no venv, 1-minute archive 25 days behind |
| `pytest tests -q` | **184 passed** |
| Headless AppTest | 10/10 pages clean |
| git | not a repository |

The baseline was committed untouched as the first commit (`16acfd1`) and pushed.

## 2. What changed

### 2.1 Version control
- `git init`, `main` → `origin`. `.gitignore` already excluded `.env`, `data/`,
  `*.duckdb`, `*.parquet`, venvs and caches. Added: `*.bak`, `.codebase-memory/`.
  Now **versioned** despite `output/*.csv`: the hand-edited
  `output/stage2_quality_tags_master.csv` and `output/final_universe.txt`.
- `.gitattributes`: `* text=auto eol=lf`.
- Staged content was scanned for secrets before the first commit; the only
  hit was a test placeholder.

### 2.2 Adjustment-vintage fix (§A.3.1)
- New table **`daily_bars_raw`**: yfinance `auto_adjust=False` (split-adjusted
  Close), `adj_close`, dividends, splits.
- **Full re-pull** of a ticker when the incremental window shows a new
  dividend, a new split, a late-posted action, or a restated close
  (`yfinance_sync.repull_reason`).
- **`load_daily(ticker, start, end, basis="price"|"total")`**. The total basis
  is derived locally from stored dividends (`total_return_factor`).
  `load_daily_total_return` is now a thin wrapper.
- `adjustment_check(ticker)`: local factor vs Yahoo `adj_close`.
- `scripts/migrate_phase8.py` did the one-time migration: backup → full
  re-pull of 61 tickers (1 m 19 s, 518,269 rows) → adjustment check (worst
  deviation **0.0033%**, KO 1987) → drop `daily_bars_tr` → migrate the legacy
  trade log.

### 2.3 Probability basis fix (§A.3.2)

| Basis | Callers |
|---|---|
| `price` | `candidates` (EV sheet, `basis_assessment`, and the `moves` breach probabilities it computes), `roll_engine`, `covered_call`, `gaps` (seam reference), `portfolio` (correlation, assignment stress), pipeline open-position evaluation, `weekly_move_analysis` |
| `total` | `wheel_backtest` via the Wheel page, `walkforward`, `regimes`, `scripts/sweep_universe.py` |

`vrp.py` and `technicals.py` take a frame from their caller, so they get the
caller's basis (price in every live path). `tests/test_phase8.py` pins the
table above by parsing the call sites.

A side benefit for `gaps.py`: the 1-minute archive is split-adjusted only.
Against the old dividend-adjusted reference, an ex-dividend drop on a
high-yield name disagreed by the dividend yield and could be excluded as a
"seam". Against the price basis, the two agree.

The stale `move_analysis.price_basis: total_return` key in `config.yaml`
(read by nothing) now documents the rule instead of contradicting it.

### 2.4 Persisted run results
- `candidates.evaluate_universe()` returns **every** evaluated strike;
  `select_sheet()` applies the previous best-per-ticker and cap logic.
  `build_decision_sheet()` is unchanged for callers.
- Each run writes `data/runs/<id>/candidates.parquet` (all rows including
  rejected; flags `accepted` / `selected` / `proposed`; rejection reasons) and
  `positions.parquet`.
- `pipeline.results.latest_run()` (skips unfinished runs), `load_run()`,
  `list_runs()`.
- `app/components/run_state.py`: Command Center, Decisions, Wheel and
  Portfolio use the latest run on disk when session state is empty, and
  always caption which run is shown and how old it is. Decisions also has an
  expander listing every evaluated strike and why it was rejected.

### 2.5 Legacy stack retired
Moved to `legacy/`: pages `1_Scanner`, `2_Ticker_Detail`, `3_Trade_Log`;
`analytics/scoring.py`, `backtest.py`, `trade_log.py`, `data_access.py`;
`app/components/jobs.py` (used only by Scanner). Imports were rewritten to
`legacy.*`, so `pytest legacy/tests` still runs. Navigation no longer lists
them.
- The `stage3_chains` reader that `iv_history.py` needed is now private to it
  (`_stage3_dates`, `_load_stage3`).
- Legacy trade log: **0 rows** in every table, so nothing needed migrating.
  `paper.migrate_legacy_trade_log()` exists, is idempotent and tested.
- `legacy/README.md` lists what replaced each file, and which
  `app/components/charts.py` builders the Trade Detail page (Phase 13/14)
  will reuse and how each must be generalised.

### 2.6 Preflight cache-age warnings
New `core/freshness.py`, with thresholds in `config.yaml → freshness`. It
measures ages from the data itself where cheap (last bar), and from file
modification time otherwise. On first run it flagged caches that preflight
had been reporting as OK:

| Cache | Age found |
|---|---|
| Daily bars | 5 weeks behind (last bar 2026-08-21) before the migration re-pull |
| Earnings calendar | 36 days |
| Dividend ex-dates | 108 days |
| Treasury rates / VIX indices | 36 days |
| Chain snapshots | 36 days |
| 1-minute cache | 88 days (last bar 2026-06-30) |

### 2.7 Other fixes found on the way

| Defect | Effect | Fix |
|---|---|---|
| **`RunLock` killed live runs on Windows.** `_pid_alive` used `os.kill(pid, 0)`, which on Windows calls `TerminateProcess(pid, 0)`. | Starting a second run (UI button, CLI, or the test suite) while one was running silently ended the first, with exit code 0. It happened to a Phase 8 pipeline run mid-capture. | `OpenProcess`/`GetExitCodeProcess` on Windows. Regression test spawns a child and checks it survives. The run-lock tests now use a temp directory, never the real lock. |
| **Open positions were read from the retired Trade Log.** `pipeline/run.py` read `trade_log.positions`, not the paper book. | Positions accepted via Decisions were never evaluated by the management engine, and never got the wider chain-capture window. | Reads `paper.list_positions(status="open")`; entry credit = actual fill, else modelled. |
| **Dividend ex-dates never refreshed.** Nothing called `sync_dividends`. | `dividends.parquet` was 108 days old, so the covered-call early-assignment warning projected from June data. | `write_dividends_from_raw()` rebuilds the file from `daily_bars_raw` after every daily stage, with no extra requests. It now covers 47 payers, latest ex-date 2026-09-21. |
| `requirements.txt` listed `massive-api` | A clean install failed; the package is `massive` on PyPI. | Corrected. |
| Newer Streamlit resolves `AppTest.from_file` relative to the caller | Ad-hoc page checks broke. | `scripts/check_pages.py` with absolute paths. |

## 3. P(breach) before / after (MO, T, KO, PBR)

Script: `docs/phase8/pbreach_compare.py`; data: `docs/phase8/pbreach_*.csv`.
Empirical terminal P(close ≤ strike), 10-year lookback, unconditional, every
start date, history truncated at 2026-08-21 in all three columns so only the
basis differs. *before* = the old `daily_bars_tr`; *after_total* = the new
raw table on the total basis; *after_price* = the new raw table on the price
basis (what the engine now uses).

| Ticker | Horizon (trading days) | OTM | Before | After (total) | **After (price)** | Change |
|---|---|---|---|---|---|---|
| MO | 7 | 5% | 7.44% | 7.44% | **8.56%** | +1.11 pts (+15%) |
| MO | 7 | 10% | 1.59% | 1.59% | **1.71%** | +0.12 pts (+8%) |
| MO | 21 | 5% | 17.43% | 17.43% | **19.86%** | +2.43 pts (+14%) |
| MO | 21 | 10% | 5.77% | 5.77% | **6.65%** | +0.88 pts (+15%) |
| T | 7 | 5% | 7.92% | 7.92% | **8.87%** | +0.96 pts (+12%) |
| T | 7 | 10% | 0.96% | 0.96% | **0.99%** | +0.04 pts (+4%) |
| T | 21 | 5% | 16.59% | 16.59% | **19.46%** | +2.87 pts (+17%) |
| T | 21 | 10% | 4.85% | 4.85% | **5.93%** | +1.07 pts (+22%) |
| KO | 7 | 5% | 2.71% | 2.71% | **2.90%** | +0.20 pts (+7%) |
| KO | 7 | 10% | 0.40% | 0.40% | **0.40%** | 0 |
| KO | 21 | 5% | 8.04% | 8.04% | **8.40%** | +0.36 pts (+4%) |
| KO | 21 | 10% | 1.47% | 1.47% | **1.63%** | +0.16 pts (+11%) |
| PBR | 7 | 5% | 18.27% | 18.27% | **19.42%** | +1.15 pts (+6%) |
| PBR | 7 | 10% | 5.01% | 5.01% | **5.65%** | +0.64 pts (+13%) |
| PBR | 21 | 5% | 26.14% | 26.14% | **28.81%** | +2.67 pts (+10%) |
| PBR | 21 | 10% | 13.53% | 13.53% | **15.08%** | +1.55 pts (+11%) |

**How to read it.**
1. **The basis error was real and one-sided.** On the traded price, assignment
   odds for these names are 4–22% higher in relative terms (up to +2.9
   points at 21 days). Every earlier EV, sizing and ranking for high-yield
   names was optimistic by about that much. The effect grows with horizon,
   because longer windows are more likely to contain an ex-date. That matters
   for the 30–45 DTE PCS work in later phases.
2. **The vintage bug was latent.** `after_total` equals `before` to every
   printed digit. A return-by-return comparison of the old table against the
   re-pulled one found **0 seam days in all 61 tickers** (worst difference
   0.0033%). The table had been built with full pulls, and no ex-date had yet
   fallen between the initial pull and the later incremental syncs. The first
   such dividend would have created the seam. It is now structurally
   prevented, and a test covers it (`test_new_dividend_triggers_a_full_repull_and_leaves_no_seam`).

## 4. Verification

| Check | Result |
|---|---|
| `pytest tests -q` | **218 passed** (184 before; 1 moved to `legacy/tests`; 35 new in `test_phase8.py`) |
| `pytest legacy/tests -q` | 1 passed |
| `scripts/check_pages.py` | **7/7 pages clean**; no legacy pages in navigation |
| `scripts/preflight.py` | exit 0. Remaining warnings: 1-minute archive 25 days behind, 1-minute cache 88 days old (`scripts/06`; needs the archive synced first) |
| Full pipeline run `20260927-143704-1da8` | 12 m 48 s, exit 0; chains for the `2026-09-25_closed` block; **223 strikes evaluated, 9 passed the gates, 6 selected, 3 proposed**: CNC 10/02 $60p ×13 (EV 16.4% annualised, P(OTM) 80%), BMY $61p ×8 (14.8%, 85%), C $130p ×9 (10.3%, 86%). The stress test flags all three as one correlated bet (2007-06-01 simultaneous assignment). Closed-session marks, not tradable quotes. |
| Persistence | `data/runs/20260927-143704-1da8/` holds `manifest.json`, `candidates.parquet` (223 rows) and `positions.parquet` |
| **Acceptance: restart → Decisions shows the last run** | Fresh AppTest session with empty state: caption *"Showing run `20260927-143704-1da8` · finished Sun 2026-09-27 14:49 · latest run on disk"*, the 3 proposals, and the expander *"Every strike evaluated in this run (223; 9 passed the gates)"*. Also automated as `test_decisions_page_shows_the_run_from_disk_after_a_restart`. |
| Acceptance: P(breach) table | section 3 |

## 5. Known limits

- **The wheel backtest has no explicit dividend credit.** It runs on the total
  basis, so strikes are placed against adjusted prices (Phase 15).
- **The 1-minute cache is still 88 days old.** Refreshing it needs the
  Massive archive sync and then `scripts/06`. Until then, `gaps.py` and
  intraday RV run on data through 2026-06-30, and preflight warns about it.
- **`chain_utils.nearest_target_delta_put` now has one caller**
  (`iv_history`). It stays where it is; Phase 12's strategy package is the
  place to fold it in.
- **`analytics/strategies.py` is unchanged.** Its registry becomes the
  `analytics/strategies/` package in Phase 12.
- **The global git email is `pixcimation@gmail.com`**, which differs from the
  GitHub account `pixcination`. Commits are attributed to that email; if it
  is a typo, fix it with
  `git config --global user.email pixcination@gmail.com`.
