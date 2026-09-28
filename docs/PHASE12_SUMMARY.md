# Phase 12 summary — options analytics, strategy package, PCS construction

Date: 2026-09-27. Roadmap: [SCREENER_ROADMAP.md §C.4](SCREENER_ROADMAP.md),
design in §B.5–B.6. Architecture: [ARCHITECTURE.md](ARCHITECTURE.md) §5.

## Headline

Every candidate trade, CSP or put credit spread, is now fully specified:
pricing (mid, natural and modelled fill), liquidity of every leg, the
expected move and the short strike's distance in EM units, the nearest
support, risk (max loss, breakeven, buying power, net Greeks), empirical
probabilities and EV net of every fee. The row also records which rule
chose the short strike, and why.

**Acceptance run** (`20260927-204444-d566`,
`--request examples/pcs_30_45.json --tickers SPY,QQQ`):

- 28 PCS rows: SPY 16, QQQ 12.
- 4 widths each ($1, $2.5→$3, $5 and $10, snapped to listed strikes).
- 2 expirations, and 2 short-strike rules (delta, EM multiple).
- 7 pass every gate. The best: SPY 30 Oct $748/$743 at $0.715 credit,
  POP 89%, P(max loss) 9.7%, **43% annualised EV on risk**, 11 contracts.

**The CSP port changes no numbers.** `candidates.evaluate_strike` moved
verbatim into `analytics/strategies/csp.py`. A golden test replays 21
strikes on 5 tickers (real chain rows and bars, frozen 2026-09-27 from the
pre-port code) and matches every figure to 1e-12: EV, fill, contracts,
fees, probabilities, the binding constraint and the rejection text. The
default CSP run after the port proposes the same trades at the same EVs:
BMY $61p 14.8% and IWM $278p 12.8%.

## Decisions

Tom said "proceed". These are the defaults I chose; each one is in
`config.yaml`.

| Question | Decision |
|---|---|
| Expected-move formula | **tastytrade's own**, as the default. The roadmap asked me to confirm what the platform uses. The tastytrade Help Center article "Expected Move in the tastytrade trading platform" gives **0.6 × ATM straddle + 0.3 × 1st OTM strangle + 0.1 × 2nd OTM strangle**, which is neither roadmap method. All three methods are on every row: tastytrade, `iv` (S × ATM IV × √(DTE/365), IV at the implied forward) and `straddle` (0.85 × straddle). |
| Default widths | $1 / $2.5 / $5 / $10 (roadmap), snapped to listed strikes. A width far from the request is warned; see §3 on indices. |
| Strike-rule default | **`conservative`**: delta, EM multiple and support rules are all built. The lowest short strike that passes every gate is marked `default_choice` per ticker, expiration and tier (roadmap: "default shows the most conservative that passes"). |
| Credit/width floor | 1/3, as a **warning, not a rejection**. At/above it, the row gets a premium flag. |
| Earnings on single-stock PCS | **Blocked like CSP** (roadmap recommendation). The Phase 9 `event_policy` already listed `pcs` under earnings. |
| Short-leg IV/RV gate for PCS | Applied, with the same floor and horizon match as CSP (`pcs.apply_iv_rv_gate`). |
| Fill model | Net mid minus 40% of the summed half-spreads, clamped between the natural and the net mid. |
| Cash-settled exit fees | The $5 assignment/exercise fee is still charged on index spreads (conservative) until a statement shows otherwise. |
| Risk tiers | By width, narrowest first: Conservative / Moderate / Aggressive (with 4 widths: C, C, M, A). |

## 1. What changed

- **`analytics/expected_move.py`** (new): the three methods,
  `for_expiration` / `for_chain` (per expiration and per root), bands,
  `distance`, and `containment`, the share of past windows inside 1× / 2×
  the EM. Containment uses stored IV where there is history and a labelled
  RV proxy otherwise. SPY 5-day: 67.9% inside 1×, 94.1% inside 2×; normal
  would be 68.3% / 95.5%.
