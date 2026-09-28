# Phase 17 — Live shakedown and open decisions

Written 2026-09-28 for Tom and for the next Claude Code session. The work
order is `docs/REVIEW_P8-16_AND_NEXT_PHASES.md` Part E, Phase 17.

## 1. What Tom decided (2026-09-28)

| Question | Answer | Where it lives |
|---|---|---|
| Account profiles | Create `roth_ira`, `traditional_ira` and `taxable` with **placeholder** values; Tom enters the real ones on Settings | `config/user_settings.yaml` (`placeholder: true`) |
| Naked approval in taxable | Yes, **research-only** | `taxable`: `account_type: margin`, `naked_approval`, `naked_research_only` |
| Gap risk source | Daily bars | `signals.gap_source: daily` |
| Review B.2 table | Apply as written | see §4 |
| Review B.1 decisions 1, 3 and 4 | As the review recommends (IV percentile regime; the shipped rules as headline; targeted PMCC widening) | §3 |

## 2. Live RTH runs (Monday 2026-09-28)

These were the first runs against a live market. All earlier Phase 8–16 runs
read closed-session chains.

| Run | Block | Request | Rows | Accepted | Result |
|---|---|---|---|---|---|
| `20260928-095644-ce7f` | rth_09 / chains rth_10 | default CSP (5–10 DTE) | 180 on **8 of 63** names | 19 | 2 proposed (QQQ, IWM) |
| `20260928-101552-fb47` | rth_10 | default PCS (4 % wide, nearest 45 DTE) — **before** the fixes | 47 | **0** | empty sheet |
| `20260928-122059-759a` | rth_12 | default PCS — **after** the fixes | 110 | **9** (5 names) | 2 proposed (SPY, EWZ) |
| `20260928-101232-2bfc` / `…-122958-e4c3` / `…-123614-1187` | rth_10 / 12 | `recommend_strategies.json` (ETFs) | 43–48 | 0 → 10 | |

Closed-session comparison (`20260927-211952-9bf3`, CSP): 327 rows on 61 names,
42 accepted.

### 2.1 The CSP window holds no Friday on a Monday

The default CSP run built rows for only 8 names. This is a calendar effect,
not an RTH one. On Monday 5–10 DTE runs from Oct 3 to Oct 8. Friday Oct 2 is
4 DTE and Friday Oct 9 is 11, so only names with Monday/Wednesday expirations
(SPY, QQQ, IWM, a few mega-caps) have an expiration in the window. The
Sunday closed-session run caught Oct 2 at 5 DTE. **Open decision** (§6).

### 2.2 Why the default PCS sheet was empty

The OI floor rejected 45 of 47 short legs and 41 of 47 long legs. Open
interest is set overnight, so the session doesn't matter. The cause was
the expiration: "nearest 45 DTE" picked **Nov 6, a weekly listed only days
earlier**, while the Nov 20 monthly two weeks later holds the open interest:

| Put OI | Nov 6 weekly (39 DTE) | Nov 20 monthly (53 DTE) |
|---|---|---|
| CVX | 45 | 8,240 |
| WMT | 177 | 49,533 |
| AIG | 1 | 1,197 |
| IWM | 4,412 | 376,622 |

After the fixes (§3.4), all **9 accepted spreads are on the Nov 20 monthly;
all 46 Nov 6 rows still fail**. The remaining rejections are mostly the
empirical-EV gate (46) and the short-leg IV/RV gate (40), which are the
intended gates.

### 2.3 A request-file pitfall found on the way

The first "default PCS" run tested the wrong thing. `ScanRequest.from_dict`,
and so every JSON request, fills missing fields with the **dataclass
defaults** (5–10 DTE, dollar widths, top 15), not `config.yaml →
scan_defaults`. Only `ScanRequest.default()` reads `scan_defaults`.
`examples/pcs_default.json` now writes the 4 % / 45 DTE / all-names defaults
out explicitly. Whether JSON requests should inherit `scan_defaults` is an
open decision (§6).

## 3. What was built

### 3.1 Account profiles, and no silent $3M default

- Three placeholder profiles: `roth_ira` $100k, `traditional_ira` $250k,
  `taxable` $500k margin. Each has 10 % per position, 15 % per ticker, 30 %
  per sector, 10 positions, a 10 % buffer, cash-secured, spreads approved.
- New profile fields: `placeholder` and `naked_research_only`. Both are
  editable on Settings and shown in its table.
- The Screener and Command Center no longer preselect a profile. A request
  cannot run until one is chosen. The picker shows value, type and
  `(PLACEHOLDER)`, and the Screener warns while a placeholder profile is
  selected. `default` ($3M research) can still be chosen, but only on
  purpose. The CLI default request is unchanged, so the golden test is
  unaffected.
