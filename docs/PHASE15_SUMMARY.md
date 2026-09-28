# Phase 15 summary — Multi-leg paper book, spread management and validation

Date: 2026-09-28. Roadmap: [SCREENER_ROADMAP.md §C.7](SCREENER_ROADMAP.md).
Architecture: [ARCHITECTURE.md](ARCHITECTURE.md) §3, §5–7, §10.

## Headline

Put credit spreads can now be recorded, managed and scored like CSPs.

- **Record.** Accept a spread from Trade Detail or Decisions. You enter the
  net credit, or the two leg fills.
- **Manage.** Each pipeline run marks every open position. A spread gets its
  own rules: profit target, loss stop, roll trigger with net-credit rolls,
  and optionally a time stop.
- **Score.** P(reach X% of max profit) predictions are stored at entry and
  scored against the marks.
- **Validate.** A new PCS rule backtest tests which management rules pay.
- **Portfolio.** The page now opens with the open book: beta-weighted
  delta, theta per day, buying power (BPR) in use, and an event calendar.

| Roadmap item (§C.7) | Result |
|---|---|
| 1. `positions` + `legs` schema, single-leg rows migrated, cycles and share lots kept | `paper_positions` is the position table, with new `paper_legs`, `paper_marks` and `paper_predictions` tables. Rows from before Phase 15 migrate to one leg each, safely on every run. |
| 2. PCS management: profit target, loss stop at k × credit, time stop, roll for credit, net of fees | `exit_rules.evaluate_put_spread` and `spread_roll_candidates`. The pipeline applies them to every open spread. |
| 3. Calibration extended to multi-leg and to P(hit X%) | POP reliability per strategy, and P(reach X%) scored as hit, miss or censored. Fills are calibrated against the spread's combined quote. Real-data spread validation of the engine: §3. |
| 4. PCS rule backtest with walk-forward over widths, deltas and targets | `analytics/pcs_backtest.py` and `scripts/backtest_pcs.py`: 768 rule sets × 4 ETFs × 20 years. Results in §4. |
| 5. Portfolio page: multi-leg exposure, beta-weighted delta, theta/day, BPR utilisation, event calendar | New **Open book** section, built on `analytics/book.py`. |

Also done: the wheel backtest item that Phase 8 deferred to this phase. It now
runs on the price basis, with each dividend credited while shares are held
(§5).

## Decisions

You said "proceed", so these are my defaults. Each one can be changed.

| Question | Decision |
|---|---|
| Table layout | I kept `paper_positions` as the position table and added `paper_legs`. The retired Trade Log's table is already called `positions` in the same database, and every page and the pipeline read `paper_positions`. |
| What a spread's row holds | `strike` is the short strike. New columns: `long_strike`, `width`, and `max_loss`. `collateral` is the buying power a spread ties up (its max loss). |
| How a spread closes | `expired_otm` or `closed_early` (the net debit you paid). A spread that finishes in the money is `settled`: you give the settlement price, and the debit and exercise/assignment fees follow from it. `rolled` records two linked positions. `assigned` stays CSP-only. |
| Wheel cycles for spreads | None. A spread's worst case is a defined loss, not shares. |
| Physically settled spread finishing between the strikes | **Your decision (after review):** recorded as `assigned`. It becomes a share lot at the short strike, with the basis lowered by the net credit, in a new wheel cycle, exactly like an assigned CSP. POP is scored on whether it settled above the breakeven. |
| Spread management defaults (`management.spread`) | **Your decision (after review): both stops on, as convention suggests.** **50% profit target** (only for trades entered above 14 DTE, and only if the gain clears the $5 net minimum). **2× loss stop.** **21-DTE time stop**, kept knowing its cost (§4). Roll on a breach of the short strike or delta −0.45, only for a net credit, at most once, not under 5 DTE; otherwise close. |
| Default spread shape | **Your decision (after review): 4% of spot wide at 45 DTE.** Two new request fields: `spread_width_pct: [4]` (replaces the dollar widths when set) and `pcs_dte_targets: [45]` (spreads only; CSPs keep their own window). Spreads are built at the listed expiration **nearest** 45, within ±14 days, because many names list only monthlies. Saved requests and the example files keep their dollar widths and shared window. The Screener has a width unit toggle and a "Spread DTE targets" field. |
| Scoring P(reach X%) | **Hit** if the best profit seen (marks and the exit) reached X. **Miss** only if the position was held to expiry. A position closed early without reaching X is **censored** and left out. Marks are sampled, so the observed rate is a lower bound. |
| POP outcome | A CSP loses on assignment. Anything else wins if it closed for less than its credit. Before, every early-closed CSP counted as a win, even one closed at a loss. |

