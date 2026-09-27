# Phase 9 summary — universe registry, yfinance coverage, events

Date: 2026-09-27. Roadmap: [SCREENER_ROADMAP.md §C.1](SCREENER_ROADMAP.md).
Architecture: [ARCHITECTURE.md](ARCHITECTURE.md) §3.3, §4, §7.

## Decisions

| Question | Decision | Who |
|---|---|---|
| Initial registry | the 61 names + SPY, QQQ, IWM, DIA, SPX, XSP, NDX, RUT (SPY and DIA were already in) → **67 symbols**: 54 stocks, 9 ETFs, 4 indices | Tom (recommended default) |
| Names without weeklies | included, flagged `weeklies = False` (only ELV today); excluded when a request asks for DTE < 14 (Phase 11) | Tom |
| Event policies | earnings **block** (stocks only), FOMC and CPI **warn**, ex-dividend **warn for covered calls only**; NFP, OPEX, quad witching ignore; splits warn | Tom |
| Registry storage | DuckDB table (`data/universe.duckdb`) mirrored on every write to a **versioned** `config/universe.csv`; an empty DB re-seeds from the snapshot first | Claude: `data/` is not versioned and the registry holds hand edits |
| XSP history | `^SPX × 0.1`. Yahoo's `^XSP` exists but starts 2021-03-01; the two agree within 0.02% (median 0.0005%) on all 1,397 overlapping days | Claude (roadmap: "verify") |
| `load_universe()` default | `scope="csp"` (active, physically settled), so every pre-Phase-9 CSP analysis is unchanged. Data stages use `scope="all"` | Claude |
| Index chains | **not captured yet**; indices get bars, metrics and events. Chain capture for index underlyings is Phase 11 (roadmap C.3 task 4) | Claude |
| Stage 1 drawdown window | `stage1_thresholds.drawdown_lookback_years: 10` (see §3) | Claude, **for Tom to confirm** |
| TastyTrade IV rank used | headline `implied-volatility-index-rank` (source "tos"); the `tw-` rank stored alongside | Claude |
| IVR/IVP in candidates | carried on every row and shown on Decisions; **not** a gate or score input (weights are Phase 11 and unvalidated) | Claude |

## 1. What changed

### Universe registry (`data_sources/universe.py`, Universe page)
- Table columns: `symbol, yf_symbol, tt_symbol, price_scale, asset_class,
  category, sector, industry, quality_tier, optionable, weeklies,
  leverage_flag, settlement, exercise, settlement_times, active, tags, notes,
  source, added_at, updated_at, stage1_pass, stage1_tier, stage1_reasons,
  stage1_checked_at`.
- Seeded from `final_universe.txt` + `stage2_quality_tags_master.csv` + the
  defaults. Sector, industry, weeklies and AM/PM settlement are filled from
  TastyTrade metrics.
- `core.paths.load_universe(scope)` reads it and falls back to the text file.
  `import_text_file()` keeps the text file as an import format.
- **Universe page** (`app/pages/6_Universe.py`): add a symbol (any vendor
  spelling), with an optional immediate data refresh; edit active, tags and
  notes; filter and search; IVR/IVP, next earnings and source disagreements,
  Stage 1 verdicts; per-symbol weekly bars, earnings reactions and upcoming
  events; a 45-day market-event calendar with the policy for each event.

### Batched yfinance, indices (`yfinance_sync.sync_daily`)
- `yf.download` in batches of 40, with threads, 3 retries and exponential
  backoff, then a single-symbol retry for anything a batch dropped. It uses
  registry mapping and `price_scale`, so one `^SPX` download fills both SPX
  and XSP.
- The Phase 8 re-pull rules are unchanged: overlap comparison, then a full
  re-pull on any new or late action or restatement.
- Indices loaded: SPX 24,801 bars (from 1927), XSP 24,801, NDX 10,326, RUT
  9,835, plus QQQ 6,930 and IWM 6,622.
- `sync_earnings` / `sync_dividends` now use the Yahoo symbol (BRK.B returned
  nothing before) and record **time of day** (bmo / amc / during / unknown;
  1,305 of 1,345 dates have bmo or amc).

### Weekly bars (`analytics/bars.py`)
`weekly()` resamples W-FRI. `weekly_on_daily()` attaches each week from its
**scheduled** last session, taken from the NYSE calendar rather than the data.
So a Wednesday sees the previous week, Friday's close sees its own week, and
the Good Friday week completes on Thursday. `last_completed_week(as_of)` and
`load_weekly(symbol)` are also provided. Cost: 1.7 s for 98 years of SPX.

### Stage 1 on yfinance (`analytics/universe_screen.py`)
This applies the `scripts/02` metrics, thresholds, hard reasons and tiers to
`daily_bars_raw` on the price basis. Results are written to the registry and
are **advisory** (they never deactivate a symbol). See §3 for the one
deliberate difference.

