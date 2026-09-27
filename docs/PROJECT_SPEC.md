# CSP Wheel Candidate Analysis Tool — Project Spec

## Goal

A local research/analysis tool (not a live trading tool, initially) for
selecting cash-secured put candidates for a wheel strategy, on a 5-14 DTE
weekly/biweekly timeframe. The user wants to *avoid* assignment for as long
as possible while collecting premium, and secondarily wants to be comfortable
owning the underlying if assigned (i.e. only high-quality, liquid names).

The tool combines:
1. Historical daily + 1-minute price data (per-ticker, local files)
2. Point-in-time option chain snapshots (captured manually/periodically)

...to score and rank CSP candidates, and to backtest how well a given
delta/DTE selection rule would have performed historically on a given
underlying.

## Data sources

- **1-minute price history**: `D:\pricing_data\stocks_etfs\1m\<TICKER>\`,
  one file per ticker-month: `<TICKER>_<YYYY-MM>_1m.txt`, columns
  `Datetime,Open,High,Low,Close,Volume`. Confirmed format details:
  - Rows within a file are in **reverse chronological order** — do not rely
    on file row order; always sort explicitly by timestamp on ingestion.
  - Timestamps are naive (no timezone) local wall-clock, spanning extended
    hours (as early as 04:00, as late as 20:00) in recent (massive.com-
    sourced, ~2024+) data. Pre-2024 (AlphaVantage-sourced) data has thinner/
    sparser extended-hours coverage — expect lower bar density outside
    09:30-16:00 in older history, this is a real feature of illiquid
    extended-hours trading, not a data defect.
  - Price precision and line endings vary by source era (AlphaVantage vs.
    massive.com) — not a functional problem, but don't assume a fixed decimal
    format or line-ending convention when writing custom parsers; DuckDB's
    CSV reader handles both automatically.
  - A handful of tickers exist under more than one folder name due to source
    naming differences (e.g. `BRK.B` vs `BRK-B`). Resolved via the alias
    logic in `scripts/01_build_daily_summary.py` — keep that resolution
    approach (or improve it) rather than assuming folder name == unique
    ticker.
  - Data is current through end of June 2026 as of this writing; the user
    updates it periodically and re-syncs via `scripts/03_copy_selected_tickers.py`.
- **Option chain snapshots**: TastyTrade, via the user's own
  `tastytrade_common.py` / `snapshot_loop.py` pipeline (DXLink streaming +
  REST). Confirmed schema: one row per (expiration, strike), with
  `call_`/`put_` prefixed `last, bid, ask, mark, volume, open_interest, iv,
  delta, gamma, theta, vega, rho` plus `call_symbol`/`put_symbol`. Bid/ask/
  mark/last come from REST (`/market-data/by-type`); open interest and all
  Greeks/IV come from DXLink streaming (Summary + Greeks events) --
  REST alone is NOT sufficient, this was confirmed by reading the actual
  helper functions (`apply_rest` only populates bid/ask/last/mark). Subject
  to TastyTrade/dxFeed's enforced limits: 5 concurrent sessions, 25,000
  subscriptions/session, 10,000 subscription changes/minute (rolling
  window). Historical option chain data is NOT available via TastyTrade's
  API -- only live/current snapshots, which is why the user is
  self-accumulating history via `snapshot_loop.py` and plans to purchase
  proper historical option data (CBOE DataShop or similar) once specific
  strategies are validated against the self-collected + synthetic data.

## Universe narrowing (already scoped/built — see README.md)

Three-stage funnel from ~1,050 tickers down to a tradable list:

1. **Stage 1 (quant filter, scripted)** — liquidity (ADV $), price range,
   realized volatility band, no unrecovered massive drawdown, non-stale data.
   Splits survivors into two tiers rather than a single pass/fail: **Tier 1**
   (also meets the minimum history requirement, so it's been observed through
   at least one real drawdown) and **Tier 2** (passes every other filter but
   has less history — in practice because the user's source data for ~58% of
   the universe only goes back to ~2024, not because the underlying is lower
   quality). Tier 2 names should be usable for forward/paper testing once
   Stage 2/3 review clears them, with the caveat that their backtest coverage
   is inherently thinner until more history accumulates. Runs against a
   compact daily-bar summary built from the full 1-minute universe
   (`01_build_daily_summary.py` → `02_stage1_screen.py`).
2. **Stage 2 (manual quality tagging)** — sector, tier (core mega-cap /
   high-vol leader / commodity / bond ETF / country ETF / speculative),
   leveraged-or-inverse-product exclusion, inception-date sanity check
   (exclude anything with <1-2 years of real trading history regardless of
   current hype/liquidity — e.g. a newly-launched thematic ETF that gathers
   huge AUM fast is not yet a known quantity for wheel purposes). A first
   pass has been drafted (`output/stage2_quality_tags_master.csv`): Tier 1
   is fully hand-tagged; Tier 2 has automated asset-class and leverage
   detection only (this caught 9 leveraged/inverse products -- e.g. UPRO,
   TNA, SPXL -- that Stage 1's liquidity/volatility filters alone did not),
   with individual stock sectors left as a TBD follow-up given the volume
   (400+ names). Revisit full automation only if the manual/incremental
   tagging burden becomes real.
3. **Stage 3 (chain liquidity confirmation) — built.** For Stage 2
   survivors (leverage-flagged names excluded), `04_stage3_chain_scan.py`
   pulls each symbol's chain restricted to the 5-14 DTE trade window via
   TastyTrade (REST for bid/ask/mark, one DXLink streaming collection per
   symbol for OI/Greeks/IV, reusing `pull_equity()` from the user's own
   `snapshot_loop.py` unchanged) and `05_stage3_screen.py` applies the
   liquidity filter: OI at the nearest-to-target-delta strike, bid/ask
   spread %, and strike density within the delta band. Requires a small
   additive patch to `tastytrade_common.py` (`tastytrade_patch/` in this
   delivery) adding a `between_M_N_dte` expiration-selection token — this
   is what keeps per-symbol subscription counts small enough (600-3,000
   vs. ~23,000 for a full 45-DTE chain) to batch-scan ~550 symbols without
   hitting dxFeed's rate limits. A rolling-window rate limiter paces the
   batch to stay under a configurable subs/minute budget.

   **Important distinction discovered during this phase:** the user's
   existing `snapshot_loop.py` is a different tool for a different job —
   narrow-and-deep continuous collection (1-2 "hero" symbols like
   SPY/XSP or SPX/NDX, 5-min cadence, for validating the analytical
   engine against real data) vs. Stage 3's broad-and-shallow one-shot
   scan (many symbols, narrow DTE, run periodically not continuously).
   Both share the same `tastytrade_common.py` primitives but should stay
   separate scripts/workflows -- don't try to unify them.

Only Stage 3 survivors get their full 1-minute history copied into
`D:\csp\data\raw_1m\` for use by the analysis engine below.

## Analysis engine (next phase — not yet built)

For each finalized underlying:

- **Realized volatility** — multiple lookback windows (10/20/30/60d), both
  close-to-close and a range-based estimator (Parkinson or Garman-Klass)
  using the 1-minute data for a tighter estimate than daily closes alone.
- **IV Rank / IV Percentile** — built from the user's own accumulated chain
  snapshot history per ticker, not a vendor number. Requires the snapshot
  storage schema above to be in place first.
- **Technical context** — moving averages, distance from 52-week range, RSI
  or similar, recent drawdown — used as a *filter/sanity check* against
  selling puts into a clear downtrend, not as a primary ranking signal.
- **Options math** — Black-Scholes pricing/Greeks computed from chain data
  (strike, DTE, underlying price, IV) to get theoretical delta and
  probability-OTM even for snapshots that don't already include delta.
- **Composite score** — combines annualized premium yield, IV rank,
  liquidity score, technical health, and probability-OTM (both theoretical
  and backtest-calibrated — see below) into a ranked candidate list per
  DTE/delta target.
- **Backtest harness (high priority, not optional)** — for a given
  delta/DTE selection rule, replay history and measure: actual win rate
  (expired OTM), assignment frequency, average realized return, worst
  drawdown of the strategy. This is what corrects for Black-Scholes'
  tendency to overstate real-world win rates for short premium strategies
  (fat tails, vol clustering) — the theoretical POP from Stage 3 chain data
  should be treated as a starting estimate, not a final answer, until
  validated against this backtest per-ticker.

## Application / GUI requirements

Reference model: [Option Samurai](https://optionsamurai.com/) — a
commercial multi-strategy option scanner (24 strategies, 100+ filters,
Excel export, broker order transmission, multi-user SaaS). The user wants
the same core workflow (**scan → analyze → log**) at a deliberately
smaller, single-purpose scale: one account, wheel-strategy-only (CSP +
covered calls), local desktop app, no execution. Scoping down from their
feature list turned out to be the more useful exercise than trying to
match it feature-for-feature — most of their surface area (custom
multi-leg strategies, broker transmission, Excel integration, multi-user
alerts) is explicitly out of scope here; the sections below are the
subset worth building, mapped to their closest Option Samurai equivalent
for reference.

- Launched via `python launch.py` from PowerShell at the project root,
  standing up both frontend and backend from one command.
- Planned implementation: **Streamlit** for the GUI (fastest iteration for
  data-heavy, chart-heavy, frequently-changing research tooling), with
  `launch.py` as a thin wrapper that invokes `streamlit run app/main.py`
  (plus starts any backend/data-refresh process if one ends up separate) so
  the user-facing entry point stays a single `python launch.py` regardless
  of internal implementation. Revisit this choice only if a real need for a
  separate persistent backend process emerges (e.g. background chain-snapshot
  scheduling) — don't build FastAPI+separate-frontend complexity
  speculatively.

### Page 1 — Scanner (≈ Option Samurai's "Scan the market")

The main/landing view. A ranked, filterable/sortable table of CSP
candidates driven by the composite score (yield, IV rank, liquidity,
technical health, backtest-calibrated probability-OTM — see Analysis
engine section above). Columns should include at minimum: ticker, DTE,
strike, delta, annualized premium yield, IV rank, OI/spread (from Stage 3),
composite score.

- **Saved scan presets** (≈ their "Predefined Trade Scans" / "Saved
  scans"): given the narrow strategy scope here, this doesn't need a
  general-purpose scan builder with 100+ filters — a small, fixed set of
  presets is enough to start (e.g. "CSP candidates, 5-14 DTE", "Covered
  call roll candidates" for positions currently assigned). Store presets
  as simple config (thresholds + universe), not a generic query language,
  unless a real need for more flexibility shows up later.
- **Universe control** (≈ their "Universes"): filter the scan to
  Tier 1/Tier 2/all, or a hand-picked ticker subset — reuses
  `stage2_quality_tags_master.csv` / `stage3_universe.txt` directly, no new
  concept needed.
- Data refresh controls surfaced here rather than requiring the command
  line: trigger `03_copy_selected_tickers.py`-equivalent sync, trigger a
  new Stage 3 chain scan.

### Page 2 — Ticker detail (≈ Option Samurai's "Trade Window" / OptionStrat's strategy builder)

Drill-in view for a single candidate. Should include:
- Price chart with realized-vol bands overlaid, and the specific
  strike/expiration marked on it.
- **IV rank / IV vs. realized-vol chart** (≈ Option Samurai's "Implied
  Volatility suite") — built from the user's own accumulated chain-snapshot
  history, not a vendor feed, which is the main differentiator vs. either
  reference site.
- **P&L / breakeven diagram** (≈ OptionStrat's strategy builder) — standard
  short-put payoff diagram at expiration, breakeven marked. Also chart P&L
  **over time**, not just at expiration (≈ their Greeks-over-time charting)
  — shows how theta decay accrues day by day under a few IV/price
  scenarios, since that's the actual shape of a CSP's return path, not just
  its terminal payoff.
- **Net Greeks panel** (≈ OptionStrat's "Net greeks") — delta, theta,
  gamma, vega for the position, both as numbers and charted across the
  DTE window so the user can see how theta/gamma risk shifts as expiration
  approaches.
- **Probability cone / chance of profit** (≈ OptionStrat's "Chance of
  profit and probability distribution") — expected-move-based price range
  visualization (a "range scale" showing 1σ/2σ bands to expiration) plus
  the numeric probability-OTM (both theoretical Black-Scholes and, once
  available, backtest-calibrated).
- **Strike/DTE what-if explorer** — this is the direct answer to "let me
  assess different DTE, different strikes": interactive controls
  (dropdowns or sliders for expiration and strike) that recompute every
  chart and metric on this page live against the same underlying's chain
  data, plus a small ranked table of nearby strikes/expirations by score —
  a lightweight, ticker-scoped version of OptionStrat's market-wide
  "Optimizer," scoped down to one underlying at a time rather than
  scanning everything (the Scanner page already covers the market-wide
  case).
- **Volume/OI overlay on the chain table** (≈ OptionStrat's "Volume
  overlay") — visually highlight the most active strikes for calls and
  puts, not just list numbers in a table.
- Optional commission input (≈ OptionStrat's "Commissions") feeding into
  net-credit/yield calculations — small addition, meaningfully changes
  realized yield on lower-premium short-DTE trades.
- Chain table for that ticker/expiration: theoretical vs. market pricing,
  OI, spread — reuses Stage 3 output directly.

### Page 3 — Trade log (≈ their "Trade Log / Trading Journal")

Not in the original screening-phase scope — added here because it directly
serves the user's stated goal ("continue to sell CSP without assignment as
long as possible"): without a log, there's no way to see, per ticker, how
often a position actually got rolled vs. assigned over time, which is the
real-world feedback the backtest calibration needs. Minimum viable version:
- Manually log opened positions (ticker, strike, expiration, premium
  collected, date).
- Mark outcomes over time: expired OTM / rolled / assigned.
- Summary stats per ticker and overall: win rate, assignment frequency,
  average annualized return realized — the actual-outcome counterpart to
  the backtest's theoretical numbers.
- This is a local log, not a broker-synced portfolio (≈ they support
  "Unlimited trading accounts" with live broker sync — out of scope here;
  manual entry is enough for a single-account research tool and avoids
  another broker-API integration surface).

### Look, feel, and extensibility

Explicit user request: the app should look and feel like a polished
commercial product, not a bare-default Streamlit script — partly for its
own sake, partly because the user wants room to expand it later (more
strategies, more data sources) without a rebuild.

- Custom Streamlit theme (`.streamlit/config.toml`) with a deliberate color
  palette and typography, not defaults. Consistent sidebar navigation
  across pages, a real app name/header, consistent number/chart
  formatting throughout.
- Use **Plotly** (already in requirements.txt) for all charts rather than
  matplotlib/st.line_chart — interactive zoom/hover/pan reads as more
  "commercial-grade" and is genuinely more useful for the chart types
  above (probability cones, P&L-over-time, Greeks-over-time all benefit
  from hover tooltips).
- Structure the codebase so strategies/pages are modular and addable, not
  hardcoded assumptions of "CSP only" throughout — e.g. a
  strategy-definition layer that covered calls (already in scope) and
  future strategies (spreads, etc. — not in scope now) could plug into
  later, even though only CSP + covered call are being built today. Don't
  over-engineer this into a plugin framework prematurely; just avoid
  hardcoding CSP-specific logic where a strategy parameter would do.

### Explicitly out of scope (vs. Option Samurai / OptionStrat's full feature sets)

- Custom multi-leg strategy builder (Option Samurai's "Custom Scan";
  OptionStrat's 50+ prebuilt multi-leg strategies) — this tool is
  wheel-strategy-only (CSP + covered call), not a general options scanner.
  Revisit only if the user wants to extend beyond the wheel.
- Broker order transmission — research/analysis only, no execution.
- Unusual options flow / congress-trade tracking (OptionStrat's flagship
  feature) — not relevant to a systematic wheel-candidate screener; pure
  novelty/momentum signal, doesn't fit the "own it if assigned" philosophy
  this tool is built around.
- Excel/Google Sheets data export toolbox, multi-user accounts, intraday
  scan-watching/alerts, Monte Carlo scenario simulator — all reasonable
  future extensions, none needed for the core workflow above. Don't build
  speculatively.
- Not a trading tool initially — no order placement, no broker integration.
  Keep that boundary explicit in the UI (no "place trade" affordances)
  unless/until the user asks to extend into execution.

## Working conventions

- `D:\csp` is the project root; all new code, data, and outputs live here.
  `D:\pricing_data\...` is treated as read-only source data, never modified
  by any script.
- DuckDB is the default analytical data store (already proven fast and
  simple for this data shape in the screening phase — no reason to introduce
  Postgres/etc. unless a real concurrency or scale need appears).
- Config lives in `config.yaml` at the project root; scripts should keep
  reading from there rather than hardcoding paths, so the same codebase
  works whether it's being run interactively or from the GUI.