## 1. What changed

**The book (`analytics/paper.py`)**
- `accept` records a CSP or a PCS: legs, entry fees for every leg, and
  buying power.
  - It stores the combined bid/ask quote of the legs, so fill slippage can
    be measured.
  - It stores the settlement type (cash for index roots).
  - It stores every blended prediction: POP, P(reach 25/30/50/100%),
    median days, P(max loss), P(touch), P(roll).
- `close_position` handles `settled` and records each leg's exit.
- `roll_position` records a roll. `record_mark` stores a mark and keeps
  the best profit seen.
- New helpers: `list_legs`, `list_marks`, `list_predictions` and `leg_text`.
- `performance()` adds a profit rate and a breakdown by strategy.

**Spread management (`analytics/exit_rules.py`)**
- `OpenSpread` and `evaluate_put_spread` apply the rules in this order:
  1. value floor
  2. loss stop
  3. short strike threatened (roll, or close when no roll is left)
  4. profit target, net of fees
  5. time stop
  6. an empirical test: is the mark above the spread's expected value at
     expiry?
- `spread_roll_candidates` lists rolls in the stored chain. Each has the
  same width, a later expiry and the same or lower strikes, and pays a net
  credit after both sides' fees.
- The Command Center shows these decisions, with the roll candidates.

**The open book (`analytics/book.py`)**
- Marks each position on the latest chain and gives the worst-case price
  (natural) and the P&L.
- Greeks come from the chain where the leg is quoted, else from
  Black-Scholes at the leg's entry IV.
- Adds a 1-year beta to SPY, delta in SPY-share equivalents, theta per day,
  vega, BPR utilisation, and an event calendar of which events fall inside
  which positions.

**Pipeline (`pipeline/run.py`)**
- `_evaluate_open_positions` now reviews spreads as well as CSPs.
- It records a `pipeline` mark for every open position on each run.
- The CSP roll ranker skips spreads.

**Calibration (`analytics/calibration.py`)**
- `outcomes`, `by_strategy`, `target_outcomes` and `target_calibration`.
- Fill calibration uses the combined quote, so a spread's slippage
  fraction is measured against its summed half-spreads.

**Validation**
- `scripts/validate_prob_engine.py --strategy pcs --width-pct` validates
  the probability engine on spreads and adds a `max_loss` target.
- The PCS backtest (§4).

**Pages**
- **Trade Detail:** Accept records spreads (net credit or leg fills). The
  management plan shows the spread rules.
- **Decisions:** accept forms on the spread cards. The book shows its
  legs, BPR and the best profit seen, with forms to settle, roll and record
  a mark.
- **Portfolio:** the Open book section.
- **Validation:** POP by strategy, P(reach X%) calibration, engine
  validation for spreads, and the PCS backtest.
- **Command Center:** spread decisions with roll candidates.

**Config**
- New `management.spread` section.

**Tests**
- `tests/test_phase15.py`: 34 tests (including the decisions made after
  review).
- Three earlier tests changed on purpose, because the behaviour they pinned
  changed in this phase:
  - Phase 12: spreads were refused before Phase 15.
  - Phase 13: `observe` now takes a `Position`.
  - Phase 8: the wheel modules must now use the price basis with dividends.

