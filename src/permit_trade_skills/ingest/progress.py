"""Lightweight progress counter for long-running sync phases.

On a terminal it redraws one line in place (throttled); when output is piped
(CI logs, `| tail`) it prints a line at every 10% so logs stay short. Writes to
stderr so the sync report on stdout stays clean.
"""

from __future__ import annotations

import sys
import time
from typing import Optional, TextIO


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


class Progress:
    def __init__(self, label: str, total: int, stream: Optional[TextIO] = None,
                 interactive: Optional[bool] = None, min_interval: float = 0.25):
        self.label = label
        self.total = max(total, 0)
        self.stream = stream or sys.stderr
        self.interactive = self.stream.isatty() if interactive is None else interactive
        self.min_interval = min_interval
        self.done = 0
        self.start = time.monotonic()
        self._last_draw = 0.0
        self._last_decile = 0  # piped mode: first line at 10%
        self._closed = False
        # Keep ordering with phase messages printed to stdout (buffered when piped).
        sys.stdout.flush()

    def line(self) -> str:
        elapsed = time.monotonic() - self.start
        rate = self.done / elapsed if elapsed > 0 else 0.0
        pct = (100.0 * self.done / self.total) if self.total else 100.0
        text = f"  {self.label}: {self.done:,} / {self.total:,} ({pct:.1f}%)"
        if self.done and rate:
            text += f" | {rate:,.0f}/s"
            if self.done < self.total:
                text += f" | ETA {_fmt_duration((self.total - self.done) / rate)}"
            else:
                text += f" | {_fmt_duration(elapsed)}"
        return text

    def update(self, done: int) -> None:
        self.done = min(done, self.total) if self.total else done
        now = time.monotonic()
        if self.interactive:
            if now - self._last_draw >= self.min_interval or self.done >= self.total:
                self.stream.write("\r\033[K" + self.line())
                self.stream.flush()
                self._last_draw = now
        else:
            decile = int(10 * self.done / self.total) if self.total else 10
            if decile > self._last_decile and decile < 10:
                self._last_decile = decile
                self.stream.write(self.line() + "\n")
                self.stream.flush()

    def advance(self, n: int = 1) -> None:
        self.update(self.done + n)

    def close(self) -> None:
        """Print the final state once (always ends with a newline)."""
        if self._closed:
            return
        self._closed = True
        if self.interactive:
            self.stream.write("\r\033[K" + self.line() + "\n")
        else:
            self.stream.write(self.line() + "\n")
        self.stream.flush()
