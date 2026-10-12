"""A small thread pool for spawned tasks and timed fetches (T035).

``ctx.spawn`` tasks and data-source fetches run here: at most four in flight,
one run of any given task at a time, per-task timeouts, cancellation on stop,
and an interval multiplier the governor (T050) can raise. The runner's
housekeeping loop pumps due work with :meth:`Scheduler.run_pending`; that keeps
the scheduler testable with a fake clock instead of real sleeps.
"""

from __future__ import annotations

import itertools
import math
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from smart_panel.sdk.context import SystemClock

DEFAULT_MAX_WORKERS = 4


@dataclass
class Task:
    """One scheduled callable and its runtime state."""

    id: str
    fn: Callable[[], Any]
    interval_s: float | None = None
    timeout_s: float | None = None
    name: str | None = None
    runs: int = 0
    running: bool = False
    cancelled: bool = False
    state: str = "idle"
    last_error: str | None = None
    last_started: float | None = None
    last_finished: float | None = None
    next_run: float | None = None
    anchor: float | None = None
    """The instant the current interval is measured from.

    Set at registration (when the first run is not immediate) and on every
    finish, so :meth:`Scheduler.set_interval_multiplier` can recompute a
    pending ``next_run`` under a new multiplier without waiting for the task to
    run again.
    """

    def cancel(self) -> None:
        """Prevent any further runs of this task."""
        self.cancelled = True


class Scheduler:
    """A bounded thread pool with interval scheduling and timeouts."""

    def __init__(self, max_workers: int = DEFAULT_MAX_WORKERS, clock: Any = None) -> None:
        self._clock = clock if clock is not None else SystemClock()
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="smart-panel-task"
        )
        self._tasks: dict[str, Task] = {}
        self._lock = threading.RLock()
        self._counter = itertools.count(1)
        self._multiplier = 1.0
        self._stopped = False

    def spawn(
        self,
        fn: Callable[[], Any],
        interval_s: float | None = None,
        timeout_s: float | None = None,
        *,
        task_id: str | None = None,
        name: str | None = None,
        run_immediately: bool = True,
    ) -> Task:
        """Register ``fn``. With ``interval_s`` it re-runs; without it, once."""
        with self._lock:
            if self._stopped:
                raise RuntimeError("scheduler has been stopped")
            identifier = task_id or f"task-{next(self._counter)}"
            if identifier in self._tasks:
                raise ValueError(f"duplicate task id {identifier!r}")
            now = self._clock.monotonic()
            task = Task(
                id=identifier,
                fn=fn,
                interval_s=interval_s,
                timeout_s=timeout_s,
                name=name,
                anchor=now,
            )
            delay = (interval_s or 0.0) * self._multiplier
            task.next_run = now if run_immediately else now + delay
            self._tasks[identifier] = task
            return task

    def run_pending(self) -> int:
        """Submit every due task that is not already running; return the count."""
        submitted = 0
        with self._lock:
            if self._stopped:
                return 0
            now = self._clock.monotonic()
            for task in list(self._tasks.values()):
                if task.cancelled or task.running:
                    continue
                if task.next_run is None or now < task.next_run:
                    continue
                task.running = True
                task.runs += 1
                task.state = "running"
                task.last_started = now
                self._pool.submit(self._run, task)
                submitted += 1
        return submitted

    def check_timeouts(self) -> list[Task]:
        """Mark running tasks whose elapsed time exceeds their timeout."""
        timed_out: list[Task] = []
        with self._lock:
            now = self._clock.monotonic()
            for task in self._tasks.values():
                if (
                    task.running
                    and task.timeout_s is not None
                    and task.last_started is not None
                    and now - task.last_started > task.timeout_s
                ):
                    task.state = "timed_out"
                    task.last_error = "timeout"
                    timed_out.append(task)
        return timed_out

    def set_interval_multiplier(self, multiplier: float) -> None:
        """Scale every task's interval (the governor raises this under load).

        A pending task's ``next_run`` is recomputed immediately from its anchor,
        so an already-scheduled interval picks up the new multiplier instead of
        waiting until the task next finishes. A task that is already due is left
        alone (it is about to run anyway).

        Raises:
            ValueError: ``multiplier`` is not a finite positive number. The
                governor must never stretch intervals to infinity or NaN.
        """
        value = float(multiplier)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("interval multiplier must be a finite positive number")
        with self._lock:
            self._multiplier = value
            now = self._clock.monotonic()
            for task in self._tasks.values():
                if task.interval_s is None or task.cancelled or task.next_run is None:
                    continue
                if task.next_run <= now:
                    continue  # already due; leave it for the next pump
                anchor = task.last_finished if task.last_finished is not None else task.anchor
                if anchor is None:
                    continue
                task.next_run = anchor + task.interval_s * value

    def cancel(self, task_id: str) -> Task | None:
        """Cancel one task by id."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None:
                task.cancel()
                task.next_run = None
            return task

    def stop(self) -> None:
        """Cancel every task and shut the pool down."""
        with self._lock:
            self._stopped = True
            for task in self._tasks.values():
                task.cancel()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _run(self, task: Task) -> None:
        error: str | None = None
        try:
            task.fn()
        except Exception as exc:  # recorded as the task error, never re-raised
            error = str(exc)
        with self._lock:
            finished = self._clock.monotonic()
            task.running = False
            task.last_finished = finished
            if error is not None:
                task.state = "error"
                task.last_error = error
            elif task.state != "timed_out":
                task.state = "ok"
            if task.interval_s is None:
                task.next_run = None
            else:
                task.anchor = finished
                task.next_run = finished + task.interval_s * self._multiplier
