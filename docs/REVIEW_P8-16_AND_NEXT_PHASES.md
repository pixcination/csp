# Review of Phases 8–16, and the plan for Phases 17–21

Audience: Tom and Claude Code. Written 2026-09-28.

Sources: all nine phase summaries, `ARCHITECTURE.md`, `config.yaml`, the git
log, and read-only inspection of `data/trade_log.duckdb`,
`data/universe.duckdb`, `data/runs/` and `data/chains/`.

Nothing was executed on Tom's machine. Every numbered claim below that is not
quoted from a summary was checked against the files named with it.

---

## Part A — Review

### A.1 Verdict

The roadmap was built in full, and the quality of the work is high. Summaries
state what didn't work alongside what did: the placebo redesign in Phase 10,
the corrected first calibration draft in Phase 14, and the false calendar
edge fixed in Phase 16. Real bugs were found and fixed along the way, such as
the Windows run-lock killing live runs and partial earnings pulls wiping the
file.

| Check | Result |
|---|---|
| Git | 14 commits, `main` == `origin/main` (`3925953`) at `github.com/pixcination/csp` |
| Tests | 184 → 451 passing (per the summaries) |
| Pages | 12, all passing the headless check |
| Golden test | Pins the CSP numbers across the refactor |

**The main gap is that the tool has never met a live market or a real
trade.** The rest of this review mostly follows from that.

### A.2 Concerns, most important first

**1. Every run so far used closed-session data.** All 17 Phase 8–16 runs read
the `2026-09-25_closed` (or `2026-08-21_closed`) chain block. `data/chains/`
has no RTH block at all.
- The liquidity gates behave differently off-hours: volume is 0 and far-OTM
  open interest looks thin.
- The shipped default spread (4% wide, 45 DTE) produced zero accepted trades
  on stored chains in both Phase 15 and Phase 16. Every one was rejected on
  the open-interest and volume floors.
- Until an RTH run proves otherwise, the default PCS request may return an
  empty sheet.

**2. No account profiles exist.** There is no `config/user_settings.yaml`, so
every run sizes against the default `research` profile at $3,000,000.
- That is why the proposals read "QQQ calendar ×49 contracts" and "BMY ×8,
  $48,800 BPR".
- Contract counts, liquidity caps, permissions and the capital gate are all
  meaningless for the Roth IRA, IRA and taxable accounts until those
  profiles are entered.

**3. The paper book is empty.** All seven tables in `trade_log.duckdb` have 0
rows. The calibration, P(reach X%) scoring, slippage measurement and rank
checks are fully built but have no real inputs. Your tracking idea (Part C)
is exactly the fix.

**4. Every validation rests on synthetic option prices** (IV = 20-day RV ×
1.15). The key defaults were all tuned on that model:
- the blend weights
- the ranking-weight calibration
- the stop-loss cost (58.5% vs 11.6% a year)
- the 4%/45-DTE spread shape
- the calendar and PMCC assumptions

The summaries flag this honestly, but it makes those numbers comparisons
between rule sets, not forecasts. Two remedies, which work together:
- Start archiving your own intraday chain history now (Phase 18). By the
  numbers below it is cheap.
- Price out historical options data (Massive's options tiers, ORATS or CBOE
  DataShop) for the decisions that can't wait months.

**5. The IV-rank measure is less clean than it looks** (checked in
`market_metrics`, 2026-09-27 snapshot):
- The headline `tos` rank is **not bounded to 0–1**: EWZ reads 1.11 and BIDU
  −0.007.
- The two TastyTrade ranks disagree enough to reclassify the universe.

| Measure | Low (< 0.25) | Mid | High (> 0.50) |
|---|---|---|---|
| IVR tos (in use) | 12 | 34 | 21 |
| IVR tw | 32 | 29 | 6 |
| IV percentile | 27 | 20 | 20 |

This matters for decision 1 (Part B).

**6. The management rules and the ranking disagree.** You chose a 2× loss
stop and a 21-DTE time stop for spreads. The engine's headline EV, which
ranks the sheet, assumes a profit target with no stops. So the sheet is
ranked on a policy you don't run (decision 3).

**7. The 1-minute data is stale.** The archive and `raw_1m_cache.duckdb` end
on 2026-06-30, so `gaps.py` and intraday RV are three months old.
- Overnight gap risk only needs the prior close and today's open, which the
  daily yfinance bars already carry.
