# Phase 20B — Symbol Lookup, weekly health check, macOS preparation

Written 2026-09-29 for Tom and for the next Claude Code session. Built after
the Phase 19 full-day test passed and the Decisions → Tracking move, as
agreed. After this, the three-month freeze applies: only fixes for problems
the daily jobs turn up. Section 6 lists the choices I made where the brief
left room, for you to confirm.

## 1. Symbol Lookup

A new page, **Symbol Lookup**, in the sidebar after Strategies
(`app/pages/12_Symbol_Lookup.py`). The work is done by `pipeline/lookup.py`,
which you can also run from the command line:
`.venv\Scripts\python pipeline\lookup.py PLTR --profile roth_ira`. It calls
the modules the pipeline already runs and adds no new analytics.

| Step | Module | Notes |
|---|---|---|
| Check the symbol | yfinance `history` + `instrumentType`; `tastytrade_client.fetch_chain` | Refused with a message if Yahoo has no prices, the type isn't a stock, ETF or index, or TastyTrade lists no options |
| Register | `universe.add(tags="adhoc", source="lookup")` | Symbols already in the universe are left alone. A deactivated one comes back tagged `adhoc`, not into the universe |
| Daily sync, market metrics, events | `sync_daily`, `sync_earnings` (stocks only), `tasty_metrics.sync`, `events.build` | Each merges into its shared store. Nothing is rewritten with only this symbol |
| Technicals and level study | `technical_study.run` | |
| Outlook at every horizon | `outlook.build(save=False)` | Pooled skill with the per-symbol shrink (`for_ticker`). The Volatility dial is ranked against the saved universe table, which is **not** rewritten |
| Targeted chain capture | `chains.capture_targets` | Within the chain staleness window, a recent chain is reused |
| Candidates | `evaluate_universe`, `probabilities.run_sheet`, `recommender.run(recommend=True)` | CSP (physically settled names only) and put spreads for the chosen profile. The CSP DTE window, spread DTE and width are editable, and everything else uses the request defaults |

**Output.** An ordinary run folder (`data/runs/<id>/`) whose manifest has
`kind: lookup` and `symbol`, plus `outlook.parquet`. Because it's a normal
run folder, Trade Detail opens its rows, and its back button returns to
Symbol Lookup. `latest_run()` skips lookups, so a lookup never replaces the
run that the Screener, Decisions and other pages show.

**Display.**
- The Outlook gauges with a horizon selector (the existing `outlook_view`).
- Trend, daily and weekly RSI, IV rank, IV percentile, and the IV /
  forecast-RV ratio (relative richness).
- The support map and upcoming events, market-wide ones included.
- The trade grid: selecting a row opens Trade Detail, and **Track rows**
  logs selected rows as tracked forward tests.
- The recommender's strategies, or its conditions table when none fit.
- "Lookup time" against the 90-second target, with the time per step.

**Rules, as specified.**
- **Run lock.** A lookup holds the pipeline's run lock. Before it takes
  the lock, it is refused if the scheduler is running a job, or a slot is
  pending or due within 15 minutes (`scheduler.busy`). The page shows the
  reason and disables the button.
