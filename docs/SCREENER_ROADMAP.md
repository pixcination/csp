# CSP / PCS Screener — Gap Analysis and Build Roadmap (Phase 8 onward)

Audience: Claude Code, running in `D:\csp`, and Tom.
Written 2026-09-27 from a read of the full source tree (`analytics/`, `app/`,
`core/`, `data_sources/`, `pipeline/`, `scripts/`, `vendor/`, `docs/`,
`config.yaml`). The code was read, not executed — the first job of Phase 8 is to
confirm the baseline actually runs (see §C.0).

---

## Part A — Where the application is today

### A.1 What exists (and is good)

The tool is further along than its own `docs/ARCHITECTURE.md` suggests (that
file still describes the original three-page app). Phases 1–7 added a
production-minded CSP/wheel engine:

| Area | Module(s) | State |
|---|---|---|
| One-button orchestrator, staleness-aware, run lock, manifest | `pipeline/run.py`, `app/pages/0_Command_Center.py` | Solid |
| Session awareness (RTH/closed, snapshot stamping) | `core/market_calendar.py` | Solid |
| Credential single-source, path discipline | `core/env.py`, `core/paths.py` | Solid |
| yfinance daily bars, earnings dates, dividends | `data_sources/yfinance_sync.py` | Works, one correctness bug (§A.3) |
| VIX complex + Treasury rates | `data_sources/reference.py`, `analytics/regime.py` | Solid |
| TastyTrade chain capture (REST + DXLink), subscription pacing | `data_sources/chains.py`, `vendor/tastytrade/` | Solid, equity-only, ≤21 DTE |
| Transaction-cost model (tastytrade schedule) | `analytics/costs.py` | Solid, single-leg |
| Empirical move engine — P(breach), P(touch), vol-conditioned, effective-n | `analytics/moves.py` | Strong — the core differentiator |
| EV-ranked CSP decision sheet with hard gates | `analytics/candidates.py` | Strong, CSP-only |
| VRP (IV/RV), skew, term structure, vol surface rebuild | `vrp.py`, `skew.py`, `surface.py` | Strong |
| Gap (overnight) risk | `analytics/gaps.py` | Strong |
| Sizing (capital + liquidity caps), portfolio clustering, stress | `sizing.py`, `portfolio.py` | Strong |
| Wheel: covered calls, roll ranking, cycle backtest, walk-forward | `covered_call.py`, `roll_engine.py`, `wheel_backtest.py`, `walkforward.py` | Strong |
| Paper book with modelled vs actual fill, calibration | `paper.py`, `calibration.py`, Validation page | Strong, single-leg |
| Tests | `tests/` (6 files) | Good behavioural coverage |

### A.2 Scorecard against the target vision

| # | Vision item | Status | Notes |
|---|---|---|---|
| 1 | Daily history from yfinance for stocks, ETFs, **indices** | **Partial (~50%)** | Only for the 61 names in `output/final_universe.txt`. No index symbols (^SPX, ^NDX, ^RUT), no symbol mapping, universe itself still comes from the local 1-minute drive pipeline (`scripts/01–05`). |
| 2 | Events → no-trade / caution periods | **Partial (~45%)** | Earnings gate (blocks if earnings before expiry) and ex-div projection exist. No unified events table, no macro calendar (FOMC/CPI/NFP/OPEX), no historical earnings-reaction stats, no configurable caution windows. |
| 3 | SMA/EMA 9/21/50/100/200 daily **and weekly**, RSI, and a per-ticker "which levels does it respect" study | **Mostly missing (~10%)** | `technicals.py` has SMA 20/50/200 daily + RSI + 52-wk range only. No EMA, no weekly bars, no support detection, no respect statistics. |
| 4 | Rank underlyings from pricing/technicals *before* pulling chains | **Missing (~10%)** | Today every universe ticker gets a chain pull and ranking is per-strike EV. There is no pre-chain "which funds" stage. |
| 5 | Snapshot the chain for selected tickers | **Good (~80%)** | Capture is robust, but the DTE window is fixed (≤21, ≤60 with a position), equity-only, and pulls the whole universe rather than the top N. |
| 6a | Expected move (TastyTrade method) | **Missing** | Only a Black-Scholes σ cone in `charts.py`. |
| 6b | IV-based premium opportunity | **Partial (~60%)** | VRP, skew, term structure present. IV rank is effectively dead: only two legacy scan dates + one closed-session block exist, well under the 10-date minimum. |
| 6c | OI at optimal strikes/deltas, bid/ask width, volume | **Good (~70%)** | Captured and used for sizing/gates; not surfaced as a per-recommendation liquidity profile. |
| 6d | Recommended strikes & DTE for **CSP** | **Good (~70%)** | EV-ranked, but DTE is fixed in config (5–10), and output is capped to one row per ticker, top 3. |
| 6e | Recommended **PCS** short/long strikes at several widths | **Missing (0%)** | No multi-leg code anywhere (pricing, costs, paper book, UI). |
| 7 | P(hit 25/30/50/100% profit) by date, from Greeks + history + technicals | **Missing (~5%)** | Only terminal P(OTM) and P(touch). No path/repricing engine. Note: `exit_rules.py` deliberately argues against % targets at 7 DTE because of fees — see §B.4 for how to reconcile. |
| 8 | Screener input form (DTE, risk, strategy, profit goals) → results grid → click → deep-dive page | **Partial (~35%)** | One-button run and progress exist; no input form, results are cards not a grid, results live only in `st.session_state` (lost on refresh), deep-dive page (`2_Ticker_Detail.py`) runs on the *legacy* stack. |
| 9 | Future multi-strategy screener / strategy spec search | **Missing (~5%)** | `strategies.py` is three label fields. `candidates.py` is put-specific throughout. |

