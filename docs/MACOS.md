# Running on macOS

Preparation only (2026-09-29). The tool runs on Windows through the
collection period, and **nothing here changes Windows behaviour**. Read
this before a move, not during one.

## The single-machine rule

Two things may live on **one machine at a time**. Having them on two is how
data gets lost silently.

1. **The TastyTrade credential (`.env`, `REFRESH_TOKEN`).** The refresh token
   *rotates*: every refresh returns a new token, and the old one stops
   working. If two machines hold the same `.env`, the first one to refresh
   invalidates the other. The second machine then fails with "OAuth refresh
   rejected", and its scheduled marks and auto-logs fail with it. Never copy
   `.env` to a second machine while the first is still running. Move it:
   stop the first machine, copy, start the second.
2. **The trade log (`data/trade_log.duckdb`) and the other DuckDB files.**
   DuckDB allows one writer. The trade log holds every tracked and taken
   position, mark and observation, and it can't be rebuilt from anything
   else. Two machines writing their own copies fork the history, and the
   forks can't be merged. Don't put `data/` in iCloud Drive, Dropbox or
   OneDrive. A sync client can copy a file in the middle of a write, or
   restore an older copy over a newer one.

**To move** (Windows to Mac, or back):

1. On the old machine, stop the worker (Settings → Schedule → "Stop worker"; confirm with
   `python pipeline/scheduler.py --status`: "running" must be false) and close
   the UI. Do it after the 18:30 nightly job, not during trading hours.
   Remove the logon entry: `python scripts\scheduler_task.py remove`.
2. Copy the whole `data/` folder and `.env` to the new machine. Use an
   external drive or `scp`, not a sync folder. `config/` is in git.
3. On the old machine, rename `.env` to `.env.retired` so nothing there can
   refresh the token again.
4. On the new machine, run the setup below, then
   `python scripts/preflight.py`, then start the worker.

## Setup

```bash
cd ~/csp                                  # a clone of github.com/pixcination/csp
bash setup.sh                             # .venv, requirements, .env from the template, preflight
.venv/bin/python launch.py                # UI + the scheduler worker
.venv/bin/python scripts/launchd_agent.py install    # the worker at every login
```

- Python 3.11 or later (Windows runs 3.13). `setup.sh` uses `python3`, or set
  `PYTHON=/path/to/python3.13`.
- Commands use `.venv/bin/python` where the Windows docs say
  `.venv\Scripts\python`.
- `pricing_data_root` in `config.yaml` points at `D:/pricing_data`, the
  1-minute screening archive. It's optional (`pricing_data_required: false`),
  and only re-screening a new universe from the full pool needs it. On a Mac
  it resolves to nothing and is ignored.
- The vendored TastyTrade client's `D:\tastytrade` default is already
  redirected into `data/tastytrade` (core/env.py).

## The scheduler worker

| | Windows | macOS |
|---|---|---|
| Start with the UI | `python launch.py` | same |
| Start at login | `scripts\scheduler_task.py install` (Task Scheduler) | `scripts/launchd_agent.py install` (LaunchAgent `com.csp.scheduler-worker`) |
| Remove | `scheduler_task.py remove` | `launchd_agent.py remove` |
| Detached how | `pythonw`, no console, detached process | its own session (`start_new_session`), so closing the terminal doesn't stop it |

Both platforms allow one worker at a time (`data/scheduler/worker.lock`),
so the login entry and `launch.py` can coexist. Jobs log to
`data/scheduler/worker.log`, and launchd's own output goes to
`data/scheduler/launchd.log`. Slot times are US Eastern whatever the Mac's
time zone, because the plan uses the NYSE calendar in ET.

## Sleep

A sleeping Mac runs nothing. A slot picked up more than 30 minutes late is
logged `missed`, and missed marks and auto-logs can't be recovered.

- The LaunchAgent wraps the worker in `caffeinate -i`, which prevents
  **idle** sleep while the worker runs (the display may still sleep).
  `launchd_agent.py install --no-caffeinate` leaves sleep to the system.
- Closing the lid on battery still sleeps a laptop. Keep it on power, lid
  open, or use clamshell mode with an external display.
- System Settings → Battery (Energy Saver on a desktop) → Options: turn on
  "Prevent automatic sleeping on power adapter when the display is off".
- If the Mac sleeps overnight anyway, schedule a wake before the first
  slot. Times are the Mac's local time. Set it for 09:30 ET, before the
  09:45 mark:

  ```bash
  sudo pmset repeat wakeorpoweron MTWRF 09:30:00     # adjust to the Mac's time zone
  pmset -g sched                                     # check
  ```

- Check a week's result with `.venv/bin/python scripts/health_check.py`:
  missed slots are listed there.

## Tests

`pytest tests` runs on both platforms. The detached-worker tests check the
Windows flags on Windows and `start_new_session` elsewhere, and each is
skipped on the other platform.
