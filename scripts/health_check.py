r"""
Weekly health check of the scheduled jobs (data/scheduler/history.jsonl).

    .venv\Scripts\python scripts\health_check.py            # the last 7 days
    .venv\Scripts\python scripts\health_check.py --days 14

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
  1. Reuse fresh chains. tracking.update force-pulls every held ticker's chain
     (chains.capture(force=True)); skip tickers whose chain block is under ~20
     minutes old -- the 10:45 scans and the 15:30 archive have just pulled most
     of them. Cuts the chain phase, which scales with tickers.
  2. Fewer paths for the mark's probabilities-from-now (n_paths, e.g. 4000 ->
     1000): the marks phase scales with open positions.
  3. If neither is enough: mark every 2 hours (Settings -> Schedule ->
     every_minutes = 120), or mark the taken book hourly and the tracked
     book twice a day."""


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
              f"{'tickers':>7} {'marks':>6} {'positions':>9} {'s/ticker':>8} {'s/pos':>6}"]
    worst = None
    per_day: dict[str, list[dict]] = defaultdict(list)
    for e in marks:
        planned = scheduler._parse(e.get("planned"))
        phases = {k: e.get(k) for k in ("chain_s", "marks_s", "tickers", "positions")}
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
            + f" {tick if tick is not None else '--':>7} "
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
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly scheduler health check.")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    print(report(args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
