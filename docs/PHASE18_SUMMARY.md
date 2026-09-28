# Phase 18 — Trade tracking (manual)

Written 2026-09-28 for Tom and for the next Claude Code session. The work
order is `docs/REVIEW_P8-16_AND_NEXT_PHASES.md` Part C and Part E, Phase 18.
Tom said "proceed" with no new decisions. The defaults below are the
review's own (K = 5, M = 3, 15:45 archive, 60/90-day retention).

## 1. What was built

### 1.1 Two books in one ledger

`paper_positions` gained these columns:

- `book`: `taken` is a real trade at your fill; `tracked` is a forward test
  at the modelled fill.
- `sample`: `top`, `control` or `manual`.
- `dedupe_key` / `trade_id`, `rank_at_log`, `preset`, `entry_spot`,
  `logged_at`.
- `entry_context` (JSON: spot, EM, leg IVs, IV rank/percentile, trend, RSI,
  days to earnings, VIX term ratio, the entry probabilities).
- `source_row` (the sheet row, so it can be promoted later) and
  `promoted_from`.
- The outcomes: `hold_status`, `hold_pnl`, `managed_pnl`,
  `managed_exit_date`, `managed_rule`.

Existing rows migrate to `taken` / `manual`.

**Tracked positions never count.** The open book (Portfolio), the stress
test, correlation and the CSP roll ranker read `book = "taken"` only
(`paper.list_positions(..., book=)`). A tracked position never opens a
wheel cycle or a share lot: an assigned tracked CSP is scored, but no
shares appear.

### 1.2 Logging (C.3)

`analytics/tracking.py`:

- `log(rows)` records sheet rows as tracked. Rows are deduplicated on the
  trade id (strategy, ticker, expiry, strikes, and the root for SPXW).
  Seeing an open logged trade again appends a `tracking_observations` row
  (run, rank, price, probabilities) instead of opening a second position.
  Rows that can't be recorded (a buy-write's stock leg) are skipped with a
  reason.
- `sample_rows(sheet)` / `log_all` take the top K passing rows, M random
  passing rows below them and M random near-miss rejects (exactly one
  failed gate). The draw is reproducible per run.
- `promote(tracked_id, fill)` records the real trade linked back to the
  tracked row, which keeps running as the forward test.

In the UI:
- The Screener has a **Log mode** toggle. With it on, the grid is
  multi-row, with *Log selected*, *Log every passing row shown* and *Log
  all* (top K + control).
- The Strategies grid is multi-row with *Log selected*.

### 1.3 Update now (C.4)

`tracking.update()`:

- Refreshes only the expirations the open legs use, per ticker, and merges
  them into the block's snapshot (`chains.capture(expirations=…)`). The
  first live update showed why: a 0–54 DTE pull of SPX exceeds the
  6,000-subscription cap, which drops the far expirations first — exactly
  the Nov 20 monthly the SPX spreads live in.
- Writes one `position_marks` row per position:
  - time and session block, spot
  - per-leg bid/ask/mark/IV/IV-from-mid/Greeks
  - mark and natural, P&L in dollars and as a share of max profit
  - DTE left, best and worst so far
  - the probabilities **from now** (the engine on the current spot and leg
    IVs, keeping the entry credit): P(reach target), P(max loss or
    assignment), POP
  - the management verdict and its reason
  - IV rank/percentile, VIX ratio, trend, RSI
- **Attribution** splits the P&L since the previous mark into delta, gamma,
  theta, vega and a residual, using the previous mark's per-leg Greeks. The
  IV change is taken from each leg's mid-implied IV: on the live run
  dxFeed's Greeks events for SPY were identical three minutes apart while
  the mids moved.
- `tracking.verdict` is now the one management-decision function. The
  pipeline's open-position review calls it too, so the rules live in one
  place.

### 1.4 Tracking page

The Tracking page has:
- a book filter and the Update now, Expire due and Archive chains buttons
- the open positions: mark, P&L, % of max, best/worst, P(target) at entry
  vs now, POP now, verdict
- per position: entry vs now, marks and probabilities over time, the
  attribution chart and table, observations, Promote
- closed positions with **hold-to-expiry vs managed** P&L
- the archive list

The book parts of Decisions are still there. Moving them off is left to the
page regrouping the review proposes (A.2.8).

### 1.5 Expiry and outcomes

`tracking.expire_due()` settles every open position whose last expiration
has a completed session, from that day's close (daily bars, price basis):
expired worthless, settled, or assigned. It stores:

- **hold**: the package at the close, net of every fee (an assigned CSP is
  marked at the close)
- **managed**: the first recorded mark whose verdict was close or roll,
  exited there with close fees; with none, the hold outcome

A calendar whose front leg expired is left for a manual close.

### 1.6 Daily chain archive and retention