### TastyTrade market metrics (`data_sources/tasty_metrics.py`)
Uses `GET /market-metrics?symbols=`, 50 symbols per request, stored daily in
`market_metrics`. All 67 symbols returned data. **Live field names and units
were verified on 2026-09-27** and are documented in the module docstring:
- Values arrive as strings.
- IVR and IVP are 0–1 fractions.
- `implied-volatility-30-day` and the `historical-volatility-*` fields are
  percentages.
- The headline IVR is the "tos" rank. The `tw-` rank differs materially
  (AAPL 0.43 vs 0.25).
- Earnings are nested and carry an `estimated` flag.
- `dividend-next-date` is stale (2022 dates were returned) and is ignored.
- Indices use bare symbols, and their expirations list AM and PM settlement.

### Events (`data_sources/events.py`, `config/macro_calendar.yaml`, `config.yaml → event_policy`)
- **Earnings.** yfinance history plus the next date, merged with the tasty
  expected date. When the two differ by more than 1 day the row is flagged
  `sources_disagree` and the **earlier** date is kept. On the first build 4
  names disagreed: ABT, GOOG, MSFT and TXN. All 54 stocks have a forward date.
  A tasty date already in the past (HRL: 08-27) is not treated as forward.
- **Ex-dividend** history, plus a projected next date (unconfirmed).
  **Splits** history.
- **Macro dates, verified against the sources.** FOMC 2026–2027 (16
  meetings, dated to the decision day at 14:00 ET) from federalreserve.gov.
  CPI and payrolls for 2026 from bls.gov. BLS has not published 2027 yet, and
  the YAML says to add it.
- **OPEX / quad witching** are computed, and a holiday moves them to the prior
  session. Juneteenth 2026 moved June's quad witching to Thursday the 18th.
- **`events.check(symbol, start, end, strategy)`** replaces the direct
  earnings gate in `candidates.py`. Blocking earnings keeps the old rejection
  text. Other blocking events are added as rejections, warnings as warnings,
  and every hit is stored on the candidate row (`events`).

### Earnings reactions (`analytics/earnings_history.py`)
For each past report it computes the gap, close-to-close move, two-session
move and ATR multiple, picking the reaction day by bmo/amc. Examples: NVDA
median |move| 4.4% (1.1× ATR), AAPL 1.0%, KO 1.8%. The implied move is taken
from the last metrics snapshot before a report, so the "beat the implied
move" rate fills in as snapshots accumulate from today.

### Pipeline
- New stages: `universe → … → earnings → metrics → events → stage1` before
  `chains`.
- **`--data-only`** stops before chains, for the nightly job.
- `latest_run()` skips runs without an analyse stage, so a nightly run cannot
  blank Decisions.
- Chains and analysis run on CSP-tradable symbols only.
- The earnings stage skips only if the file is fresh **and** every stock is
  in it, so a newly added symbol doesn't wait a day.
- Preflight freshness now covers market metrics and the events table.

## 2. Defects fixed

| Defect | Effect | Fix |
|---|---|---|
| **Every ETF permanently blocked.** The fail-safe "unknown earnings date blocks the trade" applied to all tickers, and ETFs never report. | SPY (34 strikes), DIA, EFA, KRE, IBIT, EWZ and KWEB were rejected on earnings in every run. The last Phase 8 run shows it on 59 ETF strikes. | The rule applies only to `asset_class = stock`. |
| **Partial earnings/dividend pulls wiped the file.** `sync_earnings` overwrote `earnings.parquet` with only the tickers it was asked for. | `pipeline/run.py --tickers X` deleted every other stock's earnings dates, and the fail-safe then blocked all of them. (Reproduced here by a one-symbol check, and restored.) | Merge: the requested symbols' rows are replaced and everyone else's kept. Same fix for dividends. |
| yfinance earnings used the canonical symbol | BRK.B (and any class share) got no dates | Uses `yf_symbol` |
| Earnings time of day discarded | Reaction studies could not tell bmo from amc | Stored |
| Preflight FAILED when any symbol lacked 1-minute archive data | Adding a registry symbol the archive never held (IWM, QQQ) made preflight exit non-zero | A partial gap warns (the analysis falls back to daily data); only a missing archive fails. The staleness warning is still reported alongside it. |
| `latest_run()` would pick a nightly `--data-only` run | Decisions and Wheel would show an empty run the morning after | Runs without an analyse stage are skipped |

## 3. Stage 1: yfinance vs `scripts/02`

Stage 1 was run on the registry with the `scripts/02` thresholds. Over **all
history**, 27 of 63 symbols agreed with the archive-based tiers. Nearly every
disagreement came from the unrecovered-drawdown rule, for two reasons:

- **The archive is not consistently adjusted for reverse splits.** AIG (1:20,
  2009) and C (1:10, 2011) passed `scripts/02` only because the split masked
  the 2008 collapse.