**Overall:** the CSP analysis engine is roughly **60–65%** of the vision and
is higher quality than most commercial screeners in its probability work. PCS
is essentially **not started**, the technical/level-respect layer is **not
started**, and the multi-strategy vision needs a strategy abstraction that
does not yet exist.

### A.3 Defects and debt found during the read

1. **Adjustment-vintage bug in `yfinance_sync.sync_daily`.** It downloads with
   `auto_adjust=True`, then on later runs re-pulls only the last ~5 days and
   upserts them. Every new dividend makes Yahoo re-adjust the *entire* history,
   but only the tail is re-stated, so the stored series becomes a mix of
   adjustment vintages with an artificial step at each seam. This corrupts
   returns, MAs and every probability computed across the seam.
2. **Wrong price basis for strike probabilities.** `moves.breach_probabilities`
   and `candidates.basis_assessment` read `load_daily_total_return`. Options
   settle on the *traded* price, which drops by the dividend on the ex-date.
   A dividend-adjusted series erases those drops, so P(breach) is understated
   for high-yield names (MO, T, KO, PBR, VALE, ET, KMI…) whenever the window
   spans an ex-date, and the 52-week/200-day basis comparison is skewed.
   Rule should be: **price-return (split-adjusted only) for strikes, levels,
   technicals and breach probabilities; total return only for long-run
   wheel/buy-and-hold performance comparisons.**
3. **Two parallel stacks.** Legacy (`1_Scanner`, `2_Ticker_Detail`,
   `3_Trade_Log`, `scoring.py`, `backtest.py`, `trade_log.py`,
   `data_access.py` reading `data/stage3_chains/`) runs beside the new stack
   (`0_Command_Center`, `1_Decisions`, `paper.py`, `candidates.py`,
   `wheel_backtest.py`, `data_sources/chains.py` reading `data/chains/`).
   The navigation shows both. The deep-dive page you want to build on is on
   the legacy side.
4. **Run results are not reloadable.** Decisions/Wheel/Portfolio read
   `st.session_state["last_manifest"]`; a browser refresh or restart empties
   them even though `data/runs/<id>/manifest.json` exists on disk.
5. **No version control.** There is no `.git` folder in `D:\csp`.
6. **Docs drift.** `ARCHITECTURE.md` describes the Phase-2 app; Phases 3–7 are
   documented only in module docstrings.
7. **Stale caches.** Last run manifests are from 2026-08-22; the 1-minute cache
   (`raw_1m_cache.duckdb`, used by `gaps.py` and intraday RV) was last written
   in July. Not a code defect, but preflight should flag cache age.

---

## Part B — Recommendations (design decisions)

### B.1 Keep the architecture, extend it

Stay on Python + DuckDB + Streamlit. Streamlit ≥1.35 supports single-row
selection on `st.dataframe` (`on_select="rerun"`), which is enough for
grid → deep-dive. Revisit a FastAPI + React front end only if you later need
things Streamlit can't do (e.g. dragging strikes on a payoff chart).

Keep every existing convention: all paths via `core.paths`, all thresholds in
`config.yaml`, `analytics/` has no Streamlit imports, gates are rejections not
penalties, every probability reports its sample size.