- `data_sources/chain_archive.py` + `scripts/archive_chains.py` capture
  every active symbol at 0–60 DTE (strike-filtered) and copy each ticker's
  files to `data/chain_archive/<date>/` with a manifest. The archive is kept
  forever. Phase 18 runs it by hand; Phase 19 schedules it.
- `pipeline/retention.py` + `scripts/prune.py` (a dry run unless
  `--apply`):
  - manifests are kept forever
  - a run's `prob_*` tables go after 60 days unless a logged position
    references the run
  - chain blocks older than 90 days thin to one per day (the latest RTH
    block)

  Today it has nothing to prune.

## 2. Acceptance on the live market (2026-09-28)

| Step | Result |
|---|---|
| Log a mixed CSP/PCS/spec selection from a live run | 20 tracked positions: *Log all* on the default PCS run (5 top + 3 control passing + 3 near-miss), on the CSP run (3 top + 2 control), 3 recommender positions (condor, bull put, bear call), and a 0-DTE SPY put for the expiry check |
| Update twice during RTH | 13:10 and 13:13 ET, 40 marks. The first found the SPX cap problem (§1.3), fixed before the second. A third ran at 15:42: 60 marks, 3 per position |
| Attribution and changed probabilities | e.g. QQQ bull put +$880: delta +$964, vega −$107, residual +$45. P(target) moved on every priced position: IWM 276p 0.87 → 0.92 as IWM rose, then 0.87 by 15:42 after the late sell-off; SPY 755p (CSP) 0.94 → 0.90, at −30% of max |
| Expire one position, both outcomes | #20 SPY 764 put (0 DTE) expired worthless at the $765.61 close: hold +$24, managed +$24 (no rule fired). The first attempt settled at a wrong price; see §3.1 |
| Daily archive (15:45, by hand) | 67/67 tickers, 25,199 rows, 4.9 MB, 13 min (dxFeed pacing), no failures → `data/chain_archive/2026-09-28/` |

## 3. What the first marks show

### 3.1 A daily-bar bug found by the expiry check (fixed)

The first auto-expiry settled #20 at $767.75. That was SPY's price at 10 a.m.
The real close was $765.61. The cause was a pre-existing bug in
`yfinance_sync.sync_daily`:
- Its "current" watermark was **today**, so any pipeline run during market
  hours stored today's in-progress bar.
- Every later sync then skipped the ticker as up to date.
- Today's stored SPY bar had 4.6M shares of volume against 40.8M for the
  real session.

This affects every run made during market hours since Phase 9, not only
today's. The partial bar fed:
- the engine's H/T paths
- Stage 1
- the technicals (which then skip the day as already studied)
- settlement

The fix:
- The watermark is the last **completed** session
  (`freshness.last_completed_session`, 16:15 ET), and bars after it are never
  stored.
- A regression test in `tests/test_phase8.py`.
- Repaired today: the 67 partial 2026-09-28 rows and that day's technicals
  were deleted, `--data-only` re-synced after the close, and #20 was
  reopened and re-settled at $765.61.

Earlier dates can't carry this error: the next day's incremental overlap
compares closes and fully re-pulls on any restatement. Today's rows were the
only ones left. The Phase 17 live runs this morning used the partial bar in
their H/T paths.

### 3.2 Scale and quotes

- **Size amplifies quote noise.** The runs size against the $3M `default`
  profile, so EWZ spreads carry 225–1,265 contracts. A one-cent move in a
  mid is then worth hundreds of dollars, which shows up in the residual
  (EWZ bear call: −$1,898 over three minutes, a −$848 residual). Entering
  the real account values (Phase 17, open decision 4) fixes the scale.
  Until then, compare `% of max` rather than dollars.
- **Mids are not fills.** Marks use chain mids, and `natural` is stored
  beside them. Wide far-OTM legs (EWZ, GME) swing between marks without any
  trade.

## 4. Checks

- `pytest tests`: **487 passed**, including 11 new tests in `tests/test_phase18.py` and the sync regression test. `test_phase8`'s price-basis list now names `analytics/tracking.py`, where the position review's `load_daily` moved.
- `scripts/check_pages.py`: 13/13 pages clean (Tracking added).

## 5. Open for Tom

1. The Phase 17 decisions still stand (Monday CSP window, spread-leg floors,
   JSON request defaults, real profile values).
2. The **K = 5 / M = 3** sample and the **60 / 90-day** retention are the
   review's defaults; change them in `config.yaml → tracking / retention`.
3. Moving the book parts of Decisions onto Tracking belongs with the page
   regrouping (review A.2.8). Say when.

## 6. Next

Phase 19: the scheduler. A worker process started by `launch.py` runs:
- `mark` hourly 9:45–15:45
- `scan_and_log` per preset marked auto
- `archive` at 15:45
- `nightly` at 18:30

It honours the missed-slot, overrun and half-day rules and shows a
heartbeat and job history on the page. `tracking.update`, `tracking.log_all`,
`tracking.expire_due` and `chain_archive.archive` are the functions it calls.
