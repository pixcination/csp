# Phase 13 summary — probability engine and profit targets

Date: 2026-09-27. Roadmap: [SCREENER_ROADMAP.md §C.5](SCREENER_ROADMAP.md),
design in §B.4. Architecture: [ARCHITECTURE.md](ARCHITECTURE.md) §5.

## Headline

Every candidate, CSP or PCS, now gets the probability table from Tom's
brief, from three models shown side by side and blended:

- **P(reach 25 / 30 / 50% of max profit at any point by day d).** This is
  a curve, the value by expiry, and the median days to reach it.
- **P(expire worthless)**, which counts as "100%" and can only be realised
  by holding to expiry.
- **P(profit)**, P(touch), P(assignment) or P(max loss), and P(the short
  delta crosses the roll threshold).
- **Expected P&L, holding days and annualised return for every management
  policy, net of all fees.** A target whose net gain is under the $5
  minimum is marked.

The sheet is re-ranked by **blended EV per calendar day on buying power**,
and other sorts can be picked on the Decisions page.

| Acceptance item | Result |
|---|---|
| G-model Monte Carlo vs N(d2) within 3 standard errors | yes, in 4 cases (z = 0.86, 0.14, −0.60 measured; the test covers 4 strikes/vols/DTEs) |
| 15 tickers × all candidates in under ~60 s | **22.6 s** for 106 PCS trades on 15 tickers (30–45 DTE, 20,000 paths); **19.8 s** for 327 weekly CSP strikes on 61 tickers |
| Validation results with honest caveats | §3: the blend was within ~2 points of observed frequencies over ~4,000 synthetic entries; the caveats are real |

## Decisions

Tom said "proceed". These are the defaults I chose; each one is in
`config.yaml → prob_engine`.

| Question | Decision |
|---|---|
| Blend weights | **Equal thirds.** A model that is absent (T falling back to H) is renormalised out, not counted twice. |
| IV dynamics | **Sticky strike**: each leg keeps its own IV. **No mean reversion** by default (a half-life is configurable). **Earnings crush: IV × 0.70** from an earnings date inside the trade. |
| Headline policy | **`auto`**: hold to expiry at ≤ 14 DTE, where fees make early closes uneconomic (`exit_rules.py`), else **close at 50%**. The 21-DTE time stop is reported for trades above 21 DTE but **not** used as the headline. On the SPY/QQQ 30–45 DTE spreads it lowered EV under H and T, because it closes early and pays extrinsic still priced at IV above realised vol. |
| Default rank | Blended EV per calendar day held, per dollar of buying power, for the headline policy. Rejected rows sort last. With `risk_mode: min_pop`, a row whose **blended** P(profit) is under the minimum is also rejected. |
| Paths | 20,000, with a fixed seed per ticker and horizon, so runs are reproducible. Antithetic normals are used for G only; negating bootstrapped returns would destroy their skew. |
| Model T conditions | Trend state, RSI bucket (<30 / 30–50 / 50–70 / >70), and distance in ATR to today's strongest respected support. They are relaxed in the order support, RSI, trend when fewer than 250 matching days exist. |

## 1. What changed

- **`analytics/prob_engine.py`** (new): the models, the vectorised daily
  Black-Scholes repricing core, per-trade evaluation, policies, the blend
  and the headline policy. It works on any `strategies.base.Position`.
- **`analytics/probabilities.py`** (new): `run_sheet` builds paths once per
  ticker and horizon, evaluates every row and adds blended columns
  (`pop_blend`, `p_hit_<X>_blend`, `median_days_<X>_blend`,
  `p_touch_blend`, `headline_*`, `ev_per_day_bpr`,
  `below_min_gain_targets`, per-model POP and effective n, the T flag,
  model labels). It re-ranks and returns long tables for policies, metrics
  and curves. `SORTS` lists the UI sort options.
- **`pipeline/`**: `analyse` runs the engine after trade construction and
  logs the blended headline for each proposal. Runs persist
  `prob_policies.parquet`, `prob_metrics.parquet` and
  `prob_curves.parquet`, and the manifest records timing.
- **`scripts/validate_prob_engine.py`** (new): the walk-forward calibration
  (§3). Output goes to `data/validation/`.
- **`app/pages/1_Decisions.py`**: each proposal gets a probability panel:
  the blended headline, a G/H/T/blend table, model labels and effective n,
  a policy table net of fees with the below-minimum marker, and P(reach X%
  by day) curves. The full sheet gets a sort selector and the engine
  columns.
- **`app/pages/3_Validation.py`**: a probability-engine section with
  predicted − observed per target and model, Brier scores, a calibration
  chart and the caveats.
- **`analytics/strategies/pcs.py`**: the long leg's IV is kept on the row,
  so the engine reprices both legs at their own IVs.
- **`core/paths.validation_dir()`**, and **`tests/test_phase13.py`** (17
  tests).

## 2. What the models say, and why they differ (real data)

Take SPY 30 Oct $748/$738 (run `20260927-211850-cc01`): P(profit at expiry)
is **G 80%, H 91%, T 96%, blend 89%**; the Phase 12 empirical figure was
90%. G is risk-neutral at the short leg's IV. The market charges a variance
premium, so G sees more movement than SPY has delivered at a similar
volatility, and G's EV is about −(fees + slippage + the skew paid on the
long leg) by construction. **G is the zero-edge baseline, not a
forecast.** H and T measure the premium the trade harvests.

