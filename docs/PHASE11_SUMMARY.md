# Phase 11 summary — scan request, underlying ranking, targeted chain capture

Date: 2026-09-27. Roadmap: [SCREENER_ROADMAP.md §C.3](SCREENER_ROADMAP.md).
Architecture: [ARCHITECTURE.md](ARCHITECTURE.md) §4–5.

## Headline

A run now answers an explicit **scan request**. The request covers the
strategies, the DTE range or targets, the risk mode, the account profile,
the universe, the top N and any event overrides. It is loaded from JSON
with `--request` and embedded in the manifest.

Before any chain is pulled, every registry symbol is **ranked from data
already on disk**, in about 3 seconds for 67 names. Chains are pulled only
for the top N plus any name with an open position. The chains are
**strike-filtered in expected-move units**, so 30–60 DTE pulls fit the
dxFeed budget.

The roadmap's acceptance run works:

```
python pipeline/run.py --request examples/pcs_30_45.json
```

It ranked 67 names and excluded 21 on earnings. It then pulled 30–59 DTE
chains for the top 15 in 2 m 22 s (2,130 subscriptions against 4,980
contracts listed in that window) and wrote the manifest with the request
embedded (run `20260927-194602-f1f1`).

**One finding needs Tom's attention: the ranking and the CSP gates disagree
more than expected** (§3). The last full-universe weekly CSP run found
passing strikes on 10 tickers. The final ranked top 15 contains 3 of them
(the first draft of the ranking contained 1). The weights are unvalidated
by design until Phase 14. A default top N of 15 therefore narrows the weekly
CSP search, and it can drop names like SPY and QQQ that the gates would
pass.

## Decisions

Tom said "proceed". These are the defaults I chose; each one is in
`config.yaml`.

| Question | Decision | Where |
|---|---|---|
| Default top N | **15** (roadmap default). Held names are always added. | `scan_defaults.top_n_underlyings` |
| Ranking weights | IV rank 0.25, IV/RV 0.20, liquidity 0.20, trend 0.15, support 0.10, drawdown 0.10. **Not validated**; flagged `weights_validated: false` in the manifest. | `underlying_rank.weights` |
| Roll buffer | **+14 days** (roadmap proposal): the chain window is [dte_min, dte_max + 14]. Held names get [0, max(60, that)]. | `chain_capture.roll_buffer_days` |
| Strike filter | Puts from spot − 3 EM to spot + 0.5 EM; calls from −0.5 to +1.5 EM (skew, forward and covered calls read them); never narrower than ±3% of spot. EM is per expiration. | `chain_capture.*_window_em`, `min_window_pct` |
| Subscription cap | 6,000 per symbol. Past that, the expirations furthest from the request DTE are dropped (recorded). | `chain_capture.max_subscriptions_per_symbol` |
| Events in ranking | **A gate, not a penalty.** A block before the shortest expiration excludes the name; a block inside the window marks it `partial` with the last clear DTE and keeps it. | `underlying_rank._event_status` |
| Capital in ranking | **A gate.** Checks the profile's `allowed_strategies`, and for CSP whether (spot − 1 EM) × 100 fits the per-position cap. For PCS it checks the narrowest width × 100 and `spread_approval`. | `underlying_rank._capital` |
| Stage 1 | Stays advisory (Phase 9): carried as a column, not a gate. | — |
| Default request | Reproduces the pre-Phase-11 run (CSP, `management.entry` DTE 5–10 and delta band), so the default run's numbers do not move. Only the chain universe narrows to the top N. | `ScanRequest.default()` |
| Risk modes for CSP | `delta_range` filters strikes; `min_pop` is a gate on empirical P(finish OTM), and an unknown value rejects; `max_loss_per_trade` caps contracts on (strike − fill) × 100; `max_pct_capital` replaces the per-position cap. | `candidates.evaluate_strike` |
| Account profiles | `account_profiles` added with only `default` (= `account:`). Tom's Roth / traditional IRA / taxable profiles need his numbers. | `config.yaml` |
| One-per-ticker | Removed. Every accepted strike is kept, with `best_per_ticker` marking the top one. Proposals are still drawn from best-per-ticker rows only (two strikes on one name is one bet). The Decisions page has the toggle. | `candidates.select_sheet` |
| Roadmap stages construct → probabilities → rank_trades | Kept inside `analyse` for CSP until Phases 12–13 build them out. A request without CSP ranks and pulls chains only. | `pipeline/run.py` |

## 1. What changed

- **`analytics/scan_request.py`** (new): the `ScanRequest` dataclass, with
  validation that gives readable reasons and JSON load/save. It computes
  the DTE window from a range or from targets (± 3 days), the reference DTE
  and the chain window. `resolve_universe` accepts `all | csp | stock | etf
  | index | tag:<tag> | [list]`, and cash-settled indices are included
  only when PCS is requested.
- **`analytics/underlying_rank.py`** (new): component scores (pure and
  unit-tested), the composite renormalised over components that have data
  (`coverage` reports the share), the event and capital gates,
  `chain_targets` and a manifest `summary`.