- **The Tier 2 names had only ~2.5 years of archive data**, while Yahoo shows
  their 2021 peaks.

So neither version is canonical. The yfinance screen measures drawdown over
`drawdown_lookback_years` (default **10**; `null` = all history). The resulting
tiers are **46 tier 1, 1 tier 2, 16 rejected, 4 indices not screened**:
- Deep, unrecovered drawdowns within 10 years: AAL, AFRM, APA, BIDU, CNC, DKNG,
  GME, KWEB, OXY, PCG, PDD, SOFI, VFC.
- Price under $15: AAL, PCG, VALE, VFC.
- Realized vol below the 12% floor in the current calm market: **SPY and DIA**.
  The threshold is working as written, but you may want a lower floor for
  index ETFs.

Stage 1 is advisory: nothing is deactivated.

## 4. Verification

| Check | Result |
|---|---|
| `pytest tests -q` | **265 passed** (218 after Phase 8 + 47 in `test_phase9.py`) |
| `pytest legacy/tests -q` | 1 passed |
| `scripts/check_pages.py` | **8/8 pages clean** (new: Universe) |
| `scripts/preflight.py` | exit 0. Warnings: 1-minute archive 25 days behind, missing for IWM/QQQ (never in the archive), 1-minute cache 88 days old. Market metrics and events are fresh. |
| **Acceptance: add a symbol → data** | BRK.B was added through the Universe page form in a headless AppTest session, typed as `BRK-B` on purpose. The registry mapped it to Yahoo `BRK-B` / TastyTrade `BRK/B`. `pipeline/run.py --data-only` (43 s) then produced 7,644 daily bars, 1,586 weekly bars and market metrics (IVR 0.50), plus a forward earnings event on 2026-11-06 confirmed by yfinance and tasty. That run exposed and fixed two bugs (the vendor symbol in the earnings pull, and partial pulls wiping the earnings file). BRK.B was then removed so the universe stays as approved. |
| **Acceptance: indices load** | SPX, XSP, NDX, RUT: daily and weekly bars, market metrics (AM+PM settlement for SPX/NDX/RUT), events (FOMC/CPI/OPEX; no earnings rule). `events.check("SPX", 09-28, 10-30, "pcs")` → warn on FOMC 10-28 and CPI 10-14. |
| **Acceptance: tests** | symbol mapping (11 cases), weekly no-lookahead (4, incl. the Good Friday week), event windows (8, incl. `days_before`, strategy filter, ETF/stock fail-safe, degraded calendar) |
| Full run `20260927-175007-bba2` | **277 strikes, 39 passed the gates (was 9), 10 selected, 3 proposed**: CNC $60p ×13 (EV 16.4%, IVR 0.39), BMY $61p ×8 (14.8%, 0.53), **IWM $278p ×4 (12.8%, 0.23)**. **ETF strikes: 113, earnings-rejected 0** (Phase 8 run: 59 of 59 rejected). 30 ETF strikes passed every gate (IWM 10, SPY 10, QQQ 7, DIA 3). SPY was held back only by the 0.80 correlation limit with IWM. IVR is on 277/277 rows. No row had an event hit: expirations were 10/02–10/09, NFP 10/02 is "ignore", CPI is 10/14, and no stock reports before 10/09. |
| Earnings | all 54 stocks have a forward date; 4 source disagreements flagged (ABT, GOOG, MSFT, TXN) |

**A run was killed and the cause is unknown.** The first Phase 9 full run
(started with `--force-chains`) died during chain capture at 17:49, right
after writing IBIT, with exit code 127. There was no traceback, and it left a
stale `run.lock`.

- Unlike the Phase 8 kill, nothing that touches the run lock was running.
  The only concurrent work was a doc patch and a read-only freshness query.
- The re-run reclaimed the dead lock correctly (the Phase 8 fix) and
  completed.
- If this recurs, run the pipeline in its own terminal instead of a
  background job, to rule out the job host.

## 5. Known limits

- **Index chains are not captured yet** (Phase 11). Indices appear in bars,
  metrics, events and Stage 1 only.
- **IVR/IVP don't influence ranking yet.** They are on every row and card;
  ranking weights come with `underlying_rank.py` in Phase 11.
- **Macro calendar upkeep.** CPI and payroll dates for 2027 must be added by
  hand when BLS publishes them. FOMC dates are tentative until the prior
  meeting confirms them.
- **Earnings "beat the implied move"** needs metrics snapshots from before a
  report. They are stored daily from 2026-09-27, so the first rates appear
  after the October reports.
- **Weekly bars before 1952** may group NYSE Saturday sessions into the
  following W-FRI week. This only affects the pre-1952 part of the SPX
  history.
- The TastyTrade sector taxonomy is used as-is (e.g. ELV is labelled
  "Financial").
