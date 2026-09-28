# Phase 19 — Scheduler and auto-logging

Written 2026-09-28 for Tom and for the next Claude Code session. The work
order is `docs/REVIEW_P8-16_AND_NEXT_PHASES.md` Part C.5 and Part E,
Phase 19, with Tom's decisions of 2026-09-28 (Phase 17 open items, Phase 18
follow-ups, changes to the Phase 19 plan), done in that order.

## 1. Decisions applied before Phase 19

| Decision (Tom, 2026-09-28) | What changed |
|---|---|
| CSP window: 7 ± 4 DTE | `management.entry` is now 3–11 DTE (was 5–10). Every weekday has a Friday inside it, so the scheduled scan always finds a weekly. The default request, the JSON fallback, the skew window and the exit-rule "outside the window" warning all read it. |
| Golden test: regenerate on purpose | Re-checked, not rewritten. The fixture's strikes are at 5 and 8 DTE, inside both windows, and every compared field is identical. The test docstring records the check. |
| Spread-leg floors: OI 100, no volume gate | Confirmed. A leg that traded fewer than `warn_option_volume` (25) contracts today now gets a row **warning** ("short leg: low volume …; not a gate"). It is never a rejection (`SizingResult.warnings`, PCS and strategy specs). |
| JSON requests inherit `scan_defaults`, with a warning | `ScanRequest.from_dict` fills missing fields from `scan_defaults` and lists them in `request.inherited`. The run records a manifest warning and the Screener shows it when loading a preset. Saved presets are written with every field (`to_dict`). |
| Auto presets fully explicit | A preset can be marked auto only if it spells out every `ScanRequest` field (`missing_fields`). A hand-edited preset that isn't explicit is refused at run time too. |

The inheritance rule changed three older tests' requests. Partial PCS requests
now pick up the 4% / 45 DTE defaults, so tests that need dollar widths or the
shared window now say `"spread_width_pct": null, "pcs_dte_targets": null`.

## 2. Phase 18 follow-ups

New `paper_positions` columns:

- **`account_profile`**, **`profile_nlv`**: the profile a row was sized
  against and its account value at log time. `run_id` (already stored) is
  the run it came from. The profile comes from the row itself, else the run's
  request (`paper.run_profile`), else `default`.
- **`sized_contracts`**: the profile-sized count, kept apart from the
  per-contract figures.
- **`dollar_pnl_valid`**: False for a tracked row sized against the research
  `default` ($3M) or a placeholder profile. Such rows still count in the
  probability and %-of-max-profit reports but are left out of dollar P&L:
  `paper.performance()` totals and annualised figures, the Decisions page
  metrics (with a note), and the Tracking page's dollar columns (blank). A
  taken trade's dollars are always valid.
- **`flags`**: data-quality tags (`paper.add_flag`, `scripts/flag_positions.py`).
- **`hold_pnl_per_contract`**, **`managed_pnl_per_contract`**, and
  `position_marks.pnl_per_contract`. Per contract means gross per contract
  less the position's fees split evenly, so it is exactly total ÷ contracts
  and compares across tickers and accounts. Existing marks and outcomes were
  backfilled.

Applied to the live book:

- All 20 existing tracked positions are tagged `default` / $3,000,000 /
  `dollar_pnl_valid = false`.
- **#1–#19 are flagged `pre_fix_bars`.** Those are the positions logged from
  this morning's runs (09:56, 12:20 and 12:36), priced before the Phase 18
  partial-bar fix. The flag was set with
  `scripts/flag_positions.py pre_fix_bars --runs-before 2026-09-28T16:00`.
  Nothing was deleted.
- #20 (the manual SPY 0-DTE expiry check, no run) is not flagged.

## 3. The scheduler (`pipeline/scheduler.py`)

A worker process outside Streamlit. `launch.py` starts it detached next to
the UI (`--no-worker` to skip); it keeps running after the UI closes.
`scripts/scheduler_task.py install` adds a Windows Task Scheduler entry that
starts it at logon. Only one worker runs at a time (`data/scheduler/worker.lock`).