- **`data_sources/chains.py`**:
  - `capture_targets` captures in the request's window, and
    `StrikeWindow` filters strikes per expiration. The underlying is now
    quoted first, because spot sets the window.
  - Chains and quotes use the registry `tt_symbol`. **Bug fixed:**
    `BRK.B` returned an empty chain, and TastyTrade needs `BRK/B`.
  - Rows carry root, settlement and expiration type. The underlying file
    records the captured DTE window and the IV used.
  - `needs_capture` re-pulls when the block's snapshot doesn't cover the
    requested window. A recapture within a block takes the **union** of
    the stored and requested windows. Without that, a 30–59 PCS pull
    overwrote the same weekend's 0–21 snapshot and a `--quick` CSP run
    found no weekly strikes. This was measured, then fixed.
- **`pipeline/run.py`**:
  - New `rank_underlyings` stage. `chains` now covers the targets only,
    and `analyse` takes the request.
  - Added `--request`, and `RunManifest.request`.
  - A failed ranking falls back to the whole tradable universe, as before.
- **`pipeline/results.py`**: `underlyings.parquet`, `RunResults.underlyings`
  and `.request`, and a `best_per_ticker` column.
- **`analytics/candidates.py`**: `evaluate_universe(request=…)`, the
  risk-mode gates and caps, `strategy` on every row, and `select_sheet`
  keeps all accepted rows.
- **`analytics/exit_rules.screen_entry`**: the DTE window warning follows
  the request.
- **`analytics/sizing.py`**: `account_config(profile)`, and
  `AccountState.profile` / `position_pct_override`.
- **`data_sources/events.py`**: `check(..., overrides=, frame=)` merges a
  request's `event_policy_overrides` over `event_policy`.
- **`analytics/technical_study.py`**: stores each symbol's support map
  (`support_latest`) at study time, so the ranking doesn't recompute
  indicators (about 1 s per symbol). Symbols studied before Phase 11 are
  restudied once; all 67 were restudied on 2026-09-27.
- **`app/pages/1_Decisions.py`**: names the run's request, adds a "best per
  ticker" toggle, and adds an underlying-ranking expander with component
  scores, events and exclusions.
- **`examples/`**: `pcs_30_45.json`, `csp_weekly.json`,
  `csp_pcs_targets.json`.
- **`tests/test_phase11.py`**: 35 tests.

## 2. Index chains: verified live (2026-09-27)

| Symbol | Roots from `/option-chains/{sym}/nested` | Underlying quote |
|---|---|---|
| SPX | SPXW (41 expirations, PM) + SPX (20, AM) | works with `kind="equity"` (and `"index"`) |
| NDX | NDXP (35, PM) + NDX (14, AM) | same |
| RUT | RUTW (23, PM) + RUT (9, AM) | same |
| XSP | XSP (47, PM) | same |

The chain response carries `put-streamer-symbol`, and the existing OCC →
dxFeed conversion produces the same symbols (`.SPXW261016P5500`). A 30–59
DTE SPX capture kept 1,513 of 2,266 listed strikes and 2,068
subscriptions, across 10 expirations (the 20 Nov monthly appears once per
root). Every subscribed put had streamed delta and OI. XSP kept 590 of
1,032 strikes (806 subscriptions).

## 3. Does the ranking pick the names the CSP gates would pass?

The honest check is to compare the ranked top 15 for the weekly CSP
request with the tickers that had accepted strikes in the last
**full-universe** run (`20260927-175007-bba2`, before Phase 11). That run
examined 277 strikes on 61 tickers, and 10 tickers had passing strikes:
CNC, BMY, IWM, C, QQQ, CMCSA, SPY, AAPL, DIA, AMZN.

