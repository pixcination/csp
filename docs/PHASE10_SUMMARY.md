# Phase 10 summary — technical indicators and the level-respect study

Date: 2026-09-27. Roadmap: [SCREENER_ROADMAP.md §C.2](SCREENER_ROADMAP.md),
design in §B.3. Architecture: [ARCHITECTURE.md](ARCHITECTURE.md) §5.

## Headline

**Across this universe, moving averages are mostly not respected any more
often than chance.** The study compared 1,043 testable symbol × level pairs
with a null built from each stock's own reshuffled price history:
- The median edge is **+0.4 points**, and 53% of levels are positive.
- **32 levels** clear the "strong" bar, where roughly **26 would by chance
  alone**.
- The strong levels cluster in a handful of names (CDNS, ET, MO, KMI, EWZ,
  AIG). That looks more like those stocks mean-reverting in general than
  like respect for a particular average.

The tool can still say where the levels are, how often each held and how
deep the pierces went. What it shouldn't do is treat "the 200-day held 9
times" as evidence of support without the placebo comparison beside it. The
Levels tab and the support map show both.

## Decisions

| Question | Decision | Status |
|---|---|---|
| Test band / break tolerance | 0.5 × ATR / 1 × ATR (roadmap defaults); bounce 1.5 × ATR; re-arm after 3 closes above level + 1 ATR | defaults, in `config.yaml → levels` |
| Minimum n | 8 tests; below that "insufficient", no rate shown | default |
| Levels studied | SMA and EMA 21/50/100/200, daily and weekly (16 per symbol). 9 is computed as an indicator but not studied as support | Claude |
| Lookback | 20 years (MAs warmed on the full history before the window) | Claude |
| Horizons | 5 / 10 / 20 sessions; headline 10 | Claude |
| "Strong" | status ok and the **95% CI lower bound of the edge over placebo > 0** | Claude, **for Tom to confirm** |
| **Placebo** | **Block bootstrap of the stock's own daily moves** (20-day blocks, 20 replicates), with the same MAs, detector and outcomes. This **replaces the roadmap's randomly offset levels**, which were measured to be biased (§2) | Claude, **for Tom to review** |
| Weekly levels | the last *completed* week's value, tested against daily bars | roadmap |
| Expected-move units in the support map | spot × TastyTrade IV index × √(30/365). A placeholder until `expected_move.py` (Phase 12) | Claude |

## 1. What changed

- **`analytics/indicators.py`**: SMA/EMA 9/21/50/100/200, RSI-14 (Wilder),
  ATR-14, Bollinger(20,2), MACD(12,26,9), ADX-14 with ±DI, 52-week
  high/low/position, and volume ratio. It computes daily and weekly (on
  resampled bars); `with_weekly` attaches every weekly column as `w_*` via the
  last-completed-week alignment. It uses the price basis, is vectorised, and
  takes 0.9 s for 34 years of SPY.
- **`analytics/bars.py`**: `weekly_positions` computes the week-to-day mapping
  once (numpy `searchsorted`) and reuses it across the ~40 weekly columns.
  This also fixed a µs/ns datetime mismatch in `merge_asof`.
- **`analytics/trend_state.py`**:
  - uptrend: EMA21 > EMA50 > EMA200, with EMA50 rising and ADX ≥ 20
  - downtrend: the mirror image
  - range: everything else, reported per day so Phase 13 can condition on
    historical states
  - Today's split across the universe: 51 range, 9 downtrend, 7 uptrend.
- **`analytics/level_respect.py`**: implements §B.3:
  - test detection with debounce
  - held / bounced / broke outcomes, max pierce depth in ATR units, and
    censoring near the end of the data
  - Wilson CIs, and a Newcombe CI on the edge
  - a rising/falling slope split, a recency-weighted hold rate and the min-n
    rule
  - the placebo, plus `support_map()` and `describe()` (for example
    "100D EMA: 59 of 94 held (63%, CI 53-72%), +7 pts vs placebo")