### B.2 Data layer

- **Universe registry** (DuckDB table) replaces `final_universe.txt` as the
  source of truth: `symbol, yf_symbol, tt_symbol, asset_class
  (stock|etf|index), sector, optionable, weeklies, leverage_flag,
  settlement (physical|cash), exercise (american|european), active, tags`.
  Seed it from the current 61 + stage-2 tags, then add index ETFs and indices
  (SPX, XSP, NDX, RUT). Symbol mapping lives here (BRK.B↔BRK-B, SPX↔^SPX).
  For XSP, use ^XSP if Yahoo serves it, otherwise ^SPX ÷ 10 — verify.
- **Daily bars:** store raw OHLCV (`auto_adjust=False`: Yahoo's Close is
  split-adjusted, plus `Adj Close`), dividends, splits. Derive the
  total-return factor locally. On any new dividend or split in the incremental
  window, re-pull that ticker's full history. Weekly bars are **resampled
  locally** (W-FRI), never downloaded separately.
- **Stage 1 on yfinance:** re-implement the Stage 1 filter (ADV, price, RV,
  drawdown, history) against the yfinance table so the universe can be
  refreshed without `D:\pricing_data`.
- **TastyTrade market metrics** (`GET /market-metrics?symbols=…`): this
  endpoint returns IV index, **IV rank, IV percentile, liquidity rating,
  expected earnings date**, beta and per-expiration IVs. It fixes the dead
  IV-rank component on day one. Keep your own snapshot IV history as a
  cross-check. Claude Code must verify the live field names before relying on
  them and record them in the docs.
- **Events table** (`events`: `symbol|*, date, type, time_of_day,
  confirmed, amount, source`): earnings (yfinance + tasty metrics, flag
  disagreements), ex-dividend, splits, macro (FOMC, CPI, NFP from a
  user-maintained `config/macro_calendar.yaml`), monthly OPEX and quad
  witching (computed). A **caution policy** in config maps event type →
  `block | warn | ignore` and a window.
- **Earnings reaction history** per ticker: past earnings-day moves (gap and
  close-to-close) vs. the move implied beforehand when available. Tells you
  whether a name habitually beats its implied move.

### B.3 Technical engine and the "level respect" study

This is the most novel piece of the vision and the easiest to get wrong. A
naive "price came near the 200W EMA and went up afterwards 95% of the time"
is mostly an artifact of uptrends and few samples. Build it as a real event
study:

- **Indicators** (daily and weekly): SMA/EMA 9/21/50/100/200, RSI-14,
  ATR-14, Bollinger(20,2), MACD, ADX-14, 52-week range, volume ratio. Weekly
  values used on a daily timeline must be **the last completed week's value**
  (no lookahead). Add a test for this.
- **Test event** for level L: price approaches from above (prior close > L)
  and the day's low comes within `band` of L (default 0.5×ATR). Debounce: no
  new event until price has been > L + 1×ATR for m days.
- **Outcome** over horizons 5/10/20 days: *held* (no close below L − tol,
  tol = 1×ATR), *bounced* (reached L + 1.5×ATR before breaking), *broke*.
  Also record **max pierce depth** below L before it held — this is what sets
  the strike cushion.
- **Honesty:** Wilson 95% CI on every rate, minimum n (default 8) else
  "insufficient", **placebo baseline** (same statistics at randomly offset
  levels in the same periods) and report *edge over baseline*, split by level
  slope (rising/falling). Rank levels per ticker by the CI lower bound of the
  edge, not the raw rate. Display as e.g. "200W EMA: 11 of 12 held (92%, CI
  65–99%), +31 pts vs placebo".
- **RSI/oscillator study:** forward 5/10/20-day return distributions after
  RSI < 30 / > 70 (daily and weekly) vs. unconditional — per ticker.
- **Trend state:** uptrend / range / downtrend from EMA stack order + slope +
  ADX, used for ranking and for the technical-conditioned probability model.
- Cache results nightly in DuckDB (`level_stats`, `indicator_latest`).

### B.4 Probability-of-profit engine

One engine, three models, same output, shown side by side:

| Model | Underlying paths | Meaning |
|---|---|---|
| **G — market-implied** | GBM at the options' IV (per-strike IV from the surface) | What the option prices imply |
| **H — historical** | Block bootstrap (5-day blocks) of the ticker's own price-return daily log returns, vol-conditioned (reuse `moves.attach_vol_regime`) | What this stock has actually done in similar vol |
| **T — technical-conditioned** | Model H restricted to start dates with a similar technical state (trend state, distance to nearest strong support in ATR buckets, RSI bucket); falls back with a flag when effective n is small | What it did from setups like today's |