| Job | When (ET, trading days) | Does |
|---|---|---|
| `mark` | hourly 9:45–15:45 (7 slots) | `tracking.update` on every open position, both books |
| `scan_and_log` | **once a day per auto preset, 10:45** (configurable) | run the preset (chains only; the nightly job refreshed the data), then `tracking.auto_log` |
| `observe` | optional per preset (`observe_hourly`), the other mark slots | run the preset; observations for already-tracked rows only, nothing opened |
| `archive` | 15:45 | full-universe chain snapshot |
| `nightly` | 18:30 | `run --data-only`, then `tracking.expire_due` |

**Auto-log rules.** `auto_log` logs the C.3 sample (top K, M random passing
rows lower down, M near-miss rejects) with these limits:

- Never a naked spec or a research-only row (`auto_eligible`).
- At most the preset's **daily cap** of new positions (default K + 2M = 11),
  top rows first.
- Rows already open only add an observation, so there are no duplicates.

**Refusals**, shown as `refused` in the job history and in a red banner on
the Screener and Tracking pages:

- the preset is gone
- the preset is not fully explicit
- the preset's account profile is still marked **placeholder**

**Timing rules:**

- **Half days:** slots at or after the close are dropped, and the archive
  moves to 15 minutes before the close (12:45 on a 13:00 close).
- **Holidays and DST:** handled by the NYSE calendar in ET.
- **Missed slots:** a slot picked up within 30 minutes still runs; later
  than that it is logged `missed`.
- **Overruns:** a slot that falls inside the same job's previous run is
  logged `skipped`. Different jobs due at the same minute run one after
  another, mark first.
- **Run lock:** every job holds the pipeline's run lock (shared with the UI).
  A held lock is retried each poll until the grace runs out.

**Why a plain loop, not APScheduler.** The review suggested APScheduler,
but the missed-slot, overrun and half-day rules are the whole of the logic
and had to be written either way. A loop over the day's slot plan keeps them
in one testable place with no new dependency.

**Visibility.** Settings has a new **Schedule** tab:

- worker status (pid, heartbeat age, state) with Start and Stop buttons
- today's slots with their status and message
- the last 7 days of job history
- the timetable editor
- the auto-preset table: auto flag, K, M, daily cap, observe hourly, whether
  the preset is fully explicit, and whether its profile is a placeholder
- the Task Scheduler instructions

The files behind it are in `data/scheduler/` (`heartbeat.json`,
`history.jsonl`, `worker.log`). For the command line:
`pipeline/scheduler.py --plan [DATE]`, `--status`, or `--run JOB [--preset]`.

## 4. Checks

- `pytest tests`: **506 passed**, including 19 new tests in `tests/test_phase19.py`.
- `scripts/check_pages.py`: 13/13 clean. `scripts/preflight.py`: no blocking
  issues.
- **Live, 2026-09-28 17:15 ET:**
  - The worker started detached and recorded today's 7 mark slots and the
    15:45 archive as `missed` (no worker existed then).
  - It planned the 18:30 nightly and tomorrow's 09:45 mark.
  - A second start returned the running worker, and a stop request ended it
    cleanly.
- **Not yet done: the review's acceptance test**, a full trading day run
  unattended (7 marks, auto-logs without duplicates, the archive at 15:45,
  every slot in the history). That needs a real session. It also needs an
  auto preset whose profile is not a placeholder: all three of your profiles
  still have **Placeholder** ticked.

## 5. Open for Tom

1. **Untick "Placeholder"** on each profile once its values are real (all
   three are still ticked). Until then every auto run is refused, and rows
   logged against them are left out of dollar P&L.
2. **Mark a preset auto** (Settings → Schedule) and leave the worker running
   through one session for the acceptance test.
3. You set `taxable` to `naked_approval: false`, so strangles no longer
   appear at all on that profile, not even as research-only rows. Say if
   that was meant.
4. Auto presets against the research `default` profile are allowed (it isn't
   a placeholder), but their rows are left out of dollar P&L. Say if you'd
   rather the scheduler refuse `default` too.
5. Still open: the $5 fee on cash-settled spreads, and the BLS 2027 dates.

## 6. Next

- After the scheduler has run for a while: move the paper-book parts of the
  Decisions page onto the Tracking page (Tom, 2026-09-28), once the job
  history shows how the pages are actually used.
- Phase 20: Outlook v1.