- **`analytics/oscillator_study.py`**: forward 5/10/20-session returns after
  RSI < 30 or > 70, daily and weekly, against any day. Counted as
  **episodes** (first day in the zone), not days, so one selloff isn't
  counted five times.
- **`analytics/technical_study.py`** + the pipeline's **`technicals`** stage
  (a data stage, so it also runs with `--data-only`). Results are cached in
  `data/technicals.duckdb`: `indicator_latest`, `level_stats` and
  `oscillator_stats`. Symbols already studied through their latest bar are
  skipped. The full universe takes 186 s (2.4 s per symbol).
- **Signals → Levels tab** (the new first tab), showing:
  - a universe check comparing the strong count with the number expected by
    chance
  - trend state, daily and weekly RSI, ADX and ATR
  - the support map below spot, with distances in %, ATRs and expected moves
  - every level at a chosen horizon and slope regime, ranked by the edge's
    CI lower bound
  - a price chart with the level, the 200D SMA and 200W EMA, and each test
    marked held or broke
  - the RSI-extremes table
- **`app/components/charts.py → levels_chart`**:
  - one axis; level lines direct-labelled, with labels spaced apart where
    lines converge
  - held/broke markers in the reserved status colours, with distinct
    symbols and a legend
  - level hues skip categorical green and red so they can't be confused with
    the status markers
  - rendered and inspected for SPY and KO; the label collision and the
    green-on-green conflict were fixed after that review
- Preflight freshness now includes the technicals cache.

## 2. The placebo: what the roadmap specified, and why it was replaced

The roadmap specified the same statistics "at randomly offset levels in the
same periods". I built that first (levels shifted ±2–10%) and ran the check
the roadmap asked for: **the placebo edge should be ~0 on a random walk. It
was −6.2 points.** On a pure random walk, real MAs "held" less often than
their offsets. The cause is mechanical:
- Price only reaches a level 8% below its MA after a sharp fall.
- From there the lagging MA keeps falling toward price and carries the offset
  level away with it.
- So offset levels are systematically easier to "hold".

Across the real universe that null produced a median edge of −1.9 points,
which would have read as "moving averages are anti-support".

The replacement asks the question directly: **did the level hold more often
than the same level on price paths that had no reason to respect it?**
- Each replicate is a moving-block bootstrap (20-day blocks) of the stock's
  own daily close-to-close return, with that day's high and low relative to
  close.
- That keeps the drift (so the uptrend artifact the roadmap warned about is
  in the baseline too), the volatility and the short-run clustering.
- It destroys any memory of where the MAs sit.

Verification:

| Check | Result |
|---|---|
| Random walk, 8 independent seeds, 16 levels each | pooled edge **−0.2 pts** mean (SD 1.3); "strong" in 1 of 128 level tests (< the 2.5% chance rate) |
| Positive control: a series built to bounce off its 50-day SMA | 50D SMA: 238 of 241 held (99%), **+23 pts vs placebo**, edge CI lower bound **+0.20** → strong |
| The placebo's lean level arrays vs `indicators.compute` | identical (max abs diff 0.0) for SMA/EMA daily and weekly, and for ATR |

The limit that remains: blocks longer than a few weeks aren't reshuffled, so
a stock that mean-reverts over months will show *all* its MAs as mildly
"respected". The clustering of strong levels in ET, MO, KMI and CDNS is
consistent with that.

## 3. Level tables (acceptance)

10-session horizon, all slope regimes, the last 20 years to 2026-09-25,
ranked by the lower bound of the edge. The placebo is the block-bootstrap
hold rate. The pierce is in ATR units, median over tests that held. None of
the three has a level that clears "strong".

#### SPY — close 771.35, trend state **range**, as of 2026-09-25

