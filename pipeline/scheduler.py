r"""
The scheduler worker (Phase 19, review C.5): timed jobs outside Streamlit.

Streamlit re-runs scripts on every interaction, runs one session per tab and
nothing at all when no tab is open, so the timed work lives in this worker.
`launch.py` starts it next to the UI (it keeps running when the UI closes);
`scripts/scheduler_task.py install` adds a Windows Task Scheduler entry that
starts it at logon. One worker at a time (`data/scheduler/worker.lock`).

JOBS (trading days only, times ET; config.yaml -> schedule, overridden on the
Settings page, stored in config/user_settings.yaml -> schedule)

    mark          hourly 9:45-15:45: re-mark every open position (both books)
                  -- analytics/tracking.update
    scan_and_log  once a day per auto preset, 10:45 (or the preset's own `time`,
                  to stagger them): run the preset, then
                  tracking.auto_log (the C.3 sample; never naked or
                  research-only rows; the preset's daily cap on NEW positions,
                  default K + 2M = 11)
    observe       optional, per auto preset with observe_hourly: the preset at
                  the other mark slots; observations for already-tracked rows
                  only, nothing opened
    archive       15:30: the full-universe chain snapshot (chain_archive; ~13 min,
                  so it finishes inside the session)
    nightly       18:30: `run --data-only` (bars, earnings, metrics, events,
                  Stage 1), then settle expired tracked positions

RULES
    Half days     slots at or after the close are dropped; the archive moves
                  to 30 minutes before the close (12:30 on a 13:00 close).
    Holidays/DST  the NYSE calendar in ET (core.market_calendar).
    Missed slots  asleep, powered off, or busy: a slot runs if picked up within
                  `grace_minutes` (30) of its time, else it is logged `missed`.
    Overruns      a slot that came due while the SAME job's previous run was
                  still going is `skipped`, not queued. Different jobs due at
                  the same minute run one after another (mark first).
    Run lock      every job holds pipeline.run's lock, shared with the UI; a
                  held lock is retried each poll until the grace runs out.
    Auto presets  refused (logged `refused`) when the preset is gone, is not
                  fully explicit (every ScanRequest field spelled out, so a
                  config change cannot silently change what is tracked), or
                  its account profile holds placeholder values.

VISIBILITY
    data/scheduler/heartbeat.json   pid, time, state, the next slots
    data/scheduler/history.jsonl    one line per slot: ok / failed / missed /
                                    skipped / refused, with the message, the
                                    run id and duration_s (scan_and_log also
                                    scan_s and log_s)
    data/scheduler/worker.log       the jobs' progress lines and tracebacks

Why a plain loop rather than APScheduler (the review's suggestion): the
missed-slot, overrun and half-day rules above are the whole of the logic and
had to be written either way; a loop over the day's slot plan keeps them in
one testable place without a new dependency.

    .venv\Scripts\python pipeline\scheduler.py              # serve (the worker)
    .venv\Scripts\python pipeline\scheduler.py --plan       # today's slots
    .venv\Scripts\python pipeline\scheduler.py --run mark   # one job now
    .venv\Scripts\python pipeline\scheduler.py --status
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.market_calendar import ET, _session_bounds, now_et  # noqa: E402
from core.paths import data_dir, load_config  # noqa: E402
from core.progress import BaseReporter  # noqa: E402

JOBS = ("mark", "scan_and_log", "observe", "archive", "nightly")
ORDER = {job: i for i, job in enumerate(JOBS)}
STATUSES = ("ok", "failed", "missed", "skipped", "refused")
AUTO_FIELDS = {"top_k": int, "control_m": int, "daily_cap": int, "observe_hourly": bool,
               "time": str}

log = logging.getLogger("csp.scheduler")


class Refused(Exception):
    """A job that must not run as configured (shown as `refused`)."""


# --- Settings ----------------------------------------------------------------------------

def settings() -> dict:
    """config.yaml -> schedule with the user's overrides (Settings page) on top."""
    from core import user_settings
    base = json.loads(json.dumps(load_config().get("schedule") or {}))
    user = user_settings.load().get("schedule") or {}
    for key, value in user.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    base.setdefault("enabled", True)
    base.setdefault("grace_minutes", 30)
    base.setdefault("poll_seconds", 30)
    base.setdefault("mark", {"start": "09:45", "end": "15:45", "every_minutes": 60})
    base.setdefault("scan_and_log", {"time": "10:45"})
    base.setdefault("archive", {"time": (load_config().get("archive") or {})
                                .get("time_et", "15:45")})
    base.setdefault("nightly", {"time": "18:30"})
    base.setdefault("auto_presets", {})
    return base