On the weekly CSP sheet the new ranking proposes BMY $61p, QQQ $728p and
IWM $278p (blended hold EV $75 / $101 / $143, POP 87% / 91% / 84%). CNC,
first by the Phase 12 empirical EV, drops out.

## 3. Validation (walk-forward, `scripts/validate_prob_engine.py`)

Setup: SPY, QQQ, IWM, AAPL and KO over the last 8 years, an entry every 5
trading days, 4,000 paths per model.

At each entry the script sells a **synthetic 25-delta put**, priced at IV =
20-day RV × 1.15 (the backtest's volatility-premium multiplier). The models
predict from data up to the entry date only. The actual path, repriced
daily at the same IV assumption, decides the outcome.

**Mean predicted − mean observed** (percentage points):

| DTE | Target | G | H | T | Blend | Observed |
|---|---|---|---|---|---|---|
| 30 | reach 25% | −4.1 | −0.4 | −0.2 | −1.6 | 95.4% |
| 30 | reach 50% | −4.2 | +1.4 | +1.7 | −0.3 | 90.3% |
| 30 | expire worthless | −7.5 | +2.1 | +3.1 | −0.8 | 80.3% |
| 30 | touch (closes) | +10.4 | −3.4 | −5.5 | +0.5 | 37.1% |
| 7 | reach 50% | −6.2 | −0.1 | +0.5 | −1.9 | 89.3% |
| 7 | expire worthless | −6.3 | +2.3 | +3.0 | −0.3 | 80.2% |

The 30 DTE run had 1,990 entries (about 473 independent) and the 7 DTE run
2,005 entries. Brier scores are close across models, with the blend lowest
or tied everywhere (e.g. expire worthless at 30 DTE: G 0.165, H 0.159, T
0.162, blend 0.158).

Calibration bins (expire worthless, 30 DTE, bins with n ≥ 20):
- Blend: 0.68 predicted → 0.65 observed (n 65), 0.77 → 0.79 (n 903),
  0.83 → 0.82 (n 1,022).
- H is overconfident in its top bin: 0.94 predicted → 0.87 observed
  (n 150).

**Honest caveats**
- **Synthetic prices.** There is no historical option data. The IV proxy
  embeds a fixed 1.15 volatility premium, and G simulates at that same
  proxy. G's under-prediction therefore partly reflects the multiplier
  itself, not only G.
- **Part of the blend's accuracy is cancellation**, between G's pessimism
  and H/T's optimism. That can hold or not in other regimes.
- **Narrow predictions.** Every entry is a 25-delta put, so predictions
  span a narrow range. This tests the *level* of calibration much more than
  the models' ability to tell good trades from bad.
- **Look-ahead in T is avoided by dropping support.** The support map is
  built from the full history, so validation T uses trend and RSI only.
- **One history per ticker.** Differences of 1–2 points are within noise at
  these sample sizes.

The paper book's `calibration.py` will score real fills once trades
accumulate. That is the test that counts.

## 4. Verification

- `python -m pytest tests -q`: **370 passed** (17 in `test_phase13.py`).
  These cover G vs N(d2) (4 cases, 20,000 paths); the grid pricer against
  the reference and put-call parity; bootstrap block integrity and
  reproducibility; T relaxation and fallback; non-decreasing curves; policy
  fees and the net-gain marker; no time stop at ≤ 21 DTE; spread
  max-loss / touch / roll ordering; earnings crush; blend renormalisation;
  the headline policy; `run_sheet` columns and the blended `min_pop` gate
  on real SPY bars; the table round trip; and the validation helpers.
- `python scripts/check_pages.py`: **9/9 pages clean**.
- Real runs: `20260927-211850-cc01` (PCS, 15 tickers, engine 22.6 s),
  `--quick` weekly CSP on 61 tickers (engine 19.8 s).

**One bug found and fixed while testing:** when T relaxed every condition
it was H under another name, and the blend counted H twice. It now reports
"fell back to H" and leaves the blend. Across the 106 real PCS trades, T
used its full conditions on 45, relaxed on 47 and fell back on 14.

## 5. Known limits

- **Touch** is measured on daily closes, which understates intraday
  touches.
- **Sticky-strike IV** ignores the smile moving with spot. Mean reversion
  is off by default.
- **Annualised returns on short holds are large.** A 10-day hold at 1% is
  over 40% annualised. The default rank uses EV per day on BPR, which is
  the same quantity without the compounding illusion.
- **Assignment fees apply to every in-the-money leg at expiry**, including
  cash-settled ones (conservative, as in Phase 12).
- **The blend is not validated on real option prices.** See §3.

## 6. Decisions to confirm

1. Blend weights: keep equal thirds, or down-weight G (the zero-edge
   baseline)?
2. The headline policy for trades over 14 DTE: close at 50% (current), or
   hold to expiry, which scored higher EV under H/T on the SPY/QQQ spreads?
3. An earnings crush of 30%: keep, or measure it per ticker from stored
   metrics as history accumulates?