| Level | Tests (n) | Held | Hold rate (95% CI) | Placebo | Edge (95% CI) | Median pierce (ATR) | Level now | Status |
|---|---|---|---|---|---|---|---|---|
| 100D EMA | 94 | 59 | 63% (53-72%) | 56% | +6.9 pts (-3 to +16) | 0.44 | 747.88 | ok |
| 21W EMA | 92 | 55 | 60% (50-69%) | 56% | +4.0 pts (-6 to +14) | 0.32 | 748.41 | ok |
| 21D SMA | 182 | 93 | 51% (44-58%) | 51% | -0.0 pts (-7 to +7) | 0.64 | 765.62 | ok |
| 21D EMA | 198 | 113 | 57% (50-64%) | 57% | -0.4 pts (-8 to +6) | 0.50 | 765.73 | ok |
| 50D SMA | 117 | 60 | 51% (42-60%) | 51% | -0.2 pts (-9 to +9) | 0.45 | 761.57 | ok |
| 50D EMA | 136 | 74 | 54% (46-63%) | 56% | -1.4 pts (-10 to +7) | 0.42 | 760.79 | ok |
| 100D SMA | 79 | 44 | 56% (45-66%) | 55% | +0.7 pts (-11 to +11) | 0.30 | 752.78 | ok |
| 21W SMA | 82 | 45 | 55% (44-65%) | 55% | -0.0 pts (-11 to +11) | 0.34 | 753.10 | ok |
| 200D SMA | 43 | 25 | 58% (43-72%) | 55% | +3.4 pts (-12 to +17) | 0.26 | 718.45 | ok |
| 50W SMA | 42 | 25 | 60% (44-73%) | 56% | +3.6 pts (-12 to +17) | 0.30 | 711.15 | ok |
| 200D EMA | 54 | 31 | 57% (44-70%) | 58% | -0.1 pts (-14 to +12) | 0.40 | 722.33 | ok |
| 200W SMA | 13 | 9 | 69% (42-87%) | 56% | +13.6 pts (-14 to +32) | 1.14 | 563.30 | ok |
| 50W EMA | 51 | 28 | 55% (41-68%) | 57% | -2.1 pts (-16 to +11) | 0.02 | 713.15 | ok |
| 200W EMA | 17 | 10 | 59% (36-78%) | 58% | +0.5 pts (-23 to +21) | 0.10 | 589.10 | ok |
| 100W EMA | 30 | 13 | 43% (27-61%) | 59% | -15.2 pts (-32 to +3) | 0.00 | 663.20 | ok |
| 100W SMA | 25 | 11 | 44% (27-63%) | 60% | -15.6 pts (-33 to +4) | 0.14 | 656.26 | ok |

#### AAPL — close 341.07, trend state **uptrend**, as of 2026-09-25

