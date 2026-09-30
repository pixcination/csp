r"""
Weekly health check of the scheduled jobs (data/scheduler/history.jsonl).

    .venv\Scripts\python scripts\health_check.py            # the last 7 days
    .venv\Scripts\python scripts\health_check.py --days 14
    .venv\Scripts\python scripts\health_check.py --save    # also data/scheduler/health/<date>.txt

The Windows task "CSP weekly health check" runs it with --save every Saturday
(`scripts/health_check.py --install-task`).

Reports, per job (and auto preset): slots by status, median and longest
duration; every slot that did not finish `ok`; every job over 15 minutes;
the auto-logs' positions; the archive's size; Symbol Lookups and their time
against the 90 s target; and the MARK job in detail -- per day, its duration
split into chain pulls (scale with tickers) and marks (scale with open
positions). A mark over 15 minutes gets a proposed fix, before it pushes
into the next hourly slot (the 10:45 mark into the 11:45 one).

Marks recorded before the phase split was stored (2026-09-29) are split
from worker.log's stage timestamps instead.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.market_calendar import now_et  # noqa: E402
from pipeline import scheduler  # noqa: E402

LONG_JOB_MIN = 15.0
MARK_WARN_MIN = 10.0

MARK_FIX = """\
Proposed fix (mark over {limit:.0f} min: {why}):
  (Done 2026-09-30: a mark reuses snapshots under 20 minutes old, and the
  15:45 mark reads the 15:30 archive -- see the `reused` column.)
  1. Fewer paths for the mark's probabilities-from-now (n_paths, e.g. 4000 ->
     1000): the marks phase scales with open positions.
  2. Run the 10:45 mark after the scans, so it reuses their chains (today it
     runs first and pulls every held ticker itself).
  3. If neither is enough: mark every 2 hours (Settings -> Schedule ->
     every_minutes = 120), or mark the taken book hourly and the tracked
     book twice a day."""

# Days the scheduler has not yet handled live (Tom 2026-09-29). Each is flagged
# ahead of time, then checked against history.jsonl once it has passed.
WATCH_AHEAD_DAYS = 14
FIRSTS = [
    (dt.date(2026, 11, 2), "DST ends Sun Nov 1: first trading day on EST (UTC-5)"),
    (dt.date(2026, 11, 26), "Thanksgiving: market closed, no slots"),
    (dt.date(2026, 11, 27), "Half day (13:00 close): archive 12:30, last mark 12:45"),
    (dt.date(2026, 12, 24), "Half day (13:00 close): archive 12:30, last mark 12:45"),
    (dt.date(2026, 12, 25), "Christmas: market closed, no slots"),
]


def _minutes(entry: dict) -> float | None:
    value = entry.get("duration_s")
    if value is None:
        start, end = scheduler._parse(entry.get("started")), scheduler._parse(entry.get("finished"))
        if not (start and end):
            return None
        value = (end - start).total_seconds()
    return float(value) / 60.0


def log_phases() -> dict[str, dict]:
    """{planned-iso: {chain_s, marks_s, tickers, positions}} parsed from worker.log."""
    path = scheduler.folder() / "worker.log"
    if not path.exists():
        return {}
    stamp = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+"
    out, current, marks = {}, None, {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(stamp + r" (\d{2}:\d{2}) mark: starting", line)
        if m:
            current = f"{m.group(1)[:10]}T{m.group(2)}"
            marks = {}
            continue
        if current is None:
            continue
        m = re.match(stamp + r"\s+(Chains for open positions|Marks and probabilities)"
                             r"(?: \((\d+)\)|: done)", line)
        if m:
            when = dt.datetime.fromisoformat(m.group(1))
            name = "chain" if m.group(2).startswith("Chains") else "marks"
            if m.group(3):
                marks[f"{name}_start"], marks[f"{name}_n"] = when, int(m.group(3))
            elif f"{name}_start" in marks:
                marks[f"{name}_s"] = (when - marks[f"{name}_start"]).total_seconds()
            if "marks_s" in marks:
                out[current] = {"chain_s": marks.get("chain_s"), "marks_s": marks["marks_s"],
                                "tickers": marks.get("chain_n"), "positions": marks.get("marks_n")}
                current = None
    return out


def _day_result(day: dt.date) -> str:
    """How the scheduler handled a past day: every planned slot recorded
    and ok (none on a closed day), and the marks on time."""
    got = {e["key"]: e for e in scheduler.history(since=day)
           if (scheduler._parse(e.get("planned")) or now_et()).date() == day}
    planned = scheduler.plan_day(day)
    if not planned:
        return "ok: closed, no slots ran" if not got else \
            f"PROBLEM: closed day but {len(got)} slot(s) recorded"
    missing = [s.label for s in planned if s.key not in got]
    bad = [k for k, e in got.items() if e.get("status") != "ok"]
    late = []
    for s in planned:
        started = scheduler._parse((got.get(s.key) or {}).get("started"))
        if s.job == "mark" and started and (started - s.when).total_seconds() > 600:
            late.append(f"{s.label} started {started:%H:%M}")
    if not (missing or bad or late):
        return f"ok: {len(planned)} slot(s), all ok and on time"
    return "PROBLEM: " + "; ".join(
        ([f"not recorded: {', '.join(missing)}"] if missing else [])
        + ([f"not ok: {', '.join(bad)}"] if bad else [])
        + ([f"late: {', '.join(late)}"] if late else []))


def _first_45dte_expiry() -> tuple[dt.date | None, str]:
    """The earliest expiration among positions opened at 30+ DTE, and how
    the book stands: anything past its expiration still open is a failure."""
    import pandas as pd
    from analytics import paper
    from core.freshness import last_completed_session
    frame = paper.list_positions()
    if frame.empty:
        return None, "no positions"
    exp, entry = pd.to_datetime(frame["expiration"]).dt.date, \
        pd.to_datetime(frame["entry_date"]).dt.date
    long_dated = frame[[(e - n).days >= 30 for e, n in zip(exp, entry)]]
    if long_dated.empty:
        return None, "no 30+ DTE positions yet"
    exps = pd.to_datetime(long_dated["expiration"]).dt.date
    done = last_completed_session()
    overdue = long_dated[(long_dated["status"] == "open") & (exps < done)]
    first = min(exps)
    settled = long_dated[(long_dated["status"] != "open")]
    state = (f"PROBLEM: {len(overdue)} position(s) past expiration still open "
             f"(#{', #'.join(str(i) for i in overdue['id'].head(8))})" if len(overdue)
             else f"ok: {len(settled)} settled" if len(settled) else "none settled yet")
    return first, state


def _earnings_gate(now: dt.datetime) -> list[str]:
    """When the weekly auto-logs first lose tickers to the October earnings
    gate: forecast from the earnings calendar, then per auto-log run the
    tickers the gate removed."""
    import pandas as pd
    from core.paths import load_universe
    from data_sources.yfinance_sync import load_earnings
    from pipeline import results
    out, met = [], set()
    presets = scheduler.auto_presets(scheduler.settings())
    today = now.date()
    runs = [e for e in scheduler.history(since=dt.date(today.year, 9, 29))
            if e["job"] == "scan_and_log" and e.get("run_id")]
    for e in runs:
        if e.get("preset") in met:
            continue
        try:
            cand = results.load_run(e["run_id"]).candidates
        except Exception:
            continue
        if cand is None or cand.empty or "rejections" not in cand:
            continue
        hit = cand[cand["rejections"].astype(str).str.contains(r"earnings 20\d\d-10-")]
        if hit.empty:
            continue
        met.add(e.get("preset"))
        out.append(f"  {e.get('preset')}: met on {e['key'][:10]} ({hit['ticker'].nunique()} "
                   f"ticker(s) removed for October reports, {int(cand['accepted'].sum())} "
                   f"accepted, slot {e.get('status')})")
    try:
        cal = load_earnings()
        cal = cal[cal["ticker"].isin(set(load_universe(scope="all")))]
        cal_dates = pd.to_datetime(cal["earnings_date"]).dt.date
        october = cal[[d.month == 10 and d.year == today.year and d >= today
                       for d in cal_dates]]
    except Exception:
        october = pd.DataFrame()
    if not october.empty:
        first = min(pd.to_datetime(october["earnings_date"]).dt.date)
        names = sorted(october.loc[pd.to_datetime(october["earnings_date"]).dt.date == first,
                                   "ticker"])
        from core import user_settings
        saved = user_settings.scan_presets()
        # Single-stock weeklies expire on Fridays: the gate first applies when the
        # Friday after the report comes inside the preset's DTE window.
        friday = first + dt.timedelta(days=(4 - first.weekday()) % 7)
        for name in presets:
            dte_max = int((saved.get(name) or {}).get("dte_max") or 0)
            if name in met or not 0 < dte_max < 30:
                continue
            bites = friday - dt.timedelta(days=dte_max)
            out.append(f"  {name}: not met yet; expected from {bites:%a %b %d} "
                       f"({', '.join(names)} report {first:%b %d}, the {friday:%b %d} "
                       f"expiry enters the {dte_max}-DTE window). Far fewer stock "
                       f"candidates for a few weeks is expected, not a fault.")
    return out or ["  nothing to report (earnings calendar empty)"]


def watch_list(now: dt.datetime) -> list[str]:
    today = now.date()
    lines = ["", "Firsts the scheduler has not handled live yet:"]
    items = list(FIRSTS)
    first, state = _first_45dte_expiry()
    if first:
        items.append((first, f"First 45-DTE expiry settles (nightly expire_due): {state}"))
    for day, what in sorted(items):
        ahead = (day - today).days
        if ahead > WATCH_AHEAD_DAYS:
            lines.append(f"  {day:%a %b %d}  in {ahead} days      {what}")
        elif ahead >= 0:
            lines.append(f"  {day:%a %b %d}  COMING UP ({ahead}d)  {what}")
        else:
            lines.append(f"  {day:%a %b %d}  passed          {what} -> {_day_result(day)}")
    lines += ["", "October earnings gate (auto-logs):"] + _earnings_gate(now)
    return lines


def report(days: int = 7, now: dt.datetime | None = None) -> str:
    now = now or now_et()
    since = now.date() - dt.timedelta(days=days - 1)
    entries = scheduler.history(since=since)
    lines = [f"Scheduler health check: {since} to {now.date()} ({len(entries)} slot(s))", ""]
    if not entries:
        return "\n".join(lines + ["No history in the window."])

    by_job: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_job[e["job"] + (f" ({e['preset']})" if e.get("preset") else "")].append(e)
    lines.append(f"{'job':44} {'ok':>4} {'other':>6} {'median':>8} {'longest':>8}")
    for name in sorted(by_job, key=lambda n: (scheduler.ORDER.get(n.split()[0], 9), n)):
        group = by_job[name]
        mins = [m for m in (_minutes(e) for e in group) if m is not None]
        ok = sum(e.get("status") == "ok" for e in group)
        lines.append(f"{name:44} {ok:>4} {len(group) - ok:>6} "
                     + (f"{statistics.median(mins):>7.1f}m {max(mins):>7.1f}m" if mins
                        else f"{'--':>8} {'--':>8}"))

    bad = [e for e in entries if e.get("status") != "ok"]
    lines += ["", f"Not ok: {len(bad)}"]
    lines += [f"  {e['key']}: {e.get('status')} -- {str(e.get('message', ''))[:110]}"
              for e in bad]

    long = [(e, _minutes(e)) for e in entries]
    long = [(e, m) for e, m in long if m is not None and m > LONG_JOB_MIN]
    lines += ["", f"Jobs over {LONG_JOB_MIN:.0f} minutes: {len(long)}"]
    lines += [f"  {e['key']}: {m:.1f} min" for e, m in long]

    # --- The mark job ---------------------------------------------------------------
    parsed = log_phases()
    marks = [e for e in entries if e["job"] == "mark" and e.get("status") == "ok"]
    lines += ["", "Mark job (chains scale with tickers, marks with open positions):",
              f"  {'day':10} {'runs':>4} {'median':>7} {'longest':>8} {'chains':>7} "
              f"{'pulled':>7} {'reused':>6} {'marks':>6} {'positions':>9} {'s/ticker':>8} "
              f"{'s/pos':>6}"]
    worst = None
    per_day: dict[str, list[dict]] = defaultdict(list)
    for e in marks:
        planned = scheduler._parse(e.get("planned"))
        phases = {k: e.get(k) for k in ("chain_s", "marks_s", "tickers", "reused",
                                         "positions")}
        if phases["chain_s"] is None and planned is not None:
            phases.update(parsed.get(f"{planned:%Y-%m-%dT%H:%M}", {}))
        if phases.get("positions") is None:
            m = re.search(r"marked (\d+) open", str(e.get("message", "")))
            phases["positions"] = int(m.group(1)) if m else None
        per_day[str(planned.date()) if planned else "?"].append({**e, **phases,
                                                                 "minutes": _minutes(e)})
    for day in sorted(per_day):
        rows = per_day[day]
        mins = [r["minutes"] for r in rows if r["minutes"] is not None]
        last = max(rows, key=lambda r: r["minutes"] or 0)
        chain, mk = last.get("chain_s"), last.get("marks_s")
        tick, pos = last.get("tickers"), last.get("positions")
        lines.append(
            f"  {day:10} {len(rows):>4} {statistics.median(mins):>6.1f}m {max(mins):>7.1f}m "
            + (f"{chain:>6.0f}s" if chain is not None else f"{'--':>7}")
            + f" {tick if tick is not None else '--':>7}"
            + f" {last.get('reused') if last.get('reused') is not None else '--':>6} "
            + (f"{mk:>5.0f}s" if mk is not None else f"{'--':>6}")
            + f" {pos if pos is not None else '--':>9} "
            + (f"{chain / tick:>8.1f}" if chain and tick else f"{'--':>8}")
            + (f" {mk / pos:>6.2f}" if mk and pos else f" {'--':>6}"))
        if worst is None or max(mins) > worst[1]:
            worst = (day, max(mins), last)
    if worst:
        day, top, last = worst
        if top > LONG_JOB_MIN:
            lines += ["", MARK_FIX.format(limit=LONG_JOB_MIN,
                                          why=f"{top:.1f} min on {day}")]
        elif top > MARK_WARN_MIN:
            lines += ["", f"  Watch: the longest mark took {top:.1f} min ({day}); the fix "
                          f"below applies at {LONG_JOB_MIN:.0f}.", "",
                      MARK_FIX.format(limit=LONG_JOB_MIN, why="not yet reached")]
        else:
            lines.append(f"  Longest mark {top:.1f} min: inside the {LONG_JOB_MIN:.0f}-minute "
                         f"limit.")

    # --- Auto-logs, archive, lookups ------------------------------------------------
    logs = [e for e in entries if e["job"] == "scan_and_log" and e.get("status") == "ok"]
    if logs:
        lines += ["", "Auto-logs:"]
        for e in logs:
            m = re.search(r"opened (\d+), observed (\d+), over the daily cap (\d+)",
                          str(e.get("message", "")))
            lines.append(f"  {e['key']}: " + (f"opened {m.group(1)}, observed {m.group(2)}, "
                                               f"capped {m.group(3)}" if m else e["message"])
                         + (f" ({_minutes(e):.1f} min)" if _minutes(e) else ""))
    archives = [e for e in entries if e["job"] == "archive" and e.get("status") == "ok"]
    if archives:
        lines += ["", "Archive:"] + [f"  {e['key']}: {e.get('message')}" for e in archives]
    try:
        from pipeline import lookup
        done = [r for r in lookup.recent(limit=200)
                if r["finished_at"] and r["finished_at"][:10] >= str(since)]
    except Exception:
        done = []
    if done:
        secs = [r["seconds"] or 0 for r in done]
        over = [r for r in done if (r["seconds"] or 0) > lookup.TARGET_SECONDS]
        lines += ["", f"Symbol Lookups: {len(done)}, median {statistics.median(secs):.0f}s, "
                      f"longest {max(secs):.0f}s; over the {lookup.TARGET_SECONDS}s target: "
                      f"{len(over)}"]
    lines += watch_list(now)
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly scheduler health check.")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--save", action="store_true",
                    help="also write data/scheduler/health/<date>.txt")
    ap.add_argument("--install-task", action="store_true",
                    help="create the Windows task that runs --save every Saturday 10:00")
    args = ap.parse_args()
    if args.install_task:
        import subprocess
        command = (f'"{ROOT / ".venv" / "Scripts" / "pythonw.exe"}" '
                   f'"{ROOT / "scripts" / "health_check.py"}" --save')
        done = subprocess.run(["schtasks", "/Create", "/F", "/TN", "CSP weekly health check",
                               "/SC", "WEEKLY", "/D", "SAT", "/ST", "10:00", "/RL", "LIMITED",
                               "/TR", command], capture_output=True, text=True)
        print((done.stdout or done.stderr).strip())
        return done.returncode
    text = report(args.days)
    if args.save:
        folder = scheduler.folder() / "health"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{now_et().date()}.txt").write_text(text + "\n", encoding="utf-8")
    if sys.stdout is not None:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
