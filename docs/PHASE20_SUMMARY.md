# Phase 20 — Outlook v1

Written 2026-09-28 for Tom and for the next Claude Code session. The work
order is `docs/REVIEW_P8-16_AND_NEXT_PHASES.md` Part D and Part E, Phase 20.
Tom said "proceed to the next phase". I used the review's defaults; the
choices the review left open are listed in section 6 for you to confirm.

## 1. What was built

`analytics/outlook.py` gives three dials per symbol, for each horizon on the
grid 3, 5, 7, 10, 14, 21, 30, 45 and 60 calendar days:

| Dial | Built from | 5 means |
|---|---|---|
| **Direction** | P(up by more than ¼ EM) − P(down by more than ¼ EM) | balanced |
| **Range** | P(staying inside ±1 EM), scored against the stock's own base rate | its normal |
| **Volatility** | implied vol vs the realised vol the engine forecasts (a ratio of 1.5 scores 10, 1/1.5 scores 0) | fair |

Each dial's probability is the average of two models:

- **The engine.** The probability engine's H and T paths, read at each
  horizon. The spread of those paths is also the realised-vol forecast the
  Volatility dial uses: because H is conditioned on today's volatility, the
  forecast carries the mean reversion that history shows.
- **A pooled logistic model.** A ridge-regularised logistic regression,
  pooled across all symbols, on price-based features known at each close:
  - momentum over 1 week, 1 month, 3 months and 12-1 months, scaled by each
    stock's volatility
  - distance from the 50-day and 200-day averages, and the 50/200 cross
  - RSI and ADX
  - RV20, its 1-year rank, and RV20/RV60
  - SPY's 1-month momentum and RV20
  - the stock's own base rate

  It is fitted with a small Newton solver, since scikit-learn isn't
  installed. VIX history only starts in 2021, so SPY's own momentum and RV
  stand in for market context.

**"EM" means the move implied by 20-day realised vol, not IV.** That is the
only move unit with 20+ years of history, so every Direction and Range
probability can be tested walk-forward. The IV-based expected move is still
shown beside the dials as P(inside the IV move).

### Skill (walk-forward)

`validate()` refits the model every January from 2015, using only outcomes
known by then, and predicts that year weekly for 67 symbols. Skill is the
Brier skill score against each stock's own base rate at that point in time.
The run takes 45 seconds, and the pipeline repeats it when the model is more
than 7 days old.

| Event (pooled) | Skill, 3 → 60 days |
|---|---|
| up | +0.003 to +0.004 |
| down | +0.001 to +0.009 |
| inside ±1 EM | +0.034 rising to +0.071 |

**Direction has about zero skill; Range has real skill.** Range's skill is
mostly volatility clustering and its mean reversion. The historical-analogue
component on its own does slightly worse than the base rate for Direction;
the logistic model carries what little there is. Volatility is not tested
walk-forward, because that needs IV history; the chain archive is building
it.

### How skill turns into what you see

- **Skill per symbol is noisy** (70–600 independent outcomes each), so it is
  pulled toward the pooled figure with a prior worth 300 outcomes.
- **Dial length:** a dial is shrunk toward 5 by that skill. It sits at 5
  when skill is at or below 0.005, and reaches full length at 0.03.
- **Band:** the uncertainty band widens as the shrink grows, and as the
  engine and the logistic model disagree.
- **Confidence** (none / low / medium / high) combines the effective sample,
  the skill, and how well the two models agree.

**Live result:** 72% of Direction cells have no measurable skill and sit
exactly at 5; the other 28% are within 0.2 of it. Range spreads from 2.6 to
7.8.

## 2. Where it shows

- **Universe page:** a heatmap of symbols × horizons.
  - Pick the dial to show.
  - Colours diverge around 5: red below, gray at 5, blue above.
  - Each cell shows the score and confidence dots (○○○ to ●●●).
  - A "model reading" toggle shows the dials before the skill shrink.
  - Three gauges for the selected symbol, at a horizon you choose.