def auto_presets(cfg: dict | None = None) -> dict[str, dict]:
    """Auto presets with K, M, the daily cap (default K + 2M) and the log
    time (default schedule -> scan_and_log -> time) filled in."""
    cfg = cfg or settings()
    tracking = load_config().get("tracking", {}) or {}
    out = {}
    for name, auto in (cfg.get("auto_presets") or {}).items():
        auto = dict(auto or {})
        k = int(auto.get("top_k", tracking.get("top_k", 5)))
        m = int(auto.get("control_m", tracking.get("control_m", 3)))
        out[name] = {"top_k": k, "control_m": m,
                     "daily_cap": int(auto.get("daily_cap") or k + 2 * m),
                     "observe_hourly": bool(auto.get("observe_hourly", False)),
                     "time": str(auto.get("time") or cfg["scan_and_log"]["time"])}
    return out


def _hhmm(text: str) -> dt.time:
    hour, minute = str(text).strip().split(":")
    return dt.time(int(hour), int(minute))


# --- The day's plan ----------------------------------------------------------------------

@dataclass(frozen=True)
class Slot:
    job: str
    when: dt.datetime                     # ET, tz-aware
    preset: str | None = None

    @property
    def key(self) -> str:
        return f"{self.when:%Y-%m-%d}|{self.job}|{self.preset or '-'}|{self.when:%H:%M}"

    @property
    def label(self) -> str:
        return f"{self.when:%H:%M} {self.job}" + (f" ({self.preset})" if self.preset else "")


def plan_day(day: dt.date, cfg: dict | None = None) -> list[Slot]:
    """Every slot of a trading day, in run order (none on a closed day or
    when the schedule is disabled)."""
    cfg = cfg or settings()
    bounds = _session_bounds(day)
    if bounds is None or not cfg.get("enabled", True):
        return []
    open_, close = bounds

    def at(text: str) -> dt.datetime:
        return dt.datetime.combine(day, _hhmm(text), ET)

    mark = cfg["mark"]
    step = dt.timedelta(minutes=max(int(mark.get("every_minutes", 60)), 5))
    marks, t = [], at(mark["start"])
    while t <= at(mark["end"]):
        if open_ <= t < close:            # half days: nothing at or after the close
            marks.append(t)
        t += step
    slots = [Slot("mark", t) for t in marks]
    for name, auto in auto_presets(cfg).items():
        log_time = at(auto["time"])
        if open_ <= log_time < close:
            slots.append(Slot("scan_and_log", log_time, name))
        if auto["observe_hourly"]:
            slots += [Slot("observe", t, name) for t in marks if t != log_time]
    archive = min(at(cfg["archive"]["time"]), close - dt.timedelta(minutes=30))
    if archive >= open_:
        slots.append(Slot("archive", archive))
    slots.append(Slot("nightly", at(cfg["nightly"]["time"])))
    return sorted(slots, key=lambda s: (s.when, ORDER[s.job], s.preset or ""))


def _parse(stamp) -> dt.datetime | None:
    try:
        value = dt.datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo else value.replace(tzinfo=ET)


def decide(slot: Slot, now: dt.datetime, history: list[dict],
           grace_minutes: float = 30) -> tuple[str, str]:
    """("wait" | "run" | "missed" | "skipped", why) for a slot not yet in the
    history."""
    if now < slot.when:
        return "wait", ""
    for entry in history:
        if entry.get("job") != slot.job or (entry.get("preset") or None) != slot.preset:
            continue
        started, finished = _parse(entry.get("started")), _parse(entry.get("finished"))
        if started and finished and started < slot.when < finished:
            return "skipped", (f"overrun: the previous {slot.job} ran {started:%H:%M}-"
                               f"{finished:%H:%M}, past this slot")
    late = (now - slot.when).total_seconds() / 60.0
    if late > grace_minutes:
        return "missed", (f"missed: picked up {late:.0f} min late (over the {grace_minutes:g}-"
                          f"minute grace) -- the worker was off, asleep or busy")
    return "run", ""


# --- Files -------------------------------------------------------------------------------

def folder() -> Path:
    path = data_dir() / "scheduler"
    path.mkdir(parents=True, exist_ok=True)
    return path