- Either restore the Massive sync or re-base `gaps.py` on daily bars and keep
  the 1-minute data optional.

**8. There are more pages than the workflow needs.** Command Center,
Decisions, Screener, Strategies, Wheel, Portfolio, Signals, Validation,
Universe, Settings and Trade Detail overlap: three places show proposals and
two show open positions. The tracking work is a good moment to regroup them
(proposal in C.6).

**9. Some findings should shape the next ideas.**
- Phase 10: moving averages are mostly respected no more often than chance.
  32 "strong" levels were found where about 26 are expected by luck.
- Phase 14: across names, trend and support scores had roughly zero power to
  predict short-put outcomes (IC +0.007 and −0.008). Volatility rank was
  strongly predictive (IC +0.14, t 8.8).
- Direction from technicals is weak here; volatility is where the signal is.
  Part D builds on this.

**10. Many defaults were taken on "proceed".** That was reasonable for speed,
but a list of unreviewed defaults has built up. Part B.2 gives a recommended
answer for each open one.

---

## Part B — Decisions

### B.1 The four decisions Claude Code raised

**1. IV-regime thresholds (low < 0.25, high > 0.50).** Keep the numbers, but
change the measure, and soften the gate.
- **Switch the regime input from `ivr` (tos) to IV percentile.** It is
  bounded, it is not distorted by one spike in the lookback, and at 0.25 /
  0.50 it splits today's universe 27 / 20 / 20.
- **Clamp every rank to [0, 1].**
- **Add hysteresis** (e.g. ±0.03) so a name at 0.24–0.26 doesn't flip
  strategy families from run to run.
- **Use the regime as a soft input, not a hard exclusion,** except at the
  extremes (IVP < 0.10 for credit strategies, > 0.90 for debit calendars).
  Today IWM at IVR 0.23 loses every credit strategy over two points, and the
  cliff is arbitrary.
- **Revisit with data** once tracking has recorded about 3 months of
  outcomes by regime.

**2. Margin profile for strangles.** Mirror reality: define one only if your
taxable account actually has naked-option approval at the broker.
- If you do, mark it research-only, so strangles appear for comparison but
  are never auto-tracked (Phase 19). That fits your capital-preservation
  focus.
- Either way, **enter the Roth IRA, IRA and taxable profiles now** (A.2.2).
  That matters more than the strangle.

**3. Loss stops in the probability engine.** Yes, and make the headline
policy **the rules you actually run**. That is the 2× stop and the 21-DTE
time stop for spreads, and each spec's exit block for spec positions.

Do it together with two model fixes, or the stop policies will look
artificially expensive, as in the Phase 15 backtest:
- **IV dynamics:** IV rises when spot falls (spot–vol beta, e.g. from the
  VIX/SPX relationship, scaled per name). Sticky-strike IV understates the
  mark at the moment a stop triggers.
- **Stops trigger on marks, not closes:** approximate intraday extremes from
  daily high and low. Stops on daily closes understate how often they fire.

Show every policy side by side on Trade Detail, so the cost of the
protection is visible per trade.

**4. PMCC chain coverage.** Yes, widen automatically, but keep it targeted:
- Only the back-leg role's expiration band (≈ 75–120 DTE).
- Only calls in the delta band the spec needs (≈ 0.75–0.90).
- Only for names whose PMCC entry conditions hold (uptrend).

Record the extra subscriptions in the manifest so the dxFeed budget stays
visible.

### B.2 Earlier open decisions (recommended answers)

| Phase | Question | Recommendation |
|---|---|---|
| 14 | Default ranking preset | **`calibrated`**. It is the only preset with measured support; with top-N = all it mainly affects ordering. |
| 14 | Trend/support weights in other presets | Leave them in user presets; 0 in `calibrated`. |
| 14 | Grid default; landing page | Keep: passing rows only; Screener as the landing page. |
| 13 | Blend weights | Keep **equal thirds** for now. G acts as a conservative shrink toward zero edge. Re-fit on tracked outcomes (Phase 21). |
| 13 | Headline policy above 14 DTE | Your shipped rules (see B.1 decision 3). |
| 13 | Earnings IV crush 30% | Keep until measured. Phase 18's archive gives real pre- and post-earnings IV per name. |
| 12 | Index widths | Use `spread_width_pct` for indices too; check the SPX/XSP strike grid snaps sensibly. |
| 12 | Credit/width < 1/3 | Keep as a warning. |
| 12 | $5 fee on cash-settled spreads | Check one tastytrade statement for a cash-settled index expiry and set it from that. |
| 10 | "Strong" level definition; bootstrap placebo | Accept both; they are the more rigorous choice. |
| 9 | Stage 1 drawdown lookback 10 years; RV floor for index ETFs | Accept 10 years; lower the RV floor for broad index ETFs to ~0.08 so SPY and DIA stop failing on calm markets. |
| 9 | BLS 2027 dates | A calendar reminder for when BLS publishes them. |