| Level | Tests (n) | Held | Hold rate (95% CI) | Placebo | Edge (95% CI) | Median pierce (ATR) | Level now | Status |
|---|---|---|---|---|---|---|---|---|
| 200D EMA | 45 | 31 | 69% (54-80%) | 61% | +8.0 pts (-7 to +20) | 0.80 | 292.56 | ok |
| 21D EMA | 170 | 95 | 56% (48-63%) | 59% | -3.3 pts (-11 to +4) | 0.39 | 329.94 | ok |
| 100D EMA | 83 | 50 | 60% (49-70%) | 61% | -0.3 pts (-11 to +10) | 0.50 | 310.18 | ok |
| 21W EMA | 81 | 48 | 59% (48-69%) | 61% | -1.7 pts (-13 to +9) | 0.40 | 311.31 | ok |
| 100W EMA | 33 | 22 | 67% (50-80%) | 62% | +4.7 pts (-13 to +19) | 0.44 | 261.81 | ok |
| 21D SMA | 160 | 72 | 45% (37-53%) | 50% | -5.3 pts (-13 to +3) | 0.46 | 328.69 | ok |
| 50D EMA | 117 | 64 | 55% (46-63%) | 59% | -4.0 pts (-13 to +5) | 0.35 | 321.47 | ok |
| 21W SMA | 72 | 40 | 56% (44-66%) | 57% | -1.6 pts (-13 to +10) | 0.68 | 312.96 | ok |
| 200D SMA | 42 | 25 | 60% (44-73%) | 59% | +0.3 pts (-15 to +14) | 0.06 | 287.77 | ok |
| 50W EMA | 37 | 23 | 62% (46-76%) | 61% | +1.1 pts (-15 to +15) | 0.29 | 287.63 | ok |
| 100D SMA | 76 | 40 | 53% (42-63%) | 58% | -5.5 pts (-17 to +6) | 0.53 | 311.74 | ok |
| 50D SMA | 96 | 42 | 44% (34-54%) | 55% | -11.1 pts (-21 to -1) | 0.68 | 321.83 | ok |
| 200W SMA | 8 | 6 | 75% (41-93%) | 62% | +13.0 pts (-22 to +32) | 0.53 | 219.00 | ok |
| 50W SMA | 24 | 12 | 50% (31-69%) | 60% | -9.9 pts (-29 to +9) | 0.05 | 285.02 | ok |
| 100W SMA | 25 | 11 | 44% (27-63%) | 57% | -13.4 pts (-31 to +6) | 0.97 | 254.69 | ok |
| 200W EMA | 11 | 6 | 55% (28-79%) | 69% | -14.7 pts (-42 to +10) | 0.62 | 227.06 | ok |

#### KO — close 87.81, trend state **range**, as of 2026-09-25

| Level | Tests (n) | Held | Hold rate (95% CI) | Placebo | Edge (95% CI) | Median pierce (ATR) | Level now | Status |
|---|---|---|---|---|---|---|---|---|
| 21D SMA | 153 | 94 | 61% (54-69%) | 55% | +6.4 pts (-2 to +14) | 0.64 | 88.31 | ok |
| 21D EMA | 150 | 107 | 71% (64-78%) | 66% | +5.7 pts (-2 to +13) | 0.49 | 88.20 | ok |
| 200D EMA | 85 | 57 | 67% (57-76%) | 62% | +5.2 pts (-6 to +15) | 0.32 | 80.79 | ok |
| 200W SMA | 27 | 20 | 74% (55-87%) | 63% | +11.4 pts (-8 to +25) | 0.19 | 67.46 | ok |
| 100W EMA | 48 | 32 | 67% (53-78%) | 60% | +6.3 pts (-8 to +18) | 0.33 | 74.91 | ok |
| 50W EMA | 70 | 44 | 63% (51-73%) | 63% | -0.0 pts (-12 to +11) | 0.37 | 79.77 | ok |
| 200W EMA | 27 | 19 | 70% (52-84%) | 64% | +6.6 pts (-13 to +21) | 0.36 | 69.32 | ok |
| 50D SMA | 109 | 59 | 54% (45-63%) | 58% | -3.4 pts (-13 to +6) | 0.35 | 87.48 | ok |
| 200D SMA | 62 | 37 | 60% (47-71%) | 62% | -2.4 pts (-15 to +9) | 0.15 | 79.61 | ok |
| 100D SMA | 85 | 46 | 54% (44-64%) | 59% | -5.1 pts (-16 to +5) | 0.23 | 84.24 | ok |
| 100W SMA | 38 | 23 | 61% (45-74%) | 61% | -0.2 pts (-16 to +14) | 0.06 | 73.03 | ok |
| 50D EMA | 130 | 72 | 55% (47-64%) | 63% | -7.6 pts (-16 to +1) | 0.33 | 87.12 | ok |
| 50W SMA | 61 | 37 | 61% (48-72%) | 65% | -4.0 pts (-17 to +8) | 0.48 | 78.26 | ok |
| 100D EMA | 86 | 47 | 55% (44-65%) | 61% | -6.5 pts (-17 to +4) | 0.54 | 84.62 | ok |
| 21W SMA | 77 | 39 | 51% (40-62%) | 59% | -8.4 pts (-20 to +3) | 0.39 | 84.32 | ok |
| 21W EMA | 86 | 44 | 51% (41-61%) | 62% | -10.7 pts (-21 to -0) | 0.48 | 84.69 | ok |

