# Phase 16 summary — Multi-strategy screener

Date: 2026-09-28. Roadmap: [SCREENER_ROADMAP.md §C.8](SCREENER_ROADMAP.md).
Architecture: [ARCHITECTURE.md](ARCHITECTURE.md) §5–7, §10.

## Headline

Strategies are now data. A YAML file describes the legs, how each strike and
expiration is chosen, when the strategy applies, and how it is managed. Seven
strategies ship: bull put, bear call, iron condor, short strangle, covered
call (buy-write), long call calendar 14/21 DTE, and PMCC diagonal.

A generic resolver turns a spec and a stored chain into a priced,
margined and sized position. The probability engine runs on it, and the
recommender ranks the strategies each name's conditions call for. They
appear on a new **Strategies** page, in the pipeline (a scan request with
`specs` or `recommend`), and in the paper book.

| Roadmap item (§C.8) | Result |
|---|---|
| 1. Strategy spec format: legs (type, side, ratio, selector), expirations, entry conditions, exit policy; ship the examples | `analytics/strategy_spec.py` and `strategies/*.yaml`, 7 specs. Strikes can be chosen by delta, moneyness, EM multiple, ATM, an offset from another leg ($, % of spot or EM), or the same strike as another leg. Expirations are chosen per role (front/back). |
| 2. Generic resolver; calendars valued at the front expiry from a term-structure assumption, flagged as model risk | `analytics/strategies/resolver.py`. `Position` and the probability engine now handle stock legs and several expirations. A later leg is valued at the **forward vol** implied by the entry term structure. Rows are flagged `multi_expiry` with a model-risk warning. |
| 3. Recommender: condition matrix (IV regime × trend × event) → strategy families, ranked by risk-adjusted EV; or pick one spec and scan the universe | `analytics/recommender.py`. The condition matrix is built from the specs' own entry conditions. Ranking is blended EV per day on buying power, the same measure as the CSP/PCS sheet. |
| 4. Margin/BPR per strategy class and account-profile permissions | `analytics/margin.py`: cash-secured, defined risk, covered, and naked (the broker formula). Permissions come from profile fields `spread_approval`, `naked_approval` and `account_type`, all editable on the Settings page. |
| 5. Historical options data in backtests | **Not done.** No data has been purchased (§6). |

## Decisions (defaults; you said "run sprint 16")

| Question | Decision |
|---|---|
| Where specs live | `strategies/*.yaml` at the project root, versioned. Add a strategy by adding a file. |
| CSP and PCS | They stay on their own proven paths (the Screener and its golden test). `bull_put` is the same trade as a spec, so the recommender can weigh it against the others. |
| Condition matrix | Built from each spec's `entry` block (IV regime, trend, earnings), not kept as a separate table. IV regime comes from the TastyTrade IV rank: below 0.25 is low, above 0.50 is high (`recommender.iv_regime`). |
| Explicit specs vs recommend | Explicit specs are resolved on every name whatever the conditions; each row says whether the conditions were met. `recommend` resolves only specs whose conditions a name meets. |
| Event policy for specs | Each spec says whose policy it follows (`event_policy_as: pcs` or `covered_call`). `entry.earnings: allow` turns an earnings block into a warning. |
| Naked permission | Requires `naked_approval: true` and `account_type: margin`. Never allowed in an IRA. The default profile cannot trade the strangle. |
| Headline policy | The spec's own profit target (calendar: close at 25%), else the engine's auto policy. The spec's loss stop is applied to open positions, but not yet in the engine's policy table. |
| Paper book | Records any option-only spec position: legs, max profit, BPR. Specs with a stock leg are refused: buy the shares at the broker and write the call on the Wheel page. |
| Management of spec positions | Each spec's `exit` block: value floor, then loss stop (a multiple of the credit, or of the debit for calendars and diagonals), then profit target net of fees, then time stop. The pipeline marks these positions and reviews them like CSPs and spreads. |

## 1. What changed

**Specs**
- `analytics/strategy_spec.py`: loader, validation, `applies`,
  `condition_matrix`, `iv_regime`.
- Seven specs in `strategies/`.

**Positions**
- `strategies/base.py`: stock legs; several expirations; `later_leg_vol`
  (forward vol); a dense price grid for max profit, max loss and
  breakevens when the payoff is not piecewise linear.