## 2. On real data

I recorded the Phase 14 runs' top rows into a **temporary** ledger (your
paper book was not touched) and ran the book and the pipeline review against
today's chains:

| | SPY 30 Oct $748/$738 ×2 | BMY 2 Oct $61p ×8 |
|---|---|---|
| Credit / mark / natural | $1.31 / $1.32 / $1.35 | $0.32 / $0.355 / $0.44 |
| BPR | $1,738 (= max loss) | $48,800 |
| Beta, beta-weighted delta | 1.00, +12 SPY shares | 0.27, +4 SPY shares |
| Theta/day, vega | +$2.94, −$24.8 | +$46.4, −$20.8 |
| Pipeline decision | hold: the $1.32 to close is $0.39 above the $0.93 expected value at expiry | hold: $0.24/share of edge, 11% assignment odds |
| Stored predictions | 15 (e.g. P(reach 50%) 93.6%, P(max loss) 6.7%) | |

The event calendar put the events inside the right trades:
- BMY's projected ex-dividend (1 Oct): the BMY put only.
- September payrolls (2 Oct): both positions.
- CPI (14 Oct), monthly OPEX (16 Oct) and FOMC (28 Oct): the SPY spread
  only.

Book totals: $50.5k BPR, 1.7% of net liq, +16 SPY shares beta-weighted, and
+$49/day theta.

## 3. Probability engine on spreads (real paths)

`validate_prob_engine.py --strategy pcs --tickers SPY,QQQ,IWM --dte 45
--delta -0.20 --width-pct 0.02` ran 594 entries (about 191 independent) in
36 s:

| Target | G | H | T | **Blend** | Observed |
|---|---|---|---|---|---|
| P(reach 25%) | 91.1% | 95.2% | 95.7% | **93.8%** | 94.9% |
| P(reach 50%) | 86.3% | 92.5% | 93.2% | **90.4%** | 91.8% |
| P(expire worthless) | 77.6% | 86.8% | 88.0% | **83.7%** | 85.9% |
| P(max loss) | 14.3% | 7.7% | 6.8% | **10.0%** | 9.4% |
| P(touch short) | 39.9% | 26.1% | 23.5% | **30.4%** | 31.5% |

(T is scored on 460 entries, where it had enough matching days.)

The blend lands within about 2 points on every target, as it did for puts
in Phase 13.
- G, the market-implied baseline, is pessimistic about profit and
  overstates max loss by 5 points.
- H is the closest single model.
- T understates touches by 10 points.

The same caveats apply as in Phase 13: prices are synthetic (IV = RV × 1.15)
and entries overlap. This validates the engine, not the paper book. The
book's own P(reach X%) scoring starts filling in as real trades close.

## 4. PCS rule backtest

`python scripts/backtest_pcs.py` ran SPY, QQQ, IWM and DIA over 20 years
in 376 s.

**Setup**
- **768 rule sets:** entry at 30 or 45 DTE × short delta 0.15–0.30 ×
  width 1/2/4% of spot × profit target 25/50/75%/hold × 2× loss stop on/off
  × close on a breach on/off × 21-DTE time stop on/off.
- **One spread at a time** on the real daily path.
- **Prices:** Black-Scholes at 20-day RV × 1.15, with a put skew of +5% of
  IV per standard deviation out of the money.
- **Costs:** tastytrade fees on every leg, and 40% of the modelled
  half-spread on every open and close.
- **Walk-forward:** choose the best set on 5 years, score it on the next
  year, and compare it with the shipped rules as a fixed baseline (45 DTE,
  0.20Δ, 2% wide, 50% target, 2× stop, no time stop).

**Walk-forward (60 folds)**
- In-sample: 125.4% a year on BPR. Out-of-sample: 72.3%. The fixed
  baseline scored 2.3% out-of-sample.