- **Gauges** (Universe page, and Trade Detail → Summary at the trade's own
  DTE), each showing:
  - the score, an arrow at it, and the uncertainty band
  - a dotted tick at the stock's normal position
  - the confidence level
  - the line saying what moves Direction, e.g. "+17.5% vs 200D (+),
    1-month +7.6% (+)"
  - the implied-vs-historical downside sentence, e.g. "The market prices
    more downside than this stock at this volatility and technical setup has
    historically produced: P(down more than 4.5% in 30d) 14% implied vs 3%"
- **Validation page:** the pooled skill table, a plain statement of where
  skill is about zero, and each symbol's own skill next to the skill actually
  used.
- **Screener:** an "Outlook filters" expander.
  - Read the dials at each row's own expiry (interpolated between grid
    horizons) or at one fixed horizon.
  - Set Direction, Range and Volatility bounds, a minimum confidence, and a
    from–to DTE window.
  - Each row gets Direction, Range and Vol-dial columns.
- **Recommender:** the trend condition now comes from the Direction dial at
  the spec's nearest expiry (≥ 6 uptrend, ≤ 4 downtrend, otherwise range).
  - A ticker with no Outlook row falls back to the technical trend state.
  - `outlook.recommender_source: trend_state` in config.yaml restores the
    old behaviour.
  - The Strategies page shows the Direction score behind each condition.
- **Pipeline:** a new `outlook` data stage runs in full runs and in the
  nightly job, and refits weekly. `scripts/validate_outlook.py` does the same
  by hand.

**Display and filter only.** Nothing ranks or gates trades on the Outlook
(review D.5 step 3 waits for tracked outcomes).

## 3. What the first run shows

- **Direction is honest and therefore dull.** Every symbol sits near 5, and
  the review's first example filter ("Direction ≥ 6 @ 14d, confidence ≥
  medium") returns **nothing**. That is the correct answer on today's
  evidence, not a bug.
- **High Range today means recently spiked vol.** PANW (RV20 70%), PCG and
  CDNS score 6.9–7.7: they are expected to calm down and stay inside ±1 of
  their own recent move. Low Range means unusually calm names that tend to
  wake up (PDD, HRL, ET). Before selling a condor on a high Range score,
  check P(inside the IV move) beside it, since that move is usually smaller.
- **Volatility reads IV rich almost everywhere** (mean 7). That is the
  variance risk premium: SPY's 30-day IV of 16% against a forecast realised
  vol of 10% scores 10.

## 4. Checks

- `pytest tests`: **517 passed**, including 11 new tests in `tests/test_phase20.py`:
  - point-in-time features
  - labels and base rates
  - the logistic fit and Brier skill
  - the dial mappings, shrink and confidence levels
  - the walk-forward on synthetic data (no direction skill on a random
    walk, positive range skill under clustered vol)
  - the live rows, interpolation, the Screener filters, and the
    recommender's trend source

  Trade Detail now renders 10 charts (7 plus the 3 gauges).
- `scripts/check_pages.py`: 13/13 clean.
- The heatmap and gauges were rendered to images and checked by eye.

## 5. Follow-ups

- **Volatility skill** needs IV history. After a few months of the chain
  archive, test IV against later realised vol per symbol and add the
  Volatility dial to the skill table.
- **Option features** (put/call ratios, skew, term slope) and days to
  earnings join the logistic model once the archive has history.
- **Phase 21** tests the Outlook as a ranking component (rank IC on tracked
  outcomes) before it is allowed to rank or gate anything.

## 6. Decisions to confirm

1. **Realised-vol EM** for Direction and Range (testable today), with the
   IV move shown beside them. The alternative is the IV move as the unit,
   which can't be tested until the archive has history.
2. **Recommender trend from the Outlook, as the work order says.** Because
   Direction has no skill, the condition is "range" almost everywhere: PMCC,
   which needs an uptrend, will rarely apply, and bull puts and bear calls
   apply through their "range" allowance. Keep it, or set
   `recommender_source: trend_state` until Direction shows skill?
3. **Shrink thresholds:** neutral at skill ≤ 0.005, full length at 0.03,
   and a pooled prior worth 300 outcomes.
4. **The Volatility scale saturates at an IV/forecast ratio of 1.5**, so
   many names read 10. Should the scale be wider?
