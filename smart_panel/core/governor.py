"""Load shedding on a shared host: the governor (T050, FR-048).

The runner runs niced (``os.nice(5)``) so the agent sharing the Pi stays
responsive. On top of that a governor watches the frame loop and, when frames
keep missing their budget, it steps the frame rate down a fixed ladder
(``15, 12, 10, 8, 5`` fps) and scales the scheduler's source-refresh intervals
up (doubling per step, capped at ``4x``). When the panel has had headroom for
60 s it steps one rung back up. Every change is logged and surfaced in
``status`` through the reported level (``research.md`` decision 9).

The governor is pure logic with an injected clock and an injected
``cpu_reader``, so it is unit-tested with a fake clock and without touching the
host's real process niceness (``nice`` and ``cpu_reader`` are injectable in
tests — never a real ``os.nice``/``psutil`` call there).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from typing import Any

from smart_panel.sdk.context import SystemClock

__all__ = [
    "FPS_LADDER",
    "HEADROOM_S",
    "MAX_MULTIPLIER",
    "OVERRUN_WINDOWS",
    "WINDOW_S",
    "Governor",
]

# The frame-rate ladder the governor steps down through (research.md decision 9).
FPS_LADDER: tuple[int, ...] = (15, 12, 10, 8, 5)
# One measurement window; frame time and process CPU are sampled every window.
WINDOW_S = 5.0
# Consecutive overrun windows before one step down.
OVERRUN_WINDOWS = 3
# Continuous good time before one step back up.
HEADROOM_S = 60.0
# Source-refresh intervals never stretch past this multiple (research decision 9).
MAX_MULTIPLIER = 4.0
# Niceness applied once, at start.
DEFAULT_NICE = 5
# Process CPU above this percentage counts as an overrun window.
DEFAULT_CPU_THRESHOLD = 90.0


def _read_cpu_percent() -> float | None:
    """Process CPU percentage since the previous call, or ``None``.

    ``psutil`` is an optional application-layer dependency (research decision
    12), so it is imported lazily; a host without it simply reports no CPU.
    """
    try:
        import psutil  # noqa: PLC0415 - optional dependency, imported on demand
    except Exception:  # pragma: no cover - exercised only without the app extra
        return None
    try:
        return float(psutil.Process().cpu_percent(interval=None))
    except Exception:  # pragma: no cover - defensive
        return None


def _ladder_for(target_fps: int | None) -> tuple[int, ...]:
    """The frame-rate ladder for a configured target.

    The standard ladder is ``15, 12, 10, 8, 5`` (research decision 9). When the
    operator configures a *higher* target (``display.fps`` may reach 30) that
    target becomes the rung the governor starts on and the standard rungs below
    it follow, so the configured rate is never silently ignored.
    """
    if target_fps is None:
        return FPS_LADDER
    target = int(target_fps)
    if target <= 0:
        return FPS_LADDER
    return (target,) + tuple(fps for fps in FPS_LADDER if fps < target)


class Governor:
    """Step the frame rate and source intervals to shed load (T050).

    Args:
        scheduler: The :class:`~smart_panel.core.scheduler.Scheduler` whose
            interval multiplier the governor raises; ``None`` for a governor
            that only tracks the level.
        target_fps: The configured target frame rate; the starting rung is the
            highest ladder entry at or below it (defaults to the top, 15).
        clock: Monotonic clock; injectable for tests.
        log: Optional structured logger (``logging.RunnerLog``-like).
        nice: Callable applied once at :meth:`start` (defaults to
            ``os.nice``); tests inject a recorder so no real renice happens.
        nice_amount: Niceness increment; default 5.
        cpu_reader: Callable returning process CPU percent (or ``None``);
            defaults to the lazily-imported ``psutil`` reader.
        cpu_threshold: Process CPU percentage at or above which a window counts
            as an overrun.
        ladder: The frame-rate ladder; defaults to :data:`FPS_LADDER`.
    """

    def __init__(
        self,
        scheduler: Any = None,
        *,
        target_fps: int | None = None,
        clock: Any = None,
        log: Any = None,
        nice: Callable[[int], Any] | None = None,
        nice_amount: int = DEFAULT_NICE,
        cpu_reader: Callable[[], float | None] | None = None,
        cpu_threshold: float = DEFAULT_CPU_THRESHOLD,
        ladder: Sequence[int] = FPS_LADDER,
    ) -> None:
        self._scheduler = scheduler
        self._clock = clock if clock is not None else SystemClock()
        self._log = log
        self._nice = nice if nice is not None else os.nice
        self._nice_amount = int(nice_amount)
        self._cpu_reader = cpu_reader if cpu_reader is not None else _read_cpu_percent
        self._cpu_threshold = float(cpu_threshold)

        self._ladder = _ladder_for(target_fps) if ladder is FPS_LADDER else tuple(
            int(fps) for fps in ladder
        )
        if not self._ladder:
            raise ValueError("the frame-rate ladder must not be empty")
        self._level = self._level_for(target_fps)
        self._overrun_streak = 0
        self._headroom_since: float | None = None
        self._window_start = self._clock.monotonic()
        self._window_total_s = 0.0
        self._window_frames = 0
        self._last_cpu: float | None = None
        self._last_frame_time: float | None = None
        self._started = False

    # ------------------------------------------------------------------ state

    @property
    def level(self) -> int:
        """Current rung on the ladder (0 is the configured, unthrottled rate)."""
        return self._level

    @property
    def target_fps(self) -> int:
        """The frame rate currently being driven."""
        return self._ladder[self._level]

    @property
    def multiplier(self) -> float:
        """The scheduler interval multiplier for the current rung (capped at 4x)."""
        return min(2.0**self._level, MAX_MULTIPLIER)

    def status(self) -> dict[str, Any]:
        """A JSON-compatible snapshot for ``runner.status``."""
        return {
            "level": self._level,
            "target_fps": self.target_fps,
            "multiplier": self.multiplier,
            "cpu_percent": self._last_cpu,
            "frame_time_s": self._last_frame_time,
        }

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Apply the process niceness once (idempotent)."""
        if self._started:
            return
        self._nice(self._nice_amount)
        self._started = True

    def retarget(self, target_fps: int | None) -> int:
        """Rebuild the ladder for a new configured target and reset to the top rung.

        Called when ``display.fps`` changes live, so the configured rate is
        honoured; a throttled governor returns to the unthrottled rung.
        """
        self._ladder = _ladder_for(target_fps)
        self._level = 0
        self._overrun_streak = 0
        self._headroom_since = None
        self._window_total_s = 0.0
        self._window_frames = 0
        self._apply()
        return self._level

    def record_frame(self, duration_s: float) -> None:
        """Record the duration of one composed-and-sent frame."""
        seconds = float(duration_s)
        self._window_total_s += seconds
        self._window_frames += 1
        self._last_frame_time = seconds

    def tick(self, now: float | None = None) -> int:
        """Close the current window if it has elapsed; return the new level.

        Only steps the ladder once per elapsed window, so it is driven from the
        runner's housekeeping loop (every few hundred ms) but the decision is
        made every :data:`WINDOW_S` seconds.
        """
        current = self._clock.monotonic() if now is None else float(now)
        if current - self._window_start < WINDOW_S:
            return self._level

        frames = self._window_frames
        average_frame_s = self._window_total_s / frames if frames else 0.0
        window_start, self._window_start = self._window_start, current
        self._window_total_s = 0.0
        self._window_frames = 0
        self._last_cpu = self._cpu_reader()

        if self._is_overrun(average_frame_s, self._last_cpu):
            self._headroom_since = None
            self._overrun_streak += 1
            if self._overrun_streak >= OVERRUN_WINDOWS:
                self._overrun_streak = 0
                self._step_down()
        else:
            self._overrun_streak = 0
            headroom_since = self._headroom_since
            if headroom_since is None:
                headroom_since = window_start
                self._headroom_since = headroom_since
            if current - headroom_since >= HEADROOM_S:
                self._headroom_since = current
                self._step_up()
        return self._level

    # --------------------------------------------------------------- internals

    def _is_overrun(self, average_frame_s: float, cpu_percent: float | None) -> bool:
        if average_frame_s > 1.0 / self.target_fps:
            return True
        return cpu_percent is not None and cpu_percent > self._cpu_threshold

    def _step_down(self) -> None:
        if self._level < len(self._ladder) - 1:
            self._level += 1
            reason = "overrun"
        else:
            reason = "overrun_floor"
        self._apply()
        self._log_change(reason)

    def _step_up(self) -> None:
        if self._level <= 0:
            return
        self._level -= 1
        self._apply()
        self._log_change("headroom")

    def _apply(self) -> None:
        if self._scheduler is not None:
            self._scheduler.set_interval_multiplier(self.multiplier)

    def _log_change(self, reason: str) -> None:
        if self._log is None:
            return
        self._log.info(
            "governor_step",
            level=self._level,
            fps=self.target_fps,
            multiplier=self.multiplier,
            reason=reason,
        )

    def _level_for(self, target_fps: int | None) -> int:
        if target_fps is None:
            return 0
        for index, fps in enumerate(self._ladder):
            if fps <= int(target_fps):
                return index
        return len(self._ladder) - 1