- The drop from in-sample to out-of-sample is 42% of the in-sample figure
  (the roadmap's "substantial" band).
- Re-choosing the rules beat the baseline in 78% of folds.
- Every set the walk-forward chose had no loss stop, no breach close and no
  time stop.

**How each rule changes the result**

This slice is 45 DTE, 0.20 delta and a 50% target, averaged over the four
ETFs. It is the fairest view; the full-grid averages are dragged down by the
1%-wide spreads, which fees and slippage ruin.

| Width | Loss stop | Breach close | Time stop | Annualised on BPR | Win rate | Max-loss trades | Worst trade |
|---|---|---|---|---|---|---|---|
| 4% | – | – | – | **58.5%** | 93% | 3.5% | −$1,544 |
| 4% | 2× | – | – | 20.7% | 84% | 0.1% | −$770 |
| 4% | – | – | 21 | 29.8% | 81% | 0% | −$1,199 |
| 4% | 2× | – | 21 | 11.6% | 79% | 0% | −$764 |
| 4% | – | yes | – | 6.6% | 79% | 0% | −$795 |
| 2% | – | – | – | 64.2% | 92% | 5.7% | −$796 |
| 2% | 2× | – | – (**shipped**) | −2.8% | 83% | 0.1% | −$521 |

**Reading.** Under this model, every defensive exit costs return:
- The loss stop, the breach close and the time stop each give up much of
  the edge.
- In exchange, max-loss trades fall from 3.5–5.7% to about zero. The worst
  single trade roughly halves.
- The worst drawdown barely changes.
- Wider spreads (4%) and 45 DTE beat narrower spreads and 30 DTE across the
  grid.
- A 25% target is the worst target. Fees and slippage on constant early
  closes eat it.

**Why this is not yet a reason to drop the stops**
- **The model builds in a short-vol edge.** IV is always 15% above trailing
  RV, even straight after a crash. When a model pays you to stay short
  volatility at every moment, any rule that exits early looks bad by
  construction. Real IV spikes above RV after a drop, and that is exactly
  when a stop pays.
- **Live, a breach rolls for a net credit before it closes.** The backtest
  only closes.
- **The return level is not a forecast.** A spread's credit is the
  difference of two synthetic prices, so the level is sensitive to the skew
  assumption. Read the comparisons between rule sets, not the percentages.

**What ships.** You chose both stops on, as convention suggests. The
shipped rules at 4% wide score 11.6% a year in this model, against 58.5%
with no stops. The difference is the price of the tail protection. The
backtest's fixed baseline is now the shipped rule set (45 DTE, 0.20Δ, 4%
wide, 50% target, 2× stop, 21-DTE time stop).

## 5. Wheel backtest: price basis plus dividends

`run_wheel` now takes `load_daily(basis="price", with_dividends=True)`.
Strikes sit on traded prices, and each dividend is credited while a cycle
holds shares: ex-dates after assignment, up to the day the shares are called
away. Buy-and-hold on the same bars adds the dividends back. The Wheel page,
regime scorecard, walk-forward and universe sweep all use it.

15 years, default rules (0.20 delta, 7 DTE, 0.25 delta calls):

| | Old (total basis) | Price, no dividends | **Price + dividends** | Dividends credited | Buy and hold |
|---|---|---|---|---|---|
| KO | 11.5% | 8.2% | **10.1%** | $1,393 | 9.9% |
| MO | 8.8% | 2.0% | **5.6%** | $2,777 | 13.2% |
| T | 6.2% | 0.7% | **5.0%** | $1,630 | 8.2% |
| PBR | 2.0% | −1.2% | **2.1%** | $833 | 7.9% |
| SPY | 10.8% | 9.5% | **10.2%** | $3,219 | 15.0% |
| AAPL | 16.3% | 13.0% | **13.5%** | $600 | 24.9% |

(Wheel columns are annualised return on capital deployed.)