---

## Part C — Input on trade-log tracking

### C.1 Most of the plumbing already exists

Phase 15 built:
- **`paper_positions` / `paper_legs`:** any strategy's legs are recorded.
- **`paper_predictions`:** every blended probability, stored at entry.
- **`paper_marks`:** time-stamped marks, and the best profit seen.
- **Calibration:** POP and P(reach X%) scored as hit, miss or censored.
- **Pipeline review:** every run marks every open position and gives a
  hold / close / roll verdict.

What's missing is the workflow around them: bulk logging, an on-demand
"update now", an entry-vs-now comparison, a scheduler, and a clean separation
between trades you *took* and recommendations you're only *tracking*.

### C.2 Keep "tracked" and "taken" apart

Add a `book` column to positions: `tracked` (a forward test at the modelled
fill at log time) vs `taken` (a real trade with your actual fill). They share
tables, marks, predictions and management logic, but:
- **Capacity, portfolio exposure and correlation limits count `taken` only.**
  Otherwise tracking 40 recommendations would block real entries.
- **Accuracy reports can show either book, or both.** A taken trade usually
  started as a tracked one; `promote` links them.

### C.3 Log the right sample, not just the favourites

If only the trades you like are logged, the accuracy log measures your taste,
not the model. To learn from it you need outcomes across the rank spectrum.
Each auto-log (and optionally each manual "log all") records:
- **Top K** passing rows per preset, K configurable (default 5).
- **A control sample:** a random M passing rows further down the ranking,
  and M near-miss rejected rows (failed exactly one gate), flagged
  `sample = control`. Default M = 3. These are what show whether the ranking
  and the gates add value.

Deduplicate on `(strategy, ticker, expiry, strikes)`. Re-seeing a logged trade
next hour appends an *observation* (price, rank, probabilities); it does not
create a new position.

### C.4 The update, and the entry-vs-now comparison

**"Update open positions".** Pull chains only for tickers with open positions
and record a mark per position. It exists for the pipeline; this makes it a
button plus a scheduled job. Each mark stores:
- time and session block
- spot and each leg's bid/ask/mid/IV/delta
- position mid and natural, P&L in $ and as % of max profit
- DTE left, best profit and worst drawdown so far
- **forward-looking probabilities recomputed from now:** P(reach the target
  from here), P(max loss or assignment from here), POP now
- the current management verdict (hold / take profit / roll / close) and
  its reason

**Entry vs now.** For every position, a card or row comparing:
- spot (in EM units), IV per leg, IVR/IVP
- trend state and RSI, days to events, the VIX regime
- the probabilities at entry vs now

Add **P&L attribution** between marks: delta, gamma, theta, vega and a
residual. That tells you *why* a trade is up or down (time decay working, or
an IV crush masking a bad move), which is exactly what the keep / manage /
close call depends on.

**Outcomes.** Record two outcomes per position, both of which the accuracy
log needs:
- **hold-to-expiry**, settled on the closing price
- **managed**, i.e. what the shipped rules would have done on the recorded
  marks

### C.5 Scheduler

Streamlit is the wrong host for timed jobs:
- Scripts re-run on every interaction.
- Each browser tab is a separate session.
- Nothing runs when no tab is open.

Instead:
- `launch.py` starts a small **worker process** (APScheduler, plus the
  existing run lock and market calendar) alongside Streamlit.
- A Windows Task Scheduler entry at logon can start the worker even when the
  UI isn't open.
- The schedule is edited on the Settings page and stored in
  `config/user_settings.yaml`.

Default schedule:

| Job | When | Does |
|---|---|---|
| `mark` | Trading days 9:45–15:45 ET, hourly (your 7 slots) | Update open positions (C.4) |
| `scan_and_log` | Same slots, per saved preset marked "auto" | Run the preset; auto-log per C.3 |
| `archive` | 15:45 | Full-universe chain snapshot, kept as your own option history |
| `nightly` | 18:30 | `pipeline/run.py --data-only` |