**Probability engine** (`prob_engine.py`)
- Stock legs, net of the risk-free carry on the share capital.
- A time-to-expiry per leg; a later leg is valued at the forward vol when
  the front expires, and closing it at the front expiry is charged as a fee.
- Touch and ITM counted on both sides (condors, strangles).
- G uses the mean short-leg IV when puts and calls are both short.

Single-expiry option positions compute exactly as before: the CSP golden
test and the Phase 13 and 15 tests are unchanged.

**New modules**
- `strategies/resolver.py`: chooses expirations, skipping listed but
  unquoted ones such as the Oct 12 weekly; selects strikes; prices the
  fill; calculates buying power and sizing; empirical POP and EV; gates.
  Each leg's model IV is implied from its own mid by our pricer (§3).
- `costs.multi_leg_fill`: fills for any number of legs, credit or debit.
- `analytics/margin.py`.
- `analytics/recommender.py`.

**Wiring**
- **ScanRequest:** `specs`, `recommend`. The chain capture window grows
  to cover the specs' expirations.
- **Pipeline:** a `strategies` stage writes `strategies.parquet` and
  `strategy_conditions.parquet` into the run folder.
- **Example request:** `examples/recommend_strategies.json`.

**Book and management**
- `paper.py` records spec positions: legs from JSON, `max_profit_share`,
  a legs label. It also scores a position's profit against its max profit
  rather than its credit, which a calendar's debit needs.
- `exit_rules.evaluate_spec_position`.
- Pipeline review of open spec positions.

**Pages**
- New **Strategies** page with three tabs:
  - **Recommend:** results from the latest run, or a scan of the stored
    chains now. Grid; payoff chart; legs; probabilities; record to the
    book.
  - **Library:** every spec, and whether your profile may trade it.
  - **Condition matrix:** the matrix and today's conditions per name.
- **Screener:** "Also scan strategy specs" and "Recommend by conditions".
- **Settings:** account type and naked approval.

**Tests**
- `tests/test_phase16.py`: 26 tests.

## 2. On real data

`pipeline/run.py --quick --tickers SPY,QQQ,IWM,DIA --request
examples/recommend_strategies.json` runs on the stored chains (captured 25
Sep). Run `20260928-091919-c518`. The recommender stage took **8 s** and
resolved 13 positions.

Conditions: all four names are in a **range**. IV rank:
- SPY 0.31 (mid)
- QQQ 0.45 (mid)
- DIA 0.28 (mid)
- IWM 0.23 (low)

So IWM's credit strategies were not recommended; its calendar and covered
call were. The strangle needs high IV; the PMCC needs an uptrend.

| | Legs | Net / share | Contracts | Blended POP | Headline EV | Status |
|---|---|---|---|---|---|---|
| QQQ call calendar | −745C 9 Oct / +745C 16 Oct | −$2.90 | 49 | 46% | +$484 | accepted |
| IWM call calendar | −282C / +282C | −$1.13 | 12 | 47% | +$21 | accepted |
| SPY covered call | +100 sh / −789C 30 Oct | −$766.73 | 1 | 70% | +$517 | accepted |
| SPY call calendar | −772C / +772C | −$2.21 | 51 | 43% | −$178 | accepted, ranks low |
| SPY iron condor | −731P/+710P/−806C/+830C 6 Nov | +$3.39 | 0 | 73% | −$102 | rejected: open interest |
| SPY / QQQ bull put, bear call | ~4% wide, 6 Nov | | 0 | 74–91% | | rejected: open interest |

**Reading.**
- **Why most spreads are rejected.** The same liquidity floor as in
  Phase 15: 4%-wide strikes on the Nov 6 weekly have almost no open
  interest. A fresh capture includes Nov 13 and the monthlies.
- **The SPY calendar's headline EV is negative.** It still passes its
  gates, because the gate uses the empirical EV. That is how the CSP/PCS
  sheet already works; it ranks below the positive rows.
- **The covered call's +$517 comes from model H.** H carries SPY's
  historical drift. Model G, which has no drift beyond the risk-free rate,
  puts it near zero.

## 3. Two pricing problems found and fixed