The old basis flattered high-yield names (MO by 3 points, T by 1). On
adjusted prices the ex-dividend drops vanish from the path, so strikes sat
against prices that never traded. Buy-and-hold is unchanged to within
0.2 points, which is the check that the dividend handling is consistent.

## 6. Verification

- `python -m pytest tests -q`: **425 passed**. The 34 Phase 15
  tests cover:
  - **Schema:** the migration, run twice on a Phase 14 database.
  - **Recording spreads:** legs, BPR, the combined quote and predictions;
    leg fills and credit validation; cash settlement for index roots.
  - **Closing:** cash and physical settlement, with the debit, fees and
    realised P&L; a physical spread assigned between the strikes becomes a
    share lot in a new cycle, with its POP outcome; the per-strategy status
    rules.
  - **Default request:** 45-DTE targets and 4% widths, the
    nearest-expiration rule, and explicit requests keeping dollar widths.
  - **Rolls and marks:** CSP rolls stay in their cycle; a debit roll on a
    spread is flagged; the best profit seen is kept.
  - **Calibration:** hit, miss and censored target outcomes; POP outcomes
    for losing early closes; the fill fraction from the combined quote.
  - **Spread management:** every rule, and roll candidates (same width,
    later expiry, net credit only).
  - **The open book:** marks, natural, Greek signs (chain and model), beta
    and beta-weighted delta, summary totals, the event calendar.
  - **Pipeline:** the review of an open spread, with its recorded mark.
  - **PCS backtest:** no edge when IV equals RV and costs are off; the rules
    fire as specified; grid size; walk-forward.
  - **Validation script:** the spread observation.
  - **Management plan:** the spread rules on and off.
  - **Wheel:** `load_daily(with_dividends=True)`; dividends credited only
    while shares are held; buy-and-hold with dividends.
  - **Pages:** AppTest of Decisions and Portfolio with a book holding a
    spread and a CSP.
- `python scripts/check_pages.py`: **11/11 pages clean**.
- `python scripts/preflight.py`: no blocking issues. The known 1-minute
  archive warnings remain.

## 7. Decisions

**Decided by you after review (2026-09-28), now built:**
1. **Stops:** both on, the 2× loss stop and the 21-DTE time stop.
2. **Spread shape:** 4% of spot wide, at the expiration nearest 45 DTE.
3. **Spreads assigned between the strikes** become a share lot, like an
   assigned CSP.

**A first look at the new default on stored chains.** A spreads-only
request on SPY, QQQ and IWM, using the chains captured on 25 Sep:
- **Expiry.** The expiration nearest 45 DTE was Nov 6 (39 DTE). That
  capture ran only to about 45 DTE, so Nov 13 was not pulled.
- **Shape.** Widths came out at $30 on SPY and QQQ and $11 on IWM.
  Credit/width was 8–12%, POP 86–91%.
- **All six were rejected** by the open-interest and volume floors. The odd
  strikes 4% below spot on a weekly expiry have almost no open interest.
- A fresh run captures the full window (up to 59 DTE plus the roll buffer).
  If the 4%-wide long legs are still thin there, choose one:
  - prefer monthly expirations for spreads, or
  - snap widths to strikes with open interest.

**Still open from earlier phases:**
   - Phase 14: the default ranking preset, the grid default, and whether the
     Screener stays the landing page.
   - Phase 13: blend weights and earnings crush.
   - Phase 12: index width ladders.

## 8. Known limits

- **Backtest prices are synthetic.** Treat returns as comparisons between
  rule sets, not forecasts (§4).
- **Marks come from stored chains.** A position whose chain was not captured
  in the run has no mark: its P&L is blank, and its Greeks come from the
  model.
- **P(reach X%) from the book is a lower bound.** It also needs dozens of
  closed spreads before it says anything.
- **Roll candidates use the latest stored chain.** The pipeline widens the
  capture window for tickers with open positions, but a far-dated roll
  expiry may be outside it.
- **The book records CSPs and PCS only.** Other structures wait for the
  Phase 16 strategy DSL.