def history(since: dt.date | None = None, limit: int | None = None) -> list[dict]:
    path = folder() / "history.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if since is not None:
            planned = _parse(entry.get("planned"))
            if planned is None or planned.date() < since:
                continue
        out.append(entry)
    return out[-limit:] if limit else out


def record(entry: dict) -> None:
    with (folder() / "history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")


def _write_json(name: str, data: dict) -> None:
    path = folder() / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(name: str) -> dict:
    try:
        return json.loads((folder() / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def heartbeat(state: str, upcoming: list[Slot], started_at: str) -> None:
    _write_json("heartbeat.json", {
        "pid": os.getpid(), "at": now_et().isoformat(timespec="seconds"), "state": state,
        "started_at": started_at,
        "next": [{"key": s.key, "label": s.label, "when": s.when.isoformat()}
                 for s in upcoming[:4]]})


# --- Jobs --------------------------------------------------------------------------------

class LogReporter(BaseReporter):
    """Progress into worker.log (a pythonw worker has no console)."""

    def _on_stage_start(self, record):
        log.info("  %s%s", record.label, f" ({record.total})" if record.total else "")

    def _on_stage_end(self, record):
        log.info("  %s: %s", record.label, record.error or f"done {record.done}")

    def _on_log(self, message):
        log.info("  %s", message)

    def _on_advance(self, record):
        # Per-item notes only when something was skipped or failed, so a
        # missing ticker is explained in the log (2026-09-29: XSP vanished).
        note = record.note or ""
        if any(w in note for w in ("no chain", "no spot", "no snapshot", "error", "failed")):
            log.info("    %s", note)


def check_auto(name: str):
    """The ScanRequest an auto preset runs, or Refused with the reason."""
    from analytics import sizing
    from analytics.scan_request import RequestError, ScanRequest, missing_fields
    from core import user_settings
    presets = user_settings.scan_presets()
    if name not in presets:
        raise Refused(f"auto preset {name!r} no longer exists (Settings -> Schedule)")
    fields = presets[name]
    missing = missing_fields(fields)
    if missing:
        raise Refused(f"preset {name!r} is not fully explicit (missing {', '.join(missing)}); "
                      f"open it on the Screener and save it again")
    try:
        request = ScanRequest.from_dict(fields, inherit=False)
    except RequestError as exc:
        raise Refused(f"preset {name!r} is invalid: {exc}") from None
    profile = sizing.account_config(request.account_profile)
    if profile.get("placeholder"):
        raise Refused(f"profile {request.account_profile!r} holds placeholder values -- enter "
                      f"the real account on Settings and clear 'Placeholder'")
    return request


def job_mark(slot: Slot, reporter) -> dict:
    from analytics import tracking
    from pipeline.run import RunLock
    with RunLock():
        new = tracking.update(source="scheduled", reporter=reporter)
    priced = int(new["mark"].notna().sum()) if not new.empty else 0
    return {"message": f"marked {len(new)} open position(s), {priced} priced"}


def _scan(slot: Slot, reporter):
    from pipeline.results import load_run
    from pipeline.run import run
    request = check_auto(slot.preset)
    manifest = run(request=request, reporter=reporter, refresh_data=False)
    results = load_run(manifest.run_id)
    sheets = [] if results is None else [
        s for s in (results.candidates, results.strategies) if s is not None and not s.empty]
    return request, manifest, sheets


def job_scan_and_log(slot: Slot, reporter) -> dict:
    from analytics import tracking
    auto = auto_presets().get(slot.preset)
    if auto is None:
        raise Refused(f"{slot.preset!r} is no longer marked auto")
    t0 = time.monotonic()
    request, manifest, sheets = _scan(slot, reporter)
    t1 = time.monotonic()
    totals = {"opened": 0, "observed": 0, "capped": 0, "excluded": 0}
    for sheet in sheets:
        out = tracking.auto_log(sheet, manifest.run_id, slot.preset, request.account_profile,
                                k=auto["top_k"], m=auto["control_m"],
                                daily_cap=auto["daily_cap"], today=slot.when.date())
        for key in totals:
            totals[key] += out[key]
    scan_s, log_s = round(t1 - t0, 1), round(time.monotonic() - t1, 1)
    return {"run_id": manifest.run_id, "scan_s": scan_s, "log_s": log_s,
            "message": (f"{request.account_profile}: opened {totals['opened']}, observed "
                        f"{totals['observed']}, over the daily cap {totals['capped']}, "
                        f"excluded {totals['excluded']} naked/research-only row(s)"
                        + ("" if sheets else "; the run produced no sheet")
                        + f" [scan {scan_s:.0f}s, log {log_s:.0f}s]")}


def job_observe(slot: Slot, reporter) -> dict:
    from analytics import tracking
    _, manifest, sheets = _scan(slot, reporter)
    seen = sum(tracking.observe_only(s, manifest.run_id, slot.preset)["observed"]
               for s in sheets)
    return {"run_id": manifest.run_id, "message": f"observed {seen} tracked position(s)"}


def job_archive(slot: Slot, reporter) -> dict:
    from data_sources import chain_archive
    from pipeline.run import RunLock
    with RunLock():
        manifest = chain_archive.archive(reporter=reporter)
    return {"message": f"archived {len(manifest['tickers'])} tickers, {manifest['rows']:,} rows, "
                       f"{manifest['bytes'] / 1e6:.1f} MB; failed "
                       f"{len(manifest.get('failed') or {})}"}


def job_nightly(slot: Slot, reporter) -> dict:
    from analytics import tracking
    from pipeline.run import run
    manifest = run(data_only=True, reporter=reporter)
    settled = tracking.expire_due(reporter=reporter)
    done = [s for s in settled if s["action"] not in ("waiting", "manual")]
    return {"run_id": manifest.run_id,
            "message": f"data refreshed ({len(manifest.warnings)} warning(s)); "
                       f"settled {len(done)} expired tracked position(s)"}


RUNNERS = {"mark": job_mark, "scan_and_log": job_scan_and_log, "observe": job_observe,
           "archive": job_archive, "nightly": job_nightly}


def execute(slot: Slot, manual: bool = False) -> dict | None:
    """Run one slot and record it. None when the run lock is held (the
    caller retries until the grace runs out)."""
    from pipeline.run import RunLocked
    started, t0 = now_et(), time.monotonic()
    entry = {"key": ("manual|" if manual else "") + slot.key, "job": slot.job,
             "preset": slot.preset, "planned": slot.when.isoformat(),
             "started": started.isoformat(timespec="seconds")}
    log.info("%s: starting", slot.label)
    try:
        out = RUNNERS[slot.job](slot, LogReporter())
        entry.update(status="ok", message=out.get("message", ""), run_id=out.get("run_id"),
                     **{k: out[k] for k in ("scan_s", "log_s") if k in out})
    except RunLocked as exc:
        log.info("%s: run lock held (%s); will retry", slot.label, exc)
        return None
    except Refused as exc:
        entry.update(status="refused", message=str(exc))
    except Exception as exc:
        log.error("%s failed:\n%s", slot.label, traceback.format_exc())
        entry.update(status="failed", message=f"{type(exc).__name__}: {exc}")
    entry["finished"] = now_et().isoformat(timespec="seconds")
    entry["duration_s"] = round(time.monotonic() - t0, 1)
    record(entry)
    log.info("%s: %s in %.0fs -- %s", slot.label, entry["status"], entry["duration_s"],
             entry["message"])
    return entry


# --- The worker --------------------------------------------------------------------------

def _pid_alive(pid) -> bool:
    from pipeline.run import _pid_alive as alive
    try:
        return bool(pid) and alive(int(pid))
    except (TypeError, ValueError):
        return False


def worker_pid() -> int | None:
    """The running worker's pid, or None."""
    pid = _read_json("worker.lock").get("pid")
    return int(pid) if _pid_alive(pid) else None


def request_stop() -> None:
    (folder() / "stop").write_text(now_et().isoformat(), encoding="utf-8")


def tick(now: dt.datetime, cfg: dict, lock_notes: dict) -> Slot | None:
    """Record today's missed/skipped slots and return the next one to run."""
    grace = float(cfg.get("grace_minutes", 30))
    hist = history(since=now.date())
    done = {h["key"] for h in hist}
    for slot in plan_day(now.date(), cfg):
        if slot.key in done:
            continue
        verdict, why = decide(slot, now, hist, grace)
        if verdict == "wait":
            continue
        if verdict in ("missed", "skipped"):
            note = lock_notes.pop(slot.key, None)
            record({"key": slot.key, "job": slot.job, "preset": slot.preset,
                    "planned": slot.when.isoformat(), "status": verdict,
                    "message": why + (f"; run lock held: {note}" if note else "")})
            hist.append({"key": slot.key})
            continue
        return slot
    return None


def serve() -> int:
    logging.basicConfig(filename=str(folder() / "worker.log"), level=logging.INFO,
                        format="%(asctime)s %(message)s")
    other = worker_pid()
    if other and other != os.getpid():
        log.info("another worker is running (pid %s); exiting", other)
        return 1
    started_at = now_et().isoformat(timespec="seconds")
    _write_json("worker.lock", {"pid": os.getpid(), "started_at": started_at})
    (folder() / "stop").unlink(missing_ok=True)
    log.info("worker started (pid %s)", os.getpid())
    lock_notes: dict[str, str] = {}
    try:
        while True:
            if (folder() / "stop").exists():
                (folder() / "stop").unlink(missing_ok=True)
                log.info("stop requested")
                break
            cfg = settings()
            now = now_et()
            slot = tick(now, cfg, lock_notes)
            upcoming = [s for s in plan_day(now.date(), cfg) if s.when > now] \
                + plan_day(now.date() + dt.timedelta(days=1), cfg)
            if slot is not None:
                heartbeat(f"running {slot.label}", upcoming, started_at)
                if execute(slot) is None:
                    lock_notes[slot.key] = "the UI or another job holds it"
                else:
                    continue                # time moved on: re-plan at once
            heartbeat("idle", upcoming, started_at)
            time.sleep(max(int(cfg.get("poll_seconds", 30)), 5))
    finally:
        heartbeat("stopped", [], started_at)
        if _read_json("worker.lock").get("pid") == os.getpid():
            (folder() / "worker.lock").unlink(missing_ok=True)
        log.info("worker stopped")
    return 0


def start_worker() -> int | None:
    """Start a detached worker unless one is running; returns its pid."""
    running = worker_pid()
    if running:
        return running
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    flags = 0
    if sys.platform == "win32":
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                 | subprocess.CREATE_NO_WINDOW)
    proc = subprocess.Popen(
        [str(windowless if windowless.exists() else exe), str(ROOT / "pipeline" / "scheduler.py")],
        cwd=str(ROOT), creationflags=flags, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    return proc.pid


# --- Status for the UI -------------------------------------------------------------------

def status(now: dt.datetime | None = None) -> dict:
    """Everything the Settings page and the alert banner show."""
    now = now or now_et()
    cfg = settings()
    beat = _read_json("heartbeat.json")
    pid = worker_pid()
    today = history(since=now.date())
    last_by_job: dict[str, dict] = {}
    for entry in history(since=now.date() - dt.timedelta(days=7)):
        last_by_job[entry["job"] + (f" ({entry['preset']})" if entry.get("preset") else "")] = entry
    plan = plan_day(now.date(), cfg)
    session = _session_bounds(now.date())
    in_hours = bool(session and plan and plan[0].when - dt.timedelta(minutes=30)
                    <= now <= plan[-1].when + dt.timedelta(minutes=30))
    return {"enabled": bool(cfg.get("enabled", True)), "running": pid is not None, "pid": pid,
            "heartbeat": beat, "plan": plan, "today": today, "last_by_job": last_by_job,
            "failures_today": [h for h in today if h.get("status") in ("failed", "refused")],
            "missed_today": [h for h in today if h.get("status") in ("missed", "skipped")],
            "should_be_running": bool(cfg.get("enabled", True)) and in_hours}


def main() -> int:
    ap = argparse.ArgumentParser(description="The CSP scheduler worker (Phase 19).")
    ap.add_argument("--plan", nargs="?", const="today", default=None,
                    help="print a day's slots (YYYY-MM-DD, default today) and exit")
    ap.add_argument("--run", choices=JOBS, default=None, help="run one job now and exit")
    ap.add_argument("--preset", default=None, help="the auto preset for --run scan_and_log/observe")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.plan:
        day = now_et().date() if args.plan == "today" else dt.date.fromisoformat(args.plan)
        slots = plan_day(day)
        print("\n".join(f"{s.key}" for s in slots) or f"{day}: no slots (closed or disabled)")
        return 0
    if args.status:
        info = status()
        print(json.dumps({k: v for k, v in info.items() if k != "plan"}, indent=2, default=str))
        return 0
    if args.run:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        entry = execute(Slot(args.run, now_et().replace(second=0, microsecond=0), args.preset),
                        manual=True)
        print(entry or "run lock held -- try again when the other run finishes")
        return 0 if entry and entry["status"] == "ok" else 1
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