| Ranking version | Of those 10 in the top 15 | Where the rest ranked |
|---|---|---|
| First draft: IV/RV = IV index ÷ TastyTrade 30-day HV; liquidity = rating ÷ 5 | 1 (BMY) | 18–52 |
| Final: IV at the request DTE ÷ RV on the window `vrp.match_rv_window_to_dte` pairs with it (the gate's own horizon match); liquidity = rating plus log liquidity-value | 3 (AAPL, BMY, IWM) | CMCSA 16, QQQ 29, SPY 30, AMZN 32, C 40, DIA 42, CNC 58 |

The final default run (`20260927-195627-2527`) accepted 14 strikes on 3
tickers and proposed BMY $61p, IWM $278p and AAPL $335p for 2 Oct.

Why SPY and QQQ rank about 30th: at 5–10 DTE their near-the-money IV (SPY
0.99, QQQ 0.95 of 10-day RV) looks like thin premium. The CSP gate prices the
out-of-the-money put, whose IV carries the skew. The ranking has no chain,
so it can't see skew.

I stopped tuning there. Fitting weights to one day's gate outcomes is what
Phase 14's calibration is for.

Options for Tom, none of them applied:
- **(a)** Raise `top_n_underlyings` for the weekly CSP request (a pull is
  about 10 s per name).
- **(b)** Add a skew proxy from the most recent stored snapshot, when one
  exists, to the IV/RV component.
- **(c)** Leave it until Phase 14.

## 4. Verification

- `python -m pytest tests -q`: **323 passed** (35 new in `test_phase11.py`).
  `legacy/tests`: 1 passed.
- `python scripts/check_pages.py`: **8/8 pages clean**.
- `python scripts/preflight.py`: no blocking issues. It shows 3
  pre-existing warnings, all about the 1-minute archive.
- Real-data runs on 2026-09-27 (weekend block `2026-09-25_closed`):

| Run | Request | Ranked / eligible | Chains | Result |
|---|---|---|---|---|
| `20260927-194602-f1f1` | `examples/pcs_30_45.json` | 67 / 46 (21 excluded on earnings) | top 15, 30–59 DTE, 2 m 22 s | manifest with request; PCS construction deferred (Phase 12) |
| `20260927-194948-d77e` | default (CSP 5–10) | 63 / 63 | top 15 (first-draft ranking) | 1 proposal (BMY) |
| `20260927-195627-2527` | default (CSP 5–10) | 63 / 63 | top 15 (final ranking); AAPL re-pulled as the 5–59 union | 14 accepted strikes on 3 tickers, 3 proposed |

- Ranking cost is about 3 s for 67 symbols. The support maps are cached;
  the one-off restudy of all 67 symbols took 251 s.

## 5. Known limits

- **Ranking weights are unvalidated** (§3). Phase 14 calibrates them.
- **No PCS trades yet.** A PCS request ranks and pulls chains; widths, the
  strike rule and profit targets are stored and applied in Phases 12–13.
- **The ranking cannot see skew or strike-level OI.** It runs before any
  chain exists.
- **TastyTrade rates XSP and RUT liquidity at 0**, so both rank low for
  PCS. `liquidity-value` is undocumented; it is used on a fixed log scale.
- **Portfolio construction still reads `account.net_liquidating_value`**,
  not the request's profile. That only matters once a non-default profile
  exists.
- **The strike window uses one IV per symbol** (the one at the request DTE)
  across all expirations. Skewed wings far out in time may be trimmed
  slightly early; the 3-EM put wing leaves margin.
- **Events**: a `partial` name keeps expirations before the event, but the
  CSP construction applies the event check per expiration anyway, so a
  blocked expiration still rejects there.

## 6. Decisions to confirm

1. Top N 15 as the default for the **weekly CSP** request, or larger? (§3)
2. The ranking weights, and whether to add a skew proxy before Phase 14.
3. Account profiles: capital, caps and permissions for the Roth IRA, the
   traditional IRA and the taxable account.
4. Carried over: the Phase 10 placebo choice and the "strong" definition;
   `drawdown_lookback_years: 10`; BLS 2027 dates in
   `config/macro_calendar.yaml`.

## 7. Decisions taken after review (2026-09-27)

Tom's answers to §6, and what was built:

1. **Top N: whichever gives the most options.** I measured the pass rate
   against rank. Weekly chains were pulled for all 63 eligible names (about
   7 minutes), then a `--quick` run used `top_n_underlyings: all` (run
   `20260927-202209-5938`). That run accepted 42 strikes on 10 tickers.
   Where those 10 tickers rank under each weight preset:

   | Preset | Ranks of the 10 passing tickers | In top 15 / 20 / 25 / 30 / 40 |
   |---|---|---|
   | balanced | 3, 13, 15, 16, 29, 30, 32, 40, 42, 58 | 3 / 4 / 4 / 6 / 8 |
   | premium | 5, 7, 13, 20, 34, 37, 40, 42, 43, 52 | 3 / 4 / 4 / 4 / 7 |
   | liquidity | 2, 6, 11, 13, 18, 21, 24, 36, 40, 60 | 4 / 5 / 7 / 7 / 9 |
   | defensive | 2, 11, 15, 18, 19, 28, 33, 40, 47, 55 | 3 / 5 / 5 / 6 / 8 |
   | technical | 4, 15, 22, 25, 26, 27, 36, 40, 53, 56 | 2 / 2 / 4 / 6 / 8 |

   A skew proxy would only reorder the list, and no N short of the whole
   universe keeps the passing names. **The default is therefore
   `top_n_underlyings: all`**, which pulls chains for every eligible name.
   The ranking still orders the results, and a request can still set any N
   (the Run form has a checkbox and a number). `"all"` and `0` both mean
   every eligible name.
2. **Account profiles are user-defined.** Profiles are defined on the new
   **Settings** page, validated (`core/user_settings.py`), and stored in
   `config/user_settings.yaml`, which is versioned. User entries override
   the shipped `config.yaml` entries of the same name. The Run form offers
   every profile, and `sizing.account_config` resolves them.
3. **Ranking weights are user-selectable.** `underlying_rank.weight_presets`
   ships five presets: balanced (default), premium, liquidity, defensive
   and technical. A request picks one with `ranking_weights: <name>`, or
   passes its own `{component: weight}` mapping. Users add or override
   presets on the Settings page. None of the presets is validated. The
   `liquidity` preset kept the most passing names on this one day's data;
   that is a single observation, not a calibration.

Tests: 332 pass (9 more in `test_phase11.py`), and 9/9 pages pass the
headless check.
