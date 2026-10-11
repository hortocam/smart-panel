"""Unit tests for ``smart_panel.core.scheduler`` (T035).

The scheduler runs ``ctx.spawn`` tasks and data-source fetches on a small
thread pool with per-task timeouts, one in-flight run per task, cancellation on
stop, and an interval multiplier the governor raises. It is driven from the
runner's housekeeping loop via ``run_pending()``, so tests pump it manually and
advance the fake clock instead of sleeping.
"""

from __future__ import annotations

import threading
import time

import pytest

from smart_panel.core.scheduler import Scheduler
from tests.conftest import FakeClock


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not met before the deadline")


def test_spawn_after_stop_is_rejected(fake_clock: FakeClock) -> None:
    scheduler = Scheduler(clock=fake_clock)
    scheduler.stop()
    with pytest.raises(RuntimeError):
        scheduler.spawn(lambda: None)


def test_a_task_never_overlaps_with_itself(fake_clock: FakeClock) -> None:
    scheduler = Scheduler(clock=fake_clock)
    started = threading.Event()
    release = threading.Event()
    runs: list[int] = []

    def fn() -> None:
        runs.append(1)
        started.set()
        release.wait(2)

    task = scheduler.spawn(fn, interval_s=10)
    try:
        assert scheduler.run_pending() == 1
        assert started.wait(2)
        fake_clock.advance(100)
        # The interval has elapsed, but the task is still in flight.
        assert scheduler.run_pending() == 0
        assert len(runs) == 1
    finally:
        release.set()
        _wait_until(lambda: not task.running)
        scheduler.stop()


def test_timeout_marks_the_task_without_resubmitting(fake_clock: FakeClock) -> None:
    scheduler = Scheduler(clock=fake_clock)
    started = threading.Event()
    release = threading.Event()

    def fn() -> None:
        started.set()
        release.wait(2)

    task = scheduler.spawn(fn, timeout_s=5)
    try:
        assert scheduler.run_pending() == 1
        assert started.wait(2)
        assert scheduler.check_timeouts() == []
        fake_clock.advance(6)
        timed_out = scheduler.check_timeouts()
        assert task in timed_out
        assert task.state == "timed_out"
        assert scheduler.run_pending() == 0
    finally:
        release.set()
        _wait_until(lambda: not task.running)
        scheduler.stop()


def test_cancelled_task_never_runs(fake_clock: FakeClock) -> None:
    scheduler = Scheduler(clock=fake_clock)
    calls: list[int] = []
    task = scheduler.spawn(lambda: calls.append(1))
    try:
        task.cancel()
        assert scheduler.run_pending() == 0
        assert calls == []
        assert task.cancelled
    finally:
        scheduler.stop()


def test_stop_cancels_tasks_and_stops_the_pump(fake_clock: FakeClock) -> None:
    scheduler = Scheduler(clock=fake_clock)
    calls: list[int] = []
    task = scheduler.spawn(lambda: calls.append(1), interval_s=10)
    scheduler.stop()
    assert task.cancelled
    assert scheduler.run_pending() == 0
    assert calls == []


def test_interval_multiplier_scales_the_next_run(fake_clock: FakeClock) -> None:
    scheduler = Scheduler(clock=fake_clock)
    done = threading.Event()
    task = scheduler.spawn(lambda: done.set(), interval_s=10)
    try:
        scheduler.set_interval_multiplier(2.0)
        assert scheduler.run_pending() == 1
        assert done.wait(2)
        _wait_until(lambda: not task.running)
        assert task.last_finished is not None
        assert task.next_run is not None
        assert task.next_run - task.last_finished == pytest.approx(20.0)
    finally:
        scheduler.stop()


def test_task_error_is_captured(fake_clock: FakeClock) -> None:
    scheduler = Scheduler(clock=fake_clock)
    done = threading.Event()

    def fn() -> None:
        done.set()
        raise ValueError("boom")

    task = scheduler.spawn(fn)
    try:
        scheduler.run_pending()
        assert done.wait(2)
        _wait_until(lambda: not task.running)
        assert task.state == "error"
        assert task.last_error == "boom"
    finally:
        scheduler.stop()
