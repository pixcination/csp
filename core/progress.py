"""
Progress reporting for long pipeline runs.

The activation button fans out over sixty tickers and several external APIs,
some of which are rate-limited into the tens of minutes. A run that prints
nothing for twenty minutes is indistinguishable from a run that has hung, so
every stage reports through this module.

One reporter interface, three renderers:

* `ConsoleReporter`  -- a live single-line bar for terminal runs
* `StreamlitReporter` -- progress bars and status text in the app
* `NullReporter`      -- silent, for tests

Stages nest: the pipeline owns the overall bar, each stage owns its own, and
per-item work ticks the inner one. ETA is computed from observed throughput
rather than assumed, because the Massive stage runs at a fixed 5 calls/minute
while the analysis stage runs as fast as the CPU allows.
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field


def _fmt_duration(seconds: float) -> str:
    if seconds is None or seconds != seconds:  # NaN
        return "--"
    seconds = max(int(seconds), 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


@dataclass
class StageRecord:
    key: str
    label: str
    total: int = 0
    done: int = 0
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    skipped: bool = False
    note: str = ""
    error: str | None = None

    @property
    def elapsed(self) -> float:
        return (self.finished or time.monotonic()) - self.started

    @property
    def fraction(self) -> float:
        if self.total <= 0:
            return 0.0 if self.finished is None else 1.0
        return min(self.done / self.total, 1.0)

    @property
    def eta_seconds(self) -> float:
        """Remaining time from measured throughput, not from a guess."""
        if self.done <= 0 or self.total <= 0 or self.finished is not None:
            return float("nan")
        rate = self.done / max(self.elapsed, 1e-6)
        return (self.total - self.done) / max(rate, 1e-9)

    def summary(self) -> str:
        if self.error:
            return f"{self.label}: FAILED after {_fmt_duration(self.elapsed)} -- {self.error}"
        if self.skipped:
            return f"{self.label}: skipped ({self.note})" if self.note else f"{self.label}: skipped"
        detail = f" -- {self.note}" if self.note else ""
        return f"{self.label}: {_fmt_duration(self.elapsed)}{detail}"


class BaseReporter:
    """Common bookkeeping. Renderers override the _render hooks."""

    def __init__(self, stages: list[tuple[str, str]] | None = None):
        self.stage_order = stages or []
        self.records: dict[str, StageRecord] = {}
        self.current: StageRecord | None = None
        self.run_started = time.monotonic()

    # --- lifecycle --------------------------------------------------------
    @contextmanager
    def stage(self, key: str, label: str, total: int = 0):
        record = StageRecord(key=key, label=label, total=total)
        self.records[key] = record
        self.current = record
        self._on_stage_start(record)
        try:
            yield record
        except Exception as exc:
            record.error = f"{type(exc).__name__}: {exc}"
            record.finished = time.monotonic()
            self._on_stage_end(record)
            raise
        else:
            record.finished = time.monotonic()
            self._on_stage_end(record)
        finally:
            self.current = None

    def advance(self, n: int = 1, note: str = "") -> None:
        if self.current is None:
            return
        self.current.done += n
        if note:
            self.current.note = note
        self._on_advance(self.current)

    def set_total(self, total: int) -> None:
        if self.current is not None:
            self.current.total = total
            self._on_advance(self.current)

    def skip(self, note: str = "") -> None:
        if self.current is not None:
            self.current.skipped = True
            self.current.note = note

    def log(self, message: str) -> None:
        self._on_log(message)

    # --- reporting --------------------------------------------------------
    @property
    def total_elapsed(self) -> float:
        return time.monotonic() - self.run_started

    def report(self) -> str:
        lines = [f"Run completed in {_fmt_duration(self.total_elapsed)}", ""]
        order = [k for k, _ in self.stage_order] or list(self.records)
        for key in order:
            rec = self.records.get(key)
            if rec is not None:
                lines.append("  " + rec.summary())
        return "\n".join(lines)

    # --- hooks ------------------------------------------------------------
    def _on_stage_start(self, record: StageRecord) -> None: ...
    def _on_stage_end(self, record: StageRecord) -> None: ...
    def _on_advance(self, record: StageRecord) -> None: ...
    def _on_log(self, message: str) -> None: ...


class NullReporter(BaseReporter):
    """Silent. Used by tests and by any code path that must not print."""


class ConsoleReporter(BaseReporter):
    """Single-line live bar, redrawn in place. Falls back to plain lines when
    stdout is not a terminal (a log file, or the app's subprocess capture)."""

    BAR_WIDTH = 28

    def __init__(self, stages=None, stream=None):
        super().__init__(stages)
        self.stream = stream or sys.stdout
        self.interactive = bool(getattr(self.stream, "isatty", lambda: False)())
        self._last_draw = 0.0

    def _write(self, text: str, newline: bool = True) -> None:
        self.stream.write(text + ("\n" if newline else ""))
        self.stream.flush()

    def _on_stage_start(self, record):
        index = self._index(record.key)
        prefix = f"[{index}] " if index else ""
        self._write(f"{prefix}{record.label}" + (f"  (0/{record.total})" if record.total else ""))

    def _on_advance(self, record):
        if not self.interactive:
            # Non-interactive: emit a line every 10% so logs stay readable.
            if record.total and record.done % max(record.total // 10, 1) == 0:
                self._write(f"    {record.done}/{record.total}"
                            f"  eta {_fmt_duration(record.eta_seconds)}"
                            + (f"  {record.note}" if record.note else ""))
            return
        now = time.monotonic()
        if now - self._last_draw < 0.08 and record.done < record.total:
            return
        self._last_draw = now
        filled = int(self.BAR_WIDTH * record.fraction)
        bar = "#" * filled + "." * (self.BAR_WIDTH - filled)
        note = f"  {record.note}"[:44] if record.note else ""
        self._write(f"\r    [{bar}] {record.fraction:>5.0%}"
                    f"  {record.done}/{record.total}"
                    f"  eta {_fmt_duration(record.eta_seconds):>7}{note}   ",
                    newline=False)

    def _on_stage_end(self, record):
        if self.interactive:
            self._write("")
        self._write("    " + record.summary())

    def _on_log(self, message):
        self._write(f"    - {message}")

    def _index(self, key: str) -> str:
        for i, (k, _) in enumerate(self.stage_order, 1):
            if k == key:
                return f"{i}/{len(self.stage_order)}"
        return ""


class StreamlitReporter(BaseReporter):
    """Renders into a Streamlit container: an overall bar, a per-stage bar,
    a status line, and an expandable log."""

    def __init__(self, container, stages=None):
        super().__init__(stages)
        self._c = container
        self._overall = container.progress(0.0, text="Starting...")
        self._status = container.empty()
        self._stage_bar = container.progress(0.0)
        self._log_lines: list[str] = []
        self._log_box = container.empty()

    def _overall_fraction(self) -> float:
        if not self.stage_order:
            return 0.0
        done = sum(1 for k, _ in self.stage_order
                   if self.records.get(k) and self.records[k].finished)
        partial = self.current.fraction if self.current else 0.0
        return min((done + partial) / len(self.stage_order), 1.0)

    def _refresh(self, record: StageRecord | None = None):
        self._overall.progress(
            self._overall_fraction(),
            text=f"Elapsed {_fmt_duration(self.total_elapsed)}")
        if record is not None:
            self._stage_bar.progress(record.fraction)
            eta = _fmt_duration(record.eta_seconds)
            detail = f" - {record.note}" if record.note else ""
            counts = f" ({record.done}/{record.total})" if record.total else ""
            self._status.write(f"**{record.label}**{counts} - eta {eta}{detail}")

    def _on_stage_start(self, record):
        self._stage_bar.progress(0.0)
        self._refresh(record)

    def _on_advance(self, record):
        self._refresh(record)

    def _on_stage_end(self, record):
        self._log_lines.append(record.summary())
        self._log_box.code("\n".join(self._log_lines[-40:]), language="text")
        self._refresh(record)

    def _on_log(self, message):
        self._log_lines.append(f"  {message}")
        self._log_box.code("\n".join(self._log_lines[-40:]), language="text")