Rules:
- **Half days:** stop after the close (the calendar already knows 13:00
  closes).
- **Holidays and DST:** handled by the ET calendar.
- **Missed slots** (asleep or powered off): run once on wake if within 30
  minutes, else skip and log the miss.
- **Overruns:** skip the next slot rather than queue it.
- **Visibility:** the UI shows a heartbeat, last and next run, and a job
  history with failures.
- **Why start at 9:45:** skipping the first 15 minutes avoids opening-auction
  spreads, as you intended.

### C.6 Data budget: capture enough, not everything

Measured: the 25-Sep strike-filtered snapshot of all 63 names is about 3 MB
(chain + underlying parquet).

| What | Frequency | Approx. size | Keep |
|---|---|---|---|
| Recommendation rows (`candidates.parquet` + manifest) | per run | ~50–150 KB | forever |
| Marks (per position) | hourly | ~1 KB | forever |
| Chains for tickers with open positions | hourly | small | 90 days, then thin to one per day |
| Full-universe chain snapshot | daily 15:45 | ~3 MB (~0.75 GB/yr) | **forever** |
| Full-universe snapshot, if hourly | hourly | ~20 MB/day (~5 GB/yr) | only if you want intraday history; thin after 90 days |
| `prob_curves` / `prob_metrics` per run | per run | largest run artefacts | 60 days, unless a logged position references the run |

The daily archive is the most valuable thing this phase produces. After a few
months it is real option-price history for IV rank, earnings crush, skew
dynamics and a non-synthetic check of the probability engine. That starts
replacing the IV = RV × 1.15 assumption (A.2.4).

Also add run pruning: keep manifests forever, and keep any run a logged
position references. That fixes the "old runs break Trade Detail links" limit.

### C.7 The accuracy log (what you learn from)

A **Tracking → Accuracy** view, filterable by strategy, DTE bucket, regime,
preset and book:
- **Probability calibration:** predicted vs observed POP and P(reach
  25/50%), by bin, with Brier scores for G, H, T and the blend.
- **Ranking value:** do higher-ranked trades earn more per day on BPR than
  the control sample? (Rank IC on real outcomes, the test Phase 14 couldn't
  run.)
- **Gate value:** did near-miss rejected trades do worse than passing ones?
- **Management value:** managed vs hold-to-expiry outcome, per rule.
- **Fill realism** (taken book): measured vs assumed slippage (already
  built).
- **Minimum-sample warnings everywhere.** Expect about 3 months before most
  cells mean anything.

Phase 21 uses these to re-fit the blend weights, the ranking preset and the
regime thresholds, from real data instead of synthetic.

---

## Part D — Input on fund sentiment

### D.1 Worth building, with one reframing

The idea is right: decide the outlook first, then pick the strategy that
expresses it. That is also the cleaner design for the recommender, whose
condition matrix (IV regime × trend × events) is already a crude outlook.
Two adjustments:

**1. Call it "Outlook", and give it three dials, not one.** Strategy choice
depends on three separate questions, and they are not equally predictable:

| Dial | Question | Predictability (your own Phase 10/14 evidence + literature) | Strategies it drives |
|---|---|---|---|
| **Direction** 0–10 | Up or down over the horizon? | **Weak.** Trend and MA support showed ~0 IC here. | PCS / bear call / PMCC vs neutral |
| **Range** 0–10 | Will it stay inside ±1 EM? | Moderate | Condors, calendars, strangles |
| **Volatility** 0–10 | Is IV rich or cheap vs the vol likely to be realised? | **Strong** (vol-rank IC +0.14, t 8.8) | Credit vs debit structures |

Your examples map onto these directly:
- "Bullish > 6 expiring in 14 days, for PCS" = Direction ≥ 6 + Volatility
  rich.
- "Neutral from now to 45 days, for calendars" = Range high + Volatility
  cheap in the back month.

A single bull/bear number can't express the calendar case.

**2. Every score is a calibrated probability underneath, and its confidence
is measured.** Direction 7 should mean "P(up over the horizon) ≈ 0.70 on
this model's track record", not "several indicators agree".