**A calendar showed a large false edge.** The first build put the SPY
14/21 calendar at about +$12 per contract under G. G is the market-implied
model, so it should show no edge. Two causes:
1. **The dividend assumption.** Our Black-Scholes has no dividend yield,
   but the chain's IVs are computed with one. Pricing the back call at
   the chain IV valued it about $0.60 above its own mid. The resolver now
   implies each leg's model IV from its mid with our own pricer, so the
   model reproduces entry prices exactly.
2. **The back-month assumption.** "Back-month IV holds at its own level"
   is not consistent with the term structure. At the front expiry the
   later leg is now valued at the forward vol:
   σ²_fwd = (σ_b² T_b − σ_f² T_f) / (T_b − T_f).

After both fixes, G gives −$5 to −$6 per contract on the SPY and QQQ
calendars, which is the fees: a zero-edge baseline, as intended. A test
pins this.

**A covered call showed +$300 under G.** That was the risk-free drift on
$77k of shares. Stock legs are now measured net of the carry that capital
would earn in cash, so G gives about $0. A test pins this too.

**What the forward-vol assumption does not cover.** It is still an
assumption. A calendar's main risk is back-month IV falling after entry,
and nothing here models that; every multi-expiry row carries a model-risk
warning. G remains pessimistic on credit spreads (the skew bias measured
in Phase 15); H and T are the realistic reads.

## 4. Verification

- `python -m pytest tests -q`: **451 passed**. The 26 Phase 16 tests cover:
  - **Specs:** all seven load and describe themselves; the condition
    matrix; conditions and IV regime; five kinds of invalid spec.
  - **Positions:** covered-call payoff, max loss, delta and BPR; calendar
    payoff, max loss equal to the debit, the forward-vol formula and the
    entry value; single-expiry positions unchanged.
  - **Model G is zero-edge** for a calendar and for a covered call.
  - **Fills and margin:** multi-leg fills (credit, debit, refusal); the
    naked formula, strangle BPR and permissions.
  - **Resolver (synthetic chain):** strike order and widths of an iron
    condor; the nearest-45 expiry; a calendar skipping an unquoted expiry;
    a strangle refused for the default profile; covered-call BPR.
  - **Scan request:** the chain window grows for specs; unknown specs are
    refused.
  - **Run tables:** strategy tables round-trip through a run folder.
  - **Recommender:** ranks positions on the real SPY chain.
  - **Paper book:** records, marks and closes spec positions; refuses to
    settle a calendar; refuses stock legs.
  - **Management and pipeline:** the spec-position rules; the pipeline
    review of an open spec position.
  - **Pages:** AppTest of the Strategies page, including a scan.
- `python scripts/check_pages.py`: **12/12 pages clean**.
- `python scripts/preflight.py`: no blocking issues. The known 1-minute
  archive warnings remain.

## 5. Decisions to confirm

1. **IV regime thresholds:** IV rank below 0.25 is low, above 0.50 is
   high. Are these right for your universe?
2. **Default profile and naked strategies:** the strangle is blocked
   everywhere until a profile is set to `margin` with naked approval. Do
   you want a margin profile defined?
3. **Loss stops in the engine's policy table.** Spec positions are managed
   with their loss stops, but the engine's headline EV assumes the profit
   target only. Should stop-loss policies be added to the engine? That
   helps every strategy, CSP and PCS included.
4. **PMCC coverage.** Its deep ITM, ~90 DTE long call is usually outside
   the default chain capture. Choose one:
   - widen `chain_capture.call_window_em` whenever PMCC is requested, or
   - leave it to requests that ask for it.

## 6. Known limits

- **No historical option quotes** (roadmap B.8). Multi-leg backtests,
  calendars above all, need them. The PCS backtest stays synthetic, and
  there is no backtest yet for the other specs.
- **The calendar term structure is an assumption.** It is forward-vol
  consistent at entry; IV moves after entry are not modelled.
- **Probabilities for new strategies are not validated** against
  outcomes. Only short puts (Phase 13) and put spreads (Phase 15) are.
- **The G model uses one lognormal.** With skew it is biased on condors
  and strangles; the mean short-leg IV is a compromise.
- **Stock legs:** the buy-write is priced and ranked, but the paper book
  does not record the shares. Covered calls against assigned shares live
  on the Wheel page.
- **Liquidity floors** (250 open interest, 25 contracts of volume) apply
  to every leg, so wings on weekly expiries often fail them.