- **`analytics/liquidity.py`** (new): per-leg OI, volume, $ and % spread,
  and a fillability score from 0 to 1 (spread, OI and volume parts; a
  penny-wide market scores at least 0.8). Also the position's weakest leg
  and OI walls (OI ≥ 3× the side's median).
- **`analytics/strategies/`**: `strategies.py` became a package, and
  `STRATEGIES` stays importable.
  - `base.py`: `Leg` and `Position`, with payoff, Black-Scholes value, max
    profit/loss, breakevens, BPR (collateral for a CSP, max loss for a
    spread) and signed net Greeks.
  - `csp.py`: the CSP evaluation, moved verbatim (and `as_position`).
  - `pcs.py`: short-strike rules, the width ladder, pricing, sizing, the
    empirical POP / P(short ITM) / P(max loss) / P(touch), EV net of entry
    fees and each outcome's exit fees, gates, tiers and notes.
    Cash-settled rows say AM or PM settlement; American rows note early
    assignment.
  - `context.py`: EM, nearest and strongest support, liquidity, OI wall,
    premium flags and the strike rule on every row, CSP included. These
    are added after `evaluate_strike`, so CSP numbers are untouched.
- **`analytics/costs.py`**: `legs_open` / `legs_close` (commission cap
  per leg), `vertical_exit_fees` (expire / close / short ITM / max loss,
  physical vs cash), `vertical_economics`, and `package_fill`.
- **`analytics/sizing.py`**: `max_contracts_for_position(per_contract, legs=…)`.
  The liquidity caps apply to every leg, so the thinnest binds.
  `max_contracts_for_strike` is now a one-leg wrapper, pinned by the golden
  test.
- **`analytics/moves.py`**: `select_windows` is factored out of
  `breach_probabilities`, so PCS EV reads the same sample as the CSP.
- **`analytics/candidates.py`**: `evaluate_universe` builds CSP and PCS
  rows per ticker and gives every row a `trade_id`. PCS rows share (ticker,
  expiration, strike) across widths, and SPX lists strikes on two roots.
- **`pipeline/`**: `analyse` constructs PCS, and `by_strategy` counts go in
  the manifest. `annotate_sheet` matches rows on `trade_id`, and the run
  log describes spreads.
- **`data_sources/chains.py`**: each snapshot records the expirations the
  subscription cap dropped, and a request that needs one re-pulls.
- **`app/pages/1_Decisions.py`**: PCS proposal cards (legs, max loss, POP,
  P(max loss), credit/width, rule, EM distance, support, premium,
  fillability, notes). The full sheet gains strategy, long strike, width,
  tier, rule, EM, fillability, flags and default choice.
- **`analytics/paper.py`**: refuses to record a spread until the
  multi-leg book exists (Phase 15).
- **`examples/pcs_30_45.json`**: uses the `conservative` rule.
- **`tests/test_phase12.py`** (21 tests) plus
  `tests/fixtures/csp_golden/` (592 KB of real chain rows and bars).

## 2. Bugs found and fixed while verifying on real data

1. **A stale snapshot passed the coverage check.** A weekly pull (reference
   7.5 DTE) had recorded a 0–59 DTE window, but the subscription cap had
   dropped its far expirations. A later 30–45 PCS request saw "covered" and
   built SPY on 2 expirations. Snapshots now record the dropped
   expirations, and `needs_capture` re-pulls when a requested one is
   missing.
2. **An empty ticker list analysed the whole universe.**
   `evaluate_universe([])` fell through `tickers or load_universe()`. The
   run was also filtering targets to CSP-tradable names even for PCS
   requests, so `--tickers SPX,XSP` silently analysed the top 15 stocks
   instead. Both are fixed and tested.

## 3. Index spreads (real data, `20260927-204718-188c`)

SPX (SPXW, PM) and XSP spreads are built, carry the "cash-settled,
European: no early assignment; PM-settled" note, and have unique trade ids
across roots. XSP 30 Oct $750/$740 passed: 8 contracts, POP 89%, EV 15%
annualised on risk.

**Widths are dollars.** On SPX's 25-point strikes the $1–$10 ladder snaps to
a single 25-wide spread. That row now warns "use widths that fit this
chain's strike spacing". An index request should use e.g. `[25, 50, 100]`
for SPX and `[5, 10]` for XSP. Most SPX spreads were then rejected on
weekend volume and OI of 0–2 contracts on far-OTM strikes.

## 4. Verification

- `python -m pytest tests -q`: **353 passed** (21 in `test_phase12.py`,
  including the CSP golden test). This includes hand-calculated EM tests
  (S × IV × √(t): 5.7338; tastytrade 0.6 / 0.3 / 0.1 = 4.62; 0.85 × 6 =
  5.10), and on a Black-Scholes chain: the ATM IV recovered to 0.2%, and
  straddle-EM ÷ IV-EM = 0.85 × 0.798.
- PCS payoff, max loss, breakeven, BPR and Greeks are tested by hand. So
  are vertical fees per outcome and the per-leg commission cap, the fill
  model bounds, fillability, walls, the thinnest-leg sizing cap, the three
  strike rules and snapping, tiers, default choice, trade-id uniqueness and
  the refusal to book a spread.
- `python scripts/check_pages.py`: **9/9 pages clean**.
- Real-data runs:

| Run | What | Result |
|---|---|---|
| `20260927-204444-d566` | PCS 30–45, SPY + QQQ (forced re-pull) | 28 rows, 4 widths each, 7 accepted, SPY $748/$743 proposed (43% EV on risk) |
| `20260927-204718-188c` | PCS 30–45, SPX + XSP | 34 rows (SPXW 15, XSP 19), XSP $750/$740 proposed |
| `20260927-204904-e34f` | default CSP, all 63 names (`--quick`) | 327 strikes, 42 accepted on 10 tickers, the same proposals and EVs as before the port, context columns filled |

## 5. Known limits

- **POP and EV are at expiry, from the empirical sample only.** The
  three-model engine (market-implied, historical, technical) and the
  path-dependent odds (P(reach 50% by day d)) are Phase 13.
- **Spreads can't be recorded or managed yet** (Phase 15).
- **Weekend chains:** OI and volume are often 0 on far-OTM strikes outside
  RTH, which rejects many long legs on the OI/volume floors. A weekday RTH
  run will show more passing spreads.
- **The support rule rarely fires.** Only 11 strong levels exist across the
  universe (Phase 10), so most tickers get delta and EM rows only.
- **Cash-settled exercise fees** are charged conservatively.
- **The golden test depends on `config.yaml`** account, liquidity, cost and
  entry settings as of 2026-09-27. Changing those is a deliberate reason to
  regenerate it.

## 6. Decisions to confirm

1. Keep tastytrade's formula as the default EM (all three are shown).
2. Credit/width below 1/3: a warning (current) or a gate?
3. Default widths for index requests (e.g. SPX 25/50/100, XSP 5/10). Should
   widths also be allowed as a percentage of spot, so one ladder fits every
   name?
4. Cash-settled assignment fee: keep charging it?

Sources for the EM formula: [tastytrade Help Center — Expected Move in the
tastytrade trading platform](https://support.tastytrade.com/support/s/solutions/articles/43000435415).