Confidence has three parts:
- (a) effective sample size
- (b) walk-forward **skill** for that symbol and horizon (Brier skill score
  vs the stock's own base rate)
- (c) agreement between models

Where skill is about zero, the UI should say so, with the arrow at neutral
and a wide band. Expect that for Direction on many names. It is the honest
answer, and it stops the dial from inventing conviction.

### D.2 How to compute it (reusing what exists)

The probability engine already simulates each symbol's future paths three
ways, and the Outlook is a read of the same paths at horizon h:
- G is market-implied.
- H is historical, volatility-conditioned.
- T is conditioned on the current technical state.

From them:
- **Direction** = P(S_h > S_0 · (1 + ¼·EM)) − P(S_h < S_0 · (1 − ¼·EM)),
  mapped to 0–10 (5 = balanced). The small dead zone stops noise from
  counting as direction.
- **Range** = P(|S_h/S_0 − 1| < 1·EM), mapped to 0–10 against that stock's
  own base rate.
- **Volatility** = the IV at h vs the forecast realised vol (the H/T path
  vol, plus the vol-rank mean reversion Phase 14 measured). 5 = fair.
- **G vs T divergence** is informative on its own: "the market prices more
  downside than this setup has historically produced" is a useful sentence
  on Trade Detail.

Then add features the engine doesn't use yet, point-in-time only:
- momentum 1w/1m/3m/12-1m and distance from MAs
- RSI, ADX, trend state
- RV and IV rank/percentile, IV term slope, skew
- put/call volume and OI ratios, once the chain archive exists
- days to earnings
- the SPY outlook and VIX regime as market context

Start with **regularised logistic regression pooled across symbols**, with
per-symbol base rates, validated walk-forward. Keep it simple; the risk here
is overfitting, not under-fitting. Option-derived features can only be
tested once Phase 18's archive has months of history, so v1 is price-based
and says so.

### D.3 Horizons and filters

- **A fixed horizon grid:** 3, 5, 7, 10, 14, 21, 30, 45, 60 calendar days
  (converted to trading days internally).
- **"From–to" windows:** the Outlook for each listed expiration inside the
  window, which is what an option actually settles on.
- **Screener filters:** e.g. `direction ≥ 6 @ 14d, confidence ≥ medium` or
  `range ≥ 7 @ 21–45d`. They act as a pre-filter or ranking input.
- **The recommender replaces trend state with the Outlook dials** in its
  condition matrix.

### D.4 Display

- **Universe heatmap:** symbols × horizons, diverging colour for Direction;
  toggle to Range or Volatility. Cell text shows the score; opacity or a dot
  shows confidence.
- **Gauge on Trade Detail and the Universe page:** your horizontal
  bearish–neutral–bullish line with the arrow, plus a shaded band for the
  uncertainty and a tick at the stock's normal (base-rate) position. Show
  the number too. Three small gauges, one per dial.
- **A one-line explanation of what moved the score**, e.g. "IV percentile
  0.82 (+), RSI 28 (+), 50D below 200D (−)".
- These visuals would also teach well in your options-education material,
  because they show why a strategy fits a condition.

### D.5 Rollout

1. **Display only,** with skill statistics published on the Validation page.
2. **Drive the recommender's strategy-family choice.**
3. **Rank or gate trades,** only after the tracked outcomes (Part C) show the
   Outlook adds rank IC beyond the existing components. The same test that
   retired trend and support from the `calibrated` preset.

---

## Part E — Work orders for Claude Code

The ground rules from `SCREENER_ROADMAP.md` Part C still apply: plan and
decisions first, config-driven, real data, `tests/test_phase{N}.py`,
headless page checks, `docs/PHASE{N}_SUMMARY.md`, ARCHITECTURE.md updated,
and commit + push.

### Phase 17 — Live shakedown and open decisions (short)

1. **A live RTH run on a trading day,** with the default CSP request, the
   default PCS request (4% wide / 45 DTE) and `examples/recommend_strategies.json`.
   Report accepted counts by gate vs the closed-session runs. If the PCS
   default is still empty:
   - snap the long leg to the nearest strike with OI above the floor, within
     ±25% of the target width
   - prefer monthly expirations for spreads when the weekly legs fail
     liquidity
   - make the per-leg OI/volume floors scale with the proposed contract
     count, keeping an absolute minimum
2. **Account profiles:** help Tom enter Roth IRA, traditional IRA and
   taxable (capital, caps, permissions, account type). Make the Screener
   require an explicit profile choice (no silent $3M default). Re-run the
   golden test only if the default profile is changed on purpose.
3. **Decisions B.1:**
   - IV percentile regime with clamping, hysteresis and a soft gate
   - margin profile only as Tom specifies
   - targeted PCS/PMCC chain widening
4. **Loss-stop policies in the engine** (B.1 decision 3), including
   spot–vol IV dynamics and a high/low-based trigger. The headline policy
   follows the shipped management rules. Report how the default rankings
   change.
5. **Gaps on daily bars** (A.2.7), with the 1-minute path optional; or
   restore the Massive sync. Tom picks.
6. **The B.2 table** as Tom confirms it.

Acceptance: an RTH run manifest showing non-empty default CSP and PCS
sheets, or a documented reason; profiles in `user_settings.yaml`; stop
policies in Trade Detail's policy table.

### Phase 18 — Trade tracking (manual)

1. Schema: `book` (`tracked` | `taken`), `sample` (`top` | `control` |
   `manual`), an observations table, a `promote` link; run-folder pruning
   rules (C.6).
2. Screener: multi-row selection (`selection_mode="multi-row"`), "Select
   all passing", and **Log selected** / **Log all** (C.3 sampling).
3. A new **Tracking** page (absorbing the book parts of Decisions):
   - open positions with the entry-vs-now comparison
   - P&L attribution
   - forward probabilities from now
   - verdicts
   - an **Update now** button (targeted chain pull + marks)
   - closed positions with hold-to-expiry and managed outcomes
4. The daily chain archive job (15:45 snapshot, run by hand in this phase).
5. Auto-expiry for tracked positions, settled from the closing price.

Acceptance: log a mixed CSP/PCS/spec selection from a live run, update twice
during RTH, see attribution and changed probabilities, expire one position
and see both outcomes recorded.

### Phase 19 — Scheduler and auto-logging

1. A worker process (APScheduler) started by `launch.py`; an optional Windows
   Task Scheduler entry at logon; a heartbeat file; the run lock shared with
   the UI.
2. Schedule editor on Settings (C.5 defaults), stored in
   `user_settings.yaml`; per-preset "auto" flag with K and M.
3. Missed-slot, overrun and half-day rules; a job history panel; failure
   alerts in the UI.
4. Nightly data-only job; the daily archive moved onto the schedule.

Acceptance: a full trading day runs unattended (7 mark slots, auto-logs
without duplicates, the archive at 15:45), with the history showing every
slot.

### Phase 20 — Outlook v1

1. `analytics/outlook.py`: the three dials from the engine's paths, plus the
   pooled logistic model on price-based features; the horizon grid; the
   confidence components (D.1).
2. Walk-forward skill per symbol and horizon (Brier skill vs base rate);
   results on the Validation page.
3. UI: universe heatmap; gauges on Trade Detail and Universe; Screener
   filters (`direction`, `range`, `volatility`, `confidence`, horizon or
   from–to).
4. The recommender uses the Outlook dials in place of trend state.
   **Display and filter only; not a ranking weight.**

Acceptance: skill tables published; for Direction, an honest statement of
where skill is about 0; the heatmap and gauges render; the example filters
in D.3 return sensible sets.

### Phase 21 — Learning loop (after ~3 months of tracking)

1. Re-fit the blend weights, ranking preset, regime thresholds and earnings
   crush on tracked outcomes and archived chains; compare with the synthetic
   fits.
2. Test the Outlook as a ranking component (rank IC on real outcomes).
3. Re-run the probability-engine validation on archived real option prices
   instead of RV × 1.15.
4. Report what changed and what the data couldn't yet decide.

**Requirement (added 2026-09-28): count entry days, not just positions.**
Positions logged on the same day share market exposure (one sell-off hits
all of them), so they are not independent observations. Every accuracy
statistic in Phase 21 (hit rates, calibration, rank IC, P&L by bucket,
fit residuals) must:

- group by entry date (per-day aggregates, or errors clustered / block-
  bootstrapped by entry date), and
- report the number of entry days next to the number of positions
  (e.g. "n = 214 positions over 58 entry days").

The entry-day count is the effective sample size for anything driven by
market direction; confidence intervals and "the data couldn't decide"
calls are made on it, not on the position count.

**Requirement (added 2026-09-29): leave Symbol Lookup trades out.**
Positions with `sample = lookup` (logged from the Symbol Lookup page, or
from Trade Detail on a lookup run) are single names picked by hand, not
draws from a preset's ranked sheet. They are excluded from every accuracy
statistic (`paper.NOT_FOR_ACCURACY`, `paper.for_accuracy`); their dollar
P&L and fills still count. Report their number separately.