How to read these:
- **SPY's best level is the 100D EMA**, which held 63% against 56% on its
  reshuffled history. That's +7 points, but the CI (−3 to +16) includes zero.
- **SPY's 100W MAs are the weakest** (−15 points), but those rest on only
  25–30 tests.
- **AAPL's 50D SMA is significantly *worse* than chance**: held 44% vs 55%,
  CI −21 to −1. Price came down to it and kept going more often than on
  random paths.
- **KO's short daily MAs lead** (+6 points), again inside noise.

Strong levels across the universe at the headline horizon: CDNS 21W SMA, 200W
SMA, 100D SMA/EMA, 21W EMA; ET 100W SMA, 50D SMA/EMA, 21D EMA, 100D EMA, 21W
SMA/EMA; MO 50D EMA, 21W EMA, 100D EMA; KMI 100D EMA, 200D SMA, 21W EMA; EWZ
50W EMA/SMA, 200D EMA; AIG 200D SMA, 100W SMA; GME 100W SMA; UBER 21W SMA; BAC
100W EMA; PCG 100W EMA; AFRM 200D SMA; C 100W EMA; GE 200W EMA; VFC 200W SMA;
KRE 50D SMA. That's 32 in total, against about 26 expected by chance.

## 4. Verification

| Check | Result |
|---|---|
| `pytest tests -q` | **288 passed** (265 + 23 in `test_phase10.py`) |
| `scripts/check_pages.py` | 8/8 pages clean (Signals now opens on Levels) |
| `scripts/preflight.py` | exit 0. The technicals cache is current (0 sessions behind); other warnings as in Phase 9 |
| Acceptance: detector finds exactly the planted tests | `test_detector_finds_exactly_the_planted_tests`: 5 planted dips are found. A near miss (0.75 ATR away), a second dip before re-arming, and an approach from below are all correctly **not** tests |
| Acceptance: placebo edge ≈ 0 on a random walk | `test_placebo_edge_is_about_zero_on_a_random_walk` (|edge| < 5 pts, ≤ 2 strong), plus the 8-seed check in §2 |
| Positive control | `test_a_level_that_is_really_respected_shows_a_positive_edge` |
| Weekly no-lookahead | `test_weekly_indicators_never_look_ahead` (weekly SMA on a daily row = the last completed week's value) |
| Acceptance: SPY, AAPL, KO tables | §3 |
| Pipeline | `pipeline/run.py --data-only` runs the technicals stage (2 m 27 s cold, skipped when current) |

## 5. Known limits

- **Only moving averages are studied as levels.** Horizontal levels (prior
  swing lows, round numbers, volume nodes) are a separate study; the event
  machinery here can take any level array.
- **Long-horizon mean reversion is not removed by the null** (see §2). If a
  whole symbol lights up, read it as "this stock mean-reverts", not "this
  exact MA is support".
- **Multiple comparisons.** 16 levels × 3 horizons × 3 regimes per symbol
  across 67 symbols is thousands of intervals. The universe banner compares
  the strong count with chance, but there is no per-symbol FDR adjustment.
- **Expected-move units** in the support map use the TastyTrade IV index over
  a fixed 30 days. Proper EM (per-expiration ATM IV, straddle method) comes
  in Phase 12.
- **Trend state and the level study are informational.** Neither feeds
  ranking or strike selection yet (Phases 11–13).
- **Weekly levels before 1952** may group NYSE Saturday sessions differently
  (Phase 9 limit). Irrelevant inside the 20-year window.