- **Notes.** A clear note when history is short (under 2 years of daily
  bars; under 300 bars there's no Outlook at all) and when the Outlook skill
  is the pooled estimate. Every symbol outside the validated universe gets
  the pooled note.
- **`sample = lookup`.** Anything recorded from a lookup run gets it, from
  this page, from Trade Detail's Accept, or from any other page that
  records a row of that run. It's enforced once, in `paper.accept` and
  `tracking.log` (`paper.sample_for_run`).
- **Excluded from accuracy statistics.** Lookup trades are left out of the
  win rate against prediction, POP and P(target) calibration, and the
  automation gate's count (`paper.NOT_FOR_ACCURACY`, `paper.for_accuracy`).
  They still count in dollar P&L and fill slippage, because those are real.
  Recorded as a Phase 21 requirement too.
- **Ad-hoc symbols stay out.** `universe.symbols()` leaves them out, so
  `universe: all`, the nightly data stages, the chain archive and the
  default run skip them. Keyed request universes (`all`, `stock`, `csp`,
  `tag:x`) skip them too. An explicit ticker list or `tag:adhoc` still
  reaches them, except in auto presets: the scheduler strips ad-hoc symbols
  even when listed by name, and refuses a preset that lists nothing else.
  **Add to universe** removes the tag.

## 2. Lookup times

Measured after the close on 2026-09-29, with the target under 90 seconds:

| Lookup | Time | Largest steps |
|---|---|---|
| PLTR, first time (new ad-hoc, 1,506 bars pulled) | **24 s** | chain 10 s, candidates 3 s, probabilities 2 s |
| PLTR again (data current, chain reused) | **11 s** | candidates 3 s, probabilities 2 s |
| SPX (universe index, put spreads only, large chain) | **50 s** | chain 21 s, candidates 16 s, recommender 6 s |
| ZZZZQX | refused in 2 s | "Yahoo has no prices" |

During market hours the chain step shares dxFeed's request budget. The
scheduled scans hit its pauses on 2026-09-29, so a large index could come
in nearer 90 s. **To do:** time one lookup during market hours, outside the
job windows.

## 3. Weekly health check

`.venv\Scripts\python scripts\health_check.py [--days 7]` reports:
- slots by status per job and preset, with median and longest duration
- every slot that wasn't `ok`
- jobs over 15 minutes
- the auto-logs' opened, observed and capped counts
- the archive's size and ticker count
- lookups against the 90-second target
- the **mark job** per day

The mark is now split in two: chain pulls, which scale with tickers, and
marks, which scale with open positions. The scheduler records the split in
the history (`chain_s`, `marks_s`, `tickers`, `positions`), and older marks
are split from `worker.log`. From 2026-09-29: **9.7 s per ticker** (20
tickers) and **0.95 s per open position** (42), so 3.9 minutes.

This corrects what I said yesterday about "about 5.5 s per position": the
chain pulls dominate, and tickers are capped by the universe. At 67 tickers
plus 300 open positions, a mark would pass 15 minutes. If a mark goes over
15 minutes, the report prints the proposed fix; over 10 minutes it prints a
"watch" line:

1. Reuse chains under ~20 minutes old instead of `force=True`.
2. Use fewer simulation paths for the probabilities from now.
3. Mark every 2 hours, or mark the tracked book less often than the taken
   book.

## 4. macOS preparation

Behaviour on Windows is unchanged.
- **Worker spawn.** `scheduler.spawn_args()`: Windows gets exactly the same
  arguments and flags as before (pythonw, detached, no console). POSIX
  starts the worker in its own session (`start_new_session=True`), so
  closing the terminal doesn't stop it.
- **`setup.sh`** mirrors `setup.ps1`: the venv (Python 3.11+), requirements,
  `.env` from the template with mode 600, and the preflight.
- **`scripts/launchd_agent.py install / status / remove`.** A LaunchAgent
  (`com.csp.scheduler-worker`) that starts the worker at login. It uses
  RunAtLoad without KeepAlive (the worker exits when a copy already runs),
  and wraps the worker in `caffeinate -i` unless you pass `--no-caffeinate`.
- **`docs/MACOS.md`** covers setup, the worker on each platform, sleep
  settings (caffeinate, the power-adapter setting, `pmset repeat
  wakeorpoweron`), and the single-machine rule. The TastyTrade refresh token
  rotates, and DuckDB has one writer, so the credential and `data/` live on
  one machine at a time. The doc also gives the order for moving them.
- **Tests.** No existing test was Windows-only; the pid-liveness test guards
  against a Windows bug but also passes on POSIX. The new spawn tests are
  platform-specific: the Windows one is skipped elsewhere, and the POSIX one
  is skipped on Windows. Not yet run on a Mac.

## 5. Checks

- `pytest tests`: 537 passed, 1 skipped (the POSIX spawn test on Windows).
  `tests/test_phase20b.py` adds 14 tests.
- `scripts/check_pages.py`: 14/14 pages clean.
- `scripts/preflight.py`: no blocking issues. Its four warnings (IWM and QQQ
  have no 1-minute archive, the archive and cache are old, BRK.B's bars are
  stale) predate this work.
- The worker was restarted on the new code after the nightly job, outside
  10:40–11:20 and 15:25–15:50.

## 6. Decisions to confirm

1. **Lookup runs are ordinary run folders** marked `kind: lookup`, so Trade
   Detail works unchanged. They count toward run retention like any run
   (`scripts/prune.py`).
2. **Short history = under 504 daily bars** (2 years). Under 300 there's no
   Outlook, which is `outlook.build`'s existing floor.
3. **Lookup trades keep counting in dollar P&L and fill slippage.** Only
   accuracy statistics leave them out.
4. **"Due within 15 minutes" includes a slot still pending inside its
   30-minute grace**, so at 11:00 a lookup is refused until the 10:45 mark
   has been recorded.
5. **The LaunchAgent uses `caffeinate -i` by default.**
6. **PLTR stays registered as ad hoc** from these runs. `config/universe.csv`
   gains its row, which stays out of commits as usual. Deactivate it on the
   Universe page if you don't want it.