- The recommender flags naked specs on a `naked_research_only` profile as
  `research_only`. Phase 19's auto-logging must skip them.

### 3.2 IV regime (decision 1)

- **Measure:** TastyTrade IV percentile (`recommender.iv_regime.measure:
  ivp`), clamped to [0, 1]. It falls back to the clamped IV rank when IVP is
  missing. The underlying ranking's `iv_rank` component now clamps both
  inputs (EWZ's tos rank read 1.11).
- **Hysteresis 0.03** against each symbol's previous metrics snapshot
  (`tasty_metrics.previous`). Live example: QQQ was low yesterday and moved
  to mid only because IVP 0.32 cleared 0.28.
- **Soft gate:** a spec outside its preferred regimes still resolves. It is
  reported ("prefers …; soft"), marked `regime_fit = False`, and ranked after
  in-regime rows. Only the extremes exclude: credit specs below IVP 0.10
  (live: KWEB 0.096) and debit specs above 0.90. Specs gained a `premium:
  credit | debit` field; `call_calendar` and `pmcc` are debit.
- The condition matrix still shows preferences (the regime applied
  strictly).

### 3.3 Loss stops in the probability engine (decision 3)

- New policies: `stop_{k}x`, `close_{X}_stop_{k}x`, and each of them
  `…_or_21dte`. They report `p_stopped` and appear in Trade Detail's policy
  table next to the existing ones. Adding stops leaves every existing
  policy's numbers unchanged (tested).
- **The headline is now the rules actually run** (`prob_engine.headline_policy:
  shipped`):
  - Put spreads follow `management.spread`: the 50 % target only above the
    14-DTE hold horizon, the 2× stop always, the 21-DTE time stop when
    entered above it. At 45 DTE that is `close_50_stop_2x_or_21dte`.
  - Spec positions follow their own `exit` block (e.g. the calendar's
    `close_25_stop_0.5x`).
  - CSPs keep `auto`: they are rolled, not stopped.
- **Stops trigger on intraday extremes.** H and T bootstrap each day's real
  low and high (relative to the prior close) with the same draws as the
  closes. G uses the Brownian-bridge extremes between closes. A stop fills at
  its level, or at the close when the close is already through it.
- **IV follows spot (H and T).** Each leg's IV is multiplied by
  exp(β·log return), clipped to 0.5–3× the entry IV. The index β is
  d ln VIX / d ln SPY, estimated from the reference data at **−5.3**
  (2021–2026, correlation −0.76). It is scaled to each name by
  ρ·σ_SPY/σ_name: SPY −5.3, QQQ −3.2, NVDA −1.2. G stays sticky-strike so it
  remains the zero-edge baseline.

**How the default rankings changed.** On the live PCS run
(`20260928-122059-759a`), comparing `close_50` with the shipped policy:

| | Rows | Spearman | Top-5 overlap | Mean EV `close_50` | Mean EV shipped | Mean EV hold | P(stopped) |
|---|---|---|---|---|---|---|---|
| All rows | 110 | 0.961 | 2/5 | $182 | $154 | $307 | 26.5 % |
| Accepted | 9 | 0.967 | 5/5 | $697 | $915 | $1,789 | 10.4 % |

The order barely moves among tradable rows. The accepted mean rises because
the 2× stop cuts EWZ's loss tail (EWZ 34/33: −$2,182 → −$235).

**What the two model fixes do** (the 9 accepted rows, 10,000 paths, shipped
policy, blend):

| Model | EV shipped | P(stopped) |
|---|---|---|
| closes, sticky strike (Phase 15/16 behaviour) | $908 | 8.6 % |
| + intraday triggers | $795 | 10.5 % |
| + spot-vol only | $1,032 | 8.5 % |
| **both (now shipped)** | **$921** | 10.4 % |

The review expected these fixes to make the stop policies look less costly.
They don't, on net:
- Intraday triggers fire stops about 2 points more often and cost ~$110.
- Spot-vol makes targets cheaper to reach on the up-paths (+~$125).
- **Hold-to-expiry still shows about twice the EV of the shipped rules
  under H and T.** The measured cost of the protection stands: it is your
  choice, now visible per trade.

### 3.4 Spread liquidity fallbacks

- **Monthlies.** Spread DTE targets from `scan_defaults.prefer_monthly_from_dte`
  (30) also build at the nearest monthly (`expiration_type == Regular`, else
  the third Friday), next to the nearest expiration; the gates pick between
  them. Spec roles from 30 DTE take the monthly outright when one is inside
  their tolerance.
- **Long leg snapped to OI.** When the long leg's OI is under the spread-leg
  floor, the nearest strike within ±25 % of the width that clears it is used
  instead (`pcs.long_leg_oi_snap_band`).
- **Spread-leg floors** (`liquidity_limits.spread_legs`): OI at least 100,
  and **volume off as a gate** (0). The 3 %-of-OI cap still applies, so n
  contracts need OI ≥ n / 0.03: the requirement scales with size and 100 is
  only the minimum. Single-leg CSPs keep 250 / 25, and the golden test is
  unchanged.

### 3.5 Targeted PMCC chain widening (decision 4)

For a request that recommends or names a spec in
`chain_capture.widen_for_specs` (`pmcc`), `chains.spec_widening` adds a
call band:
- deltas 0.68–0.92 (the long call's 0.80 ± `widen_delta_pad`), converted to
  strikes by Black-Scholes at the window IV
- only at that role's expirations (60–120 DTE)
- only for names whose PMCC entry trend holds (uptrend)

Each ticker's `extra_subscriptions` is recorded in the manifest. On
2026-09-28 no ETF was in an uptrend (6 of 67 names are), so the live
recommender runs added 0 subscriptions.

### 3.6 Gaps on daily bars

`gaps.session_frame` now reads the yfinance daily open/high/low/close on the
price basis by default, completed sessions only. `gap_source: 1m` or `auto`
brings back the 1-minute archive, which is 89 days stale. The seam filter
runs only on the 1-minute path. SPY: 5,028 days, 36.5 % of daily variance
overnight, worst gap −10.4 % (2020-03-16). The Signals and Trade Detail
wording was updated to match.

### 3.7 Other fixes

- **Trade Detail loaded the wrong chain.** It read the latest block for the
  ticker, so this morning's RTH pulls hid the Oct 2 expiration from
  yesterday's runs ("no longer on disk"). It now loads the run's own chains
  block, falling back to the manifest's session block (for `--quick` runs).
- `ScanRequest.default()` failed on any new `scan_defaults` key. New keys are
  now popped there.
- A variable collision in `recommender._conditions` put the trend state into
  `iv_value`, so the regime was never computed. It was found on the live run
  and fixed, with a regression test.

## 4. Review B.2, applied

| Item | Done |
|---|---|
| Default ranking preset | `underlying_rank.default_weight_preset: calibrated` |
| Trend/support weights | Unchanged: 0 in `calibrated`, kept in other presets |
| Grid default, landing page | Unchanged (passing rows; Screener) |
| Blend weights, 30 % earnings crush | Unchanged; re-fit in Phase 21 |
| Headline above 14 DTE | The shipped rules (§3.3) |
| Index widths | `spread_width_pct` already applies to indices. Live check: SPX 4 % → 305–325 wide on 5-point strikes, XSP 30 — sensible |
| Credit/width < 1/3 | Stays a warning |
| $5 fee on cash-settled spreads | **Not done:** needs a tastytrade statement for a cash-settled expiry |
| Stage 1 drawdown 10 y; index RV floor | 10 y kept; `stage1_thresholds.min_rv_broad_index: 0.08` for `broad_index_*` ETFs |
| BLS 2027 dates | **Not done:** a reminder for when BLS publishes them |

## 5. Checks

- `pytest tests`: **475 passed**, including 24 new tests in
  `tests/test_phase17.py`. Five older tests were updated for intended
  changes: the default preset, the soft regime, the shipped headline, and the
  Screener's required profile.
- `scripts/check_pages.py`: 12/12 pages clean.
- `scripts/preflight.py`: no blocking issues. Three warnings about the
  1-minute archive, which is now optional.

## 6. Open decisions for Tom

1. **CSP window on Mondays (§2.1).** Options:
   - (a) `management.entry.dte_max: 11`
   - (b) CSP by DTE target 7 ± 4
   - (c) leave it

   (a) or (b) changes the golden test's inputs, so regenerate it on
   purpose. Recommendation: (a).
2. **Spread-leg floors (§3.4).** Confirm OI 100 and no volume gate for spread
   legs, or give other numbers.
3. **JSON requests and `scan_defaults` (§2.3).** Should a saved request
   inherit missing fields from `scan_defaults`? Recommendation: yes. The
   caveat is that `spread_width_pct` would then override the dollar widths in
   old files unless they set it to null.
4. **Enter real profile values** on Settings and untick "placeholder". Until
   then the default PCS sheet sizes against $3M. Example: EWZ ×523 contracts,
   capped only by 3 % of OI.
5. The $5 cash-settled fee and the BLS 2027 dates (§4).

## 7. Next

Phase 18: manual trade tracking. The `book` / `sample` columns, observations,
Log selected / Log all, a Tracking page with entry-vs-now, P&L attribution,
Update now, the daily 15:45 chain archive, and auto-expiry.