On every simulated path, reprice every leg daily with Black-Scholes (sticky
strike IV from today's surface, optional IV mean-reversion toward RV and a
post-earnings crush if an event sits inside the trade). From that compute,
per trade:

- P(reach X% of max profit **at any point** by day d) for X ∈ {25, 30, 50,
  75} — as a curve over d and as a value at expiry; median days to reach X.
- "100%" = P(expire worthless) = P(S_T ≥ short strike) — this can only be
  realised by holding to expiry, and the UI should say so.
- P(touch short strike), P(assignment) (CSP), P(max loss) = P(S_T ≤ long
  strike) (PCS), P(short delta beyond the roll threshold by day d).
- Expected P&L, expected holding days and annualised return **per management
  policy** (close at 25/50/75%, hold to expiry, 21-DTE time stop where
  applicable), all **net of fees**.
- A blended probability with configurable weights (default equal). Do not
  claim the blend is better until `calibration.py` has scored it on real
  outcomes.

Reconciling with `exit_rules.py`: its finding (at 7 DTE, fees make early
closes uneconomic and freed capital sits idle) stays true. The engine should
show % targets because you asked for them, **but** show the net-dollar result
of each and mark targets whose net gain is below
`management.exit.min_net_gain_to_close_early`. At 30–45 DTE (typical PCS) the
targets become meaningful; at 7 DTE the table will show why they aren't.

Validate: for model G, MC P(S_T > K) must match N(d2) within MC error (unit
test). For H/T, walk forward on real price history with BS-repriced options
(same synthetic-pricing caveat as `wheel_backtest.py`) and check that
predicted P(hit 50% by d) matches observed frequency.

### B.5 Expected move

- **IV method** (what the tastytrade platform displays): EM = S × IV ×
  √(DTE/365), IV = that expiration's ATM IV (interpolated at the forward).
- **Straddle method** (tastylive rule of thumb): EM ≈ 0.85 × ATM straddle
  mid; also show the full straddle.
- Show both, plus 1× and 2× bands, and each strike's distance in EM units.
- Historical EM containment: % of past windows where |move| ≤ EM (using
  stored IV when available, RV proxy otherwise, labelled).
- Claude Code should confirm which formula the current tastytrade platform
  uses and set the default in config accordingly.

### B.6 Strategy abstraction (needed now for PCS, essential later)

Create `analytics/strategies/` as a package (keep `STRATEGIES` import
compatible):

```
Strategy protocol
  id, label, legs_template
  build_candidates(chain, spot, context, request) -> list[Position]
Position
  legs: list[Leg(option_type, side, strike, expiration, qty, bid, ask, mid, iv, greeks)]
  net_credit_mid / natural / modelled_fill
  payoff(S_T) ; value(S, t, iv_fn)
  max_profit, max_loss, breakevens, collateral (BPR), net_greeks
  fees (costs.py generalised per leg, per-leg commission cap)
```

- **CSP**: port `candidates.evaluate_strike` into this shape without changing
  its numbers (regression test against the current output).
- **PCS**: short put chosen by rule (delta band, outside k×EM, or below the
  strongest respected support minus median pierce depth — user picks; default
  shows the most conservative that passes). Long legs at each width in the
  request (dollar widths, e.g. 1/2.5/5/10, snapped to listed strikes) →
  credit, credit/width (flag < 1/3), max loss, BPR, return on risk,
  breakeven, POP at breakeven, P(max loss), EV over the empirical terminal
  distribution, liquidity of *both* legs, net Greeks. Label each width
  Conservative / Moderate / Aggressive.
- Multi-leg fill model: net mid minus `slippage_fraction_of_half_spread` of
  the summed half-spreads; also show natural (short bid − long ask).
- Index options (SPX/XSP/NDX/RUT): cash-settled, European, no assignment —
  CSP does not apply; PCS does. Flag AM- vs PM-settlement. Verify the vendored
  client's underlying quote path (currently `instrument_type="equity"`)
  handles indices.

### B.7 Account profiles

Tom trades a Roth IRA, a traditional IRA and a taxable account. Add
`account_profiles` in config (capital, allowed strategies, cash-secured
requirement, max per-position, spread approval) and let the screener run
against a chosen profile. The current single `account:` block becomes the
default profile.

