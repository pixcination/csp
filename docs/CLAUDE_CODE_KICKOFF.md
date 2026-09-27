# Claude Code kickoff prompt — CSP Wheel Analysis Tool

Paste this entire file as your first message to Claude Code, run from a
terminal opened at `D:\csp`. (Or just tell Claude Code "read
CLAUDE_CODE_KICKOFF.md and PROJECT_SPEC.md, then begin" — either works.)

---

## Context

Read `PROJECT_SPEC.md` in this folder in full before doing anything else —
it's the complete brief: the strategy this tool supports (cash-secured put
wheel, 5-14 DTE), the three-stage screening pipeline that's already built
and validated (`scripts/01` through `05`), the confirmed data formats for
both the 1-minute price history and the TastyTrade option chain snapshots,
and the page-by-page design for the application itself.

**Everything for this project lives inside `D:\csp`.** Don't create
anything outside this folder. The screening pipeline (`scripts/`,
`config.yaml`, `output/*.csv`, `data/`) already exists and works — you're
building the application layer on top of it, not replacing it.

## What's already built (don't redo this)

- `scripts/01_build_daily_summary.py` → `05_stage3_screen.py`: the full
  screening pipeline, tested against real data, producing
  `output/stage3_candidates.csv` (the current tradable universe) plus the
  Tier 1/Tier 2 daily data in `data/universe_daily.duckdb`.
- `tastytrade_patch/`: a small, tested patch to the user's existing
  TastyTrade integration (`tastytrade_common.py` / `snapshot_loop.py`,
  living outside this folder — path is in `config.yaml` as
  `tastytrade_pipeline_dir`) that adds narrow-DTE-window chain scanning.
- Raw 1-minute price data for the finalized universe, once
  `03_copy_selected_tickers.py` has been run, lands in `data/raw_1m/`.
- Chain snapshots from Stage 3 scans live in `data/stage3_chains/<date>/`.

## What you're building

The analytics engine and GUI described in `PROJECT_SPEC.md`'s "Analysis
engine" and "Application / GUI requirements" sections. In short:

1. **Analytics engine**: realized volatility (multiple windows/estimators),
   IV rank built from accumulated chain snapshots, Black-Scholes/Greeks,
   a composite scoring model, and a backtest harness that replays history
   for a given delta/DTE rule.
2. **Three-page Streamlit app** (`launch.py` → `streamlit run` under the
   hood, single-command startup):
   - **Scanner**: ranked/filterable candidate table, saved scan presets,
     data-refresh controls.
   - **Ticker detail**: price+vol chart, IV rank/history chart, P&L
     diagrams (at expiration AND over time), net Greeks (numeric + charted
     over the DTE window), probability cone / chance-of-profit, a
     **strike/DTE what-if explorer** (interactive controls that recompute
     everything live — this is the direct answer to "let me try longer
     DTE / different strikes"), volume/OI overlay on the chain table.
   - **Trade log**: manually logged positions, outcome tracking (rolled /
     assigned / expired), realized win-rate stats vs. the backtest's
     theoretical numbers.
3. **Commercial look and feel**: custom Streamlit theme, real navigation,
   Plotly for all charts (not matplotlib/st.line_chart) — see "Look, feel,
   and extensibility" in `PROJECT_SPEC.md` for the reasoning. Structure
   the code so it's not hardcoded CSP-only throughout, since the user
   wants to extend this later — without over-building a plugin framework
   prematurely for strategies that don't exist yet.

## How I'd like you to work

- **Propose a plan before writing a lot of code.** Given the scope above,
  start by proposing a folder structure (e.g. `app/`, `app/pages/`,
  `analytics/`, `app/components/`) and a build order, and confirm it makes
  sense before generating the full app. A reasonable incremental order:
  skeleton app + `launch.py` + theme → Scanner page wired to real
  `stage3_candidates.csv` data → analytics engine (vol/IV rank/Greeks) →
  Ticker detail page → backtest harness → Trade log → polish pass.
- **Validate against real data, not synthetic placeholders, wherever
  real data already exists** (it does — `output/stage3_candidates.csv`,
  `data/universe_daily.duckdb`, the sample chain parquet files this
  pipeline already produced). Don't build against invented mock data when
  the real thing is sitting right there.
- **Ask before making a judgment call that materially changes scope** —
  e.g. exact composite scoring weights, which specific technical
  indicators to include, exact backtest methodology. Reasonable defaults
  are fine to propose, but flag them as decisions rather than silently
  committing to one approach.
- This is a research/analysis tool, not a trading tool. No order
  placement, no broker execution, no "place trade" UI affordances.

## Starting point

Confirm you've read `PROJECT_SPEC.md`, propose the folder structure and
build order, and we'll go from there.