### B.8 Other features worth adding

- Scheduled nightly data refresh (Windows Task Scheduler →
  `python pipeline/run.py --data-only`) so the interactive run only pulls
  chains.
- Saved scan presets from the new input form (name + full `ScanRequest`).
- Watchlist alerts when a saved scan produces a new top-ranked trade.
- Portfolio view extended to multi-leg: beta-weighted delta (to SPY),
  theta/day, BPR utilisation, event exposure calendar.
- Excel/CSV export of any results grid.
- Data-quality panel: freshness per source, adjustment-seam check, earnings
  source disagreements, cache ages.
- Historical options data decision: multi-leg backtests (calendars especially)
  need real historical quotes. Evaluate Massive's options plans (you already
  use Massive), ORATS, or CBOE DataShop before Phase 14.

---

## Part C — Work orders for Claude Code

### Ground rules for every phase

1. Read `docs/SCREENER_ROADMAP.md` (this file), `docs/ARCHITECTURE.md`,
   `docs/PHASE1_NOTES.md` and the docstrings of any module you touch before
   changing it. The module docstrings hold the reasoning behind Phases 1–7 —
   do not undo a documented decision without asking.
2. Propose a short plan and list the **decisions to confirm** for the phase;
   wait for Tom's answer before building. Reasonable defaults are fine to
   propose.
3. All paths through `core.paths`; all thresholds in `config.yaml`; no
   Streamlit imports in `analytics/` or `data_sources/`.
4. Validate against real data on disk wherever it exists; synthetic fixtures
   only for unit tests of formulas.
5. Every phase adds `tests/test_phase{N}.py`. `python -m pytest tests -q` must
   be green, and every page must pass the headless check
   (`streamlit.testing.v1.AppTest.from_file(...).run()` with no exceptions).
6. End each phase with `docs/PHASE{N}_SUMMARY.md` (what changed, decisions,
   known limits, verification evidence), update `docs/ARCHITECTURE.md`, and
   commit + push.
7. Research/analysis tool only: no order placement, no "place trade" UI.

---

### C.0 Phase 8 — Baseline, version control, consolidation, data-basis fixes

**Goal:** a clean, versioned, single-stack foundation with correct prices.

Tasks
1. Run `scripts/preflight.py` and the full test suite; record results in the
   summary. Fix anything broken before proceeding.
2. `git init`; confirm `.gitignore` excludes `.env`, `data/`, `.venv/`,
   `__pycache__/`, `.pytest_cache/`, large outputs. First commit. Ask Tom for a
   private GitHub remote URL and push.
3. **Fix the adjustment bug** (§A.3.1): new table `daily_bars_raw` (raw
   OHLCV from `auto_adjust=False`, `adj_close`, dividends, splits). Full
   re-pull of a ticker whenever the incremental window contains a new
   dividend or split. Provide `load_daily(ticker, basis="price"|"total")`;
   keep `load_daily_total_return` as a thin wrapper.
4. **Fix the probability basis** (§A.3.2): `moves.py`, `candidates.basis_assessment`,
   `gaps.py`, `vrp.py`, `technicals.py` use `basis="price"`;
   `wheel_backtest.compare_to_buy_and_hold` and long-run performance use
   `"total"`. Add a test showing a synthetic ex-dividend drop is present in
   the price basis and absent in the total basis, and a before/after table of
   P(breach) for MO, T, KO, PBR in the summary.
5. **Persist run results:** write the decision sheet (all rows, including
   rejected, with the census) to `data/runs/<id>/candidates.parquet` and open
   positions evaluation to `positions.parquet`. Add
   `pipeline.results.latest_run()`; every page falls back to the latest run on
   disk when session state is empty, and shows the run id/time.
6. **Retire the legacy stack:** move `1_Scanner.py`, `2_Ticker_Detail.py`,
   `3_Trade_Log.py`, `scoring.py`, `backtest.py`, `trade_log.py` and the
   `stage3_chains` readers in `data_access.py` to `legacy/` (not imported by
   the app). Before moving: migrate any rows in `trade_log.duckdb` into the
   paper book, and list which Ticker Detail charts will be reused in Phase 13.
   Remove the legacy pages from navigation.
7. Preflight: add cache-age warnings (1-minute cache, daily bars, events).
8. Rewrite `docs/ARCHITECTURE.md` to describe the system as it now is.

Decisions to confirm: GitHub remote; moving vs deleting legacy files; whether
to keep `scripts/01–06` (recommend keep, documented as "universe rebuild from
the 1-minute archive").

Acceptance: tests green; app has no legacy pages; restart → Decisions still
shows the last run; P(breach) comparison table in the summary.

---

### C.1 Phase 9 — Universe registry, yfinance coverage, events

**Goal:** any stock/ETF/index can be added and gets daily bars, weekly bars,
events and IV metrics automatically.

Tasks
1. `data_sources/universe.py` + `universe` table (§B.2). Seed from
   `final_universe.txt` + `stage2_quality_tags_master.csv`; add SPY, QQQ, IWM,
   DIA and ^SPX/XSP/^NDX/^RUT. `core.paths.load_universe()` reads the registry
   (keep the text file as an import format). A simple "Universe" page to
   add/remove/tag symbols.
2. Batch yfinance sync (`yf.download`, threads, retry with backoff, polite
   pacing) for the whole registry, including indices. Nightly, not on the
   interactive path when data is current.
3. `analytics/bars.py`: weekly (W-FRI) resample from daily; helper returning
   the last *completed* weekly value as of any date.
4. Stage 1 on yfinance data: `analytics/universe_screen.py` reproducing
   `scripts/02` thresholds against `daily_bars_raw`; results written to the
   registry as `stage1_pass` + reasons.
5. `data_sources/tasty_metrics.py`: call `/market-metrics` in batches;
   store daily in `market_metrics` (IVR, IVP, IV index, liquidity rating,
   expected earnings date, beta, per-expiration IV). Verify and document the
   live field names. Wire IVR/IVP into `candidates.py` and the Validation
   page's IV coverage section (own-history IVR stays as a cross-check).
6. `data_sources/events.py` + `events` table + `config/macro_calendar.yaml`
   (Tom maintains FOMC/CPI/NFP dates; seed with the published 2026–2027 FOMC
   schedule, verified from the Fed's site). Computed OPEX/quad witching.
   Earnings merged from yfinance and tasty metrics with a `sources_disagree`
   flag.
7. `config.yaml` → `event_policy:` per type (`block|warn|ignore`, window in
   days, applies_to strategies). Replace the direct earnings gate in
   `candidates.py` with an `events.check(symbol, start, end, strategy)` call.
8. `analytics/earnings_history.py`: per ticker, last N earnings reactions
   (gap %, close-to-close %, vs ATR), plus implied move when stored IV exists.

Decisions: initial registry size; whether to include single-stock names
without weeklies; default event policies (recommended: earnings = block,
FOMC/CPI = warn, ex-div = warn for short calls only).

Acceptance: adding a symbol in the Universe page and running the pipeline
yields daily + weekly bars, events, and market metrics for it; indices load;
tests cover symbol mapping, weekly no-lookahead, event windows.

---

### C.2 Phase 10 — Technical indicators and level-respect study

**Goal:** per ticker, know which MAs/levels it has historically respected,
how strongly, and where the nearest strong support is.

Tasks
1. `analytics/indicators.py` (§B.3 list), daily and weekly, vectorised,
   cached to `indicator_latest` and computed on the price basis.
2. `analytics/level_respect.py` exactly as specified in §B.3: event
   detection, debouncing, outcomes at 5/10/20 days, pierce depth, Wilson CIs,
   placebo baseline, slope split, recency-weighted rate, min-n rule. Results
   to `level_stats(symbol, level_id, timeframe, n, hold_rate, ci_lo, ci_hi,
   edge_vs_placebo, median_pierce_atr, last_test_date, slope_regime)`.
3. `analytics/oscillator_study.py`: RSI extreme forward-return study.
4. `analytics/trend_state.py`: uptrend/range/downtrend classifier.
5. `support_map(symbol)`: ordered list of levels below spot with strength
   (CI lower bound of edge), distance in %, ATR and EM units.
6. Signals page: new "Levels" tab — per-ticker table of levels ranked by
   strength and a chart with the levels drawn.

Decisions: band/tolerance defaults (0.5 ATR / 1 ATR proposed), min n, which
levels count as "strong" for strike placement.

Acceptance: tests on synthetic series with known bounces (detector finds
exactly the planted tests; placebo edge ≈ 0 on a random walk); summary shows
SPY, AAPL, KO level tables with sample sizes.

---

### C.3 Phase 11 — Scan request, underlying ranking, targeted chain capture

**Goal:** the workflow in Tom's brief, up to "chains downloaded for the top
candidates".

Tasks
1. `analytics/scan_request.py` — `ScanRequest` dataclass: `strategies`
   (csp/pcs), `dte_min/dte_max` or `dte_targets`, risk mode (`delta_range` |
   `min_pop` | `max_loss_per_trade` | `max_pct_capital`), `spread_widths`,
   `profit_targets` (default [25, 30, 50, 100]), `account_profile`,
   `universe` (preset/list), `top_n_underlyings` (default 15),
   `event_policy_overrides`, `strike_rules` (delta / EM multiple / support).
   Serialisable to JSON; stored in the run manifest.
2. `analytics/underlying_rank.py`: score every registry symbol without
   chains — trend state, nearest strong support distance (EM units), IVR/IVP,
   IV/RV proxy (tasty IV index vs RV), liquidity rating, event blocks inside
   the requested DTE, drawdown, capital feasibility for the chosen profile.
   Component breakdown kept per row. Weights in config, flagged as
   unvalidated until Phase 14 calibration.
3. `pipeline/run.py`: accept a `ScanRequest`; new stages `rank_underlyings`
   → `chains` (top N only; DTE window = request max + roll buffer) →
   `construct` → `probabilities` → `rank_trades`. Keep the existing data
   stages and staleness logic. Add `--data-only` for the nightly job.
4. `data_sources/chains.py`: DTE window from the request; **strike filter**
   before subscribing (only strikes within spot ± 3×EM, puts and calls as
   needed) to keep 30–60 DTE pulls inside the dxFeed budget. Verify index
   underlyings.
5. Remove the forced one-per-ticker/top-3 output; keep `proposed` as a flag
   and add a "best per ticker" view toggle.

Decisions: default top N; default ranking weights; roll buffer (+14 days
proposed).

Acceptance: a CLI run `python pipeline/run.py --request examples/pcs_30_45.json`
ranks underlyings, pulls chains only for the top N, and writes the manifest
with the request embedded.

---

### C.4 Phase 12 — Options analytics, strategy package, PCS construction

**Goal:** every candidate trade (CSP and PCS) fully specified with pricing,
liquidity, expected move and risk.

Tasks
1. `analytics/expected_move.py` (§B.5) including historical containment.
2. `analytics/liquidity.py`: per-leg and per-position profile — OI, volume,
   bid/ask $ and %, OI concentration ("walls"), fillability score; sizing
   caps from `sizing.py` applied to the *least* liquid leg.
3. `analytics/strategies/` package (§B.6). Port CSP with a regression test
   proving identical EV/fill/contract numbers to the Phase-8 output. Build
   PCS with the width ladder and risk tiers.
4. `costs.py`: multi-leg fees (per-leg open commission and cap, clearing,
   regulatory), spread close/expiry/assignment cases, early-assignment note.
5. Premium-opportunity flags: IVP ≥ threshold and IV/RV ≥ threshold;
   credit/width ≥ 1/3 for PCS.
6. Every trade row carries: which strike rule chose the short strike and
   why, EM distance, nearest support and its stats.

Decisions: default widths; strike-rule default; credit/width floor;
whether PCS on single stocks requires the same earnings block as CSP
(recommended yes).

Acceptance: unit tests for PCS payoff/max loss/breakeven/BPR; EM formula
tests against hand calculations; real-data run producing PCS rows for SPY and
QQQ at several widths.

---

### C.5 Phase 13 — Probability engine and profit targets

**Goal:** the probability table in Tom's brief.

Tasks
1. `analytics/prob_engine.py` implementing models G, H, T (§B.4) with a
   shared path/repricing core (numpy vectorised, antithetic variates, fixed
   seed per run for reproducibility, default 20,000 paths). Generic over
   `Position`, so any future strategy reuses it.
2. Outputs per trade (§B.4 list), including per-policy expected P&L,
   holding days and annualised return net of fees, and the
   below-minimum-net-gain marker.
3. Blended probability with config weights; each model's effective n shown.
4. Walk-forward validation script `scripts/validate_prob_engine.py` and a
   Validation page section: predicted vs observed P(hit X% by d).
5. Ranking: default sort = blended EV-per-day on BPR subject to the request's
   risk mode; alternative sorts selectable in the UI.

Decisions: blend weights; IV dynamics defaults (sticky strike, earnings
crush size); which management policy is the default headline.

Acceptance: G-model MC matches N(d2) within 3 standard errors; engine runs
for 15 tickers × all candidates in under ~60 s on Tom's machine (measure and
report); validation results in the summary with honest caveats.

---

### C.6 Phase 14 — Screener UI and Trade Deep-Dive

**Goal:** the click-through workflow.

Tasks
1. New **Screener** page (becomes the landing page with Command Center's
   status banner on top):
   - Input form: strategy (CSP / PCS / both), DTE range or target list,
     risk mode + value, spread widths, profit targets, account profile,
     universe, top N, event overrides, strike rule. Save/load presets.
   - Run with the existing `StreamlitReporter` stage progress, including the
     freshness check ("data current / downloading X").
   - Results grid (`st.dataframe`, `on_select="rerun"`,
     `selection_mode="single-row"`, `column_config` progress bars for
     probabilities): Ticker, Strategy, Expiry, DTE, Short K, Long K, Width,
     Credit (modelled / mid), Max loss, BPR, RoR, Annualised, EV, POP,
     P(25%), P(30%), P(50%), P(100%), median days to 50%, IVR, IV/RV, EM
     distance, nearest support + strength, liquidity score, event flags.
     Multiple rows per ticker; group/"best per ticker" toggle; filters;
     CSV/Excel export.
   - Selecting a row → `st.switch_page` to Trade Detail with the trade id in
     `st.query_params` (so the URL is bookmarkable within the run).
2. **Trade Detail** page, loaded from the persisted run by trade id, tabs:
   - *Summary*: plain-English thesis, verdict, risk flags, why this strike.
   - *Chart*: daily/weekly candles, MA overlays, respected levels with
     stats, strike lines, EM cone (IV and straddle) to expiry, event markers.
   - *Expected move*: empirical vs lognormal terminal distribution with
     strikes and EM bands.
   - *Payoff*: at expiry + T+n curves for any number of legs, breakevens.
   - *Probabilities*: P(hit X%) vs day curves per model, target × day table,
     touch/assignment/max-loss odds, policy comparison.
   - *Greeks*: net Greeks now and over time; price × IV scenario heatmap.
   - *Chain & liquidity*: strikes around the trade, OI/volume bars,
     bid/ask width, fill estimate.
   - *Management plan*: target, time stop, roll trigger, roll preview
     (`roll_engine`), and for CSP the assignment → covered-call preview.
   - *Context*: earnings reaction history, gap risk, regime, correlation with
     the current book.
   - *Accept*: record to the paper book (needs Phase 15 multi-leg schema;
     until then CSP only).
   Reuse and generalise the chart builders in `app/components/charts.py`.
3. Keep Decisions (accept/override flow) but feed it from the Screener
   selection.

Acceptance: AppTest passes for both pages; a PCS and a CSP row each open a
fully populated Trade Detail; screenshots of both in the summary.

---

### C.7 Phase 15 — Multi-leg paper book, management and validation

1. Paper schema: `positions` + `legs` tables (migrate existing single-leg
   rows; keep cycles/share_lots for the wheel).
2. `exit_rules.py`: PCS management (profit target, loss stop at k×credit,
   time stop, roll-for-credit), same net-of-fee logic.
3. Calibration extended to multi-leg and to P(hit X%) predictions.
4. PCS rule backtest (synthetic BS pricing on real paths, like
   `wheel_backtest.py`) with walk-forward over widths/deltas/targets.
5. Portfolio page: multi-leg exposure, beta-weighted delta, theta/day, BPR
   utilisation, event calendar.

---

### C.8 Phase 16+ — Multi-strategy screener

1. **Strategy spec DSL** (YAML under `strategies/`): legs with
   `type, side, ratio, selector (delta | moneyness | EM multiple | ATM),
   expiration (dte target/range, front/back)`, entry conditions (IVR range,
   trend state, event policy), exit policy. Examples to ship: bull put,
   bear call, iron condor, strangle (margin accounts only), covered call,
   long call calendar 14/21 DTE ATM, diagonal/PMCC.
2. Generic resolver: spec + chain → positions; pricing and the probability
   engine are already generic. Calendars need the back leg valued at the
   front expiry from a term-structure assumption — flag as model risk.
3. **Recommender:** condition matrix (IV regime × trend state × upcoming
   event) → applicable strategy families; run each, rank by calibrated
   risk-adjusted EV. User can also pick one spec and scan the universe for
   its best instances.
4. Margin/BPR per strategy class (defined risk = max loss; undefined =
   broker formula approximation) and account-profile permissions.
5. Historical options data integration once purchased (§B.8), replacing
   synthetic pricing in backtests for multi-leg strategies.
