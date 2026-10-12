"""Unit tests for ``smart_panel.core.governor`` (T041).

The governor calls ``os.nice(5)`` at start, measures frame time and process CPU
every 5 s, steps the frame rate down the ladder (15, 12, 10, 8, 5) after three
consecutive overrun windows while doubling the scheduler's source-refresh
interval (capped at 4x), and steps one rung back up after 60 s of headroom.
Every change is logged and reflected in the reported level (``status()``).

The clock, the ``nice`` call and the CPU reader are all injected, so these tests
never renice the test process, never touch ``psutil`` and never sleep.
"""

from __future__ import annotations

from smart_panel.core.governor import (
    FPS_LADDER,
    HEADROOM_S,
    OVERRUN_WINDOWS,
    WINDOW_S,
    Governor,
)
from tests.conftest import FakeClock


class _Scheduler:
    """Records the interval multipliers the governor asks for."""

    def __init__(self) -> None:
        self.multipliers: list[float] = []

    def set_interval_multiplier(self, multiplier: float) -> None:
        self.multipliers.append(float(multiplier))


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def debug(self, event: str, **fields: object) -> None:
        self.events.append(("debug", {"event": event, **fields}))

    def info(self, event: str, **fields: object) -> None:
        self.events.append(("info", {"event": event, **fields}))

    def warning(self, event: str, **fields: object) -> None:
        self.events.append(("warning", {"event": event, **fields}))

    def error(self, event: str, **fields: object) -> None:
        self.events.append(("error", {"event": event, **fields}))


def _governor(
    clock: FakeClock, *, scheduler: _Scheduler | None = None
) -> tuple[Governor, _Scheduler, _Log]:
    sched = scheduler if scheduler is not None else _Scheduler()
    log = _Log()
    governor = Governor(
        sched,
        target_fps=15,
        clock=clock,
        log=log,
        nice=lambda amount: None,
        cpu_reader=lambda: None,
    )
    return governor, sched, log


def _overrun_window(governor: Governor, clock: FakeClock) -> None:
    """Record a frame well over budget and let the 5 s window elapse."""
    governor.record_frame(1.0)
    clock.advance(WINDOW_S)
    governor.tick()


def _good_window(governor: Governor, clock: FakeClock) -> None:
    """Record a tiny frame and let the 5 s window elapse."""
    governor.record_frame(0.001)
    clock.advance(WINDOW_S)
    governor.tick()


def _log_events(log: _Log, name: str) -> list[dict]:
    return [fields for _, fields in log.events if fields["event"] == name]


class TestGovernorLadder:
    def test_start_calls_nice_once(self, fake_clock: FakeClock) -> None:
        calls: list[int] = []
        governor = Governor(
            None,
            target_fps=15,
            clock=fake_clock,
            nice=calls.append,
            cpu_reader=lambda: None,
        )
        governor.start()
        governor.start()
        assert calls == [5]

    def test_starts_on_the_ladder_rung_for_the_target(self, fake_clock: FakeClock) -> None:
        governor, _, _ = _governor(fake_clock)
        assert governor.level == 0
        assert governor.target_fps == FPS_LADDER[0] == 15
        assert governor.multiplier == 1.0

    def test_a_single_overrun_window_does_not_step_down(self, fake_clock: FakeClock) -> None:
        governor, _, _ = _governor(fake_clock)
        for _ in range(OVERRUN_WINDOWS - 1):
            _overrun_window(governor, fake_clock)
        assert governor.target_fps == 15

    def test_three_consecutive_overrun_windows_step_the_whole_ladder(
        self, fake_clock: FakeClock
    ) -> None:
        governor, scheduler, log = _governor(fake_clock)

        expected = [(12, 2.0), (10, 4.0), (8, 4.0), (5, 4.0)]
        for fps, multiplier in expected:
            for _ in range(OVERRUN_WINDOWS):
                _overrun_window(governor, fake_clock)
            assert governor.target_fps == fps
            assert governor.multiplier == multiplier
            # The change is reported in the level ...
            assert governor.status()["level"] == FPS_LADDER.index(fps)
            assert governor.status()["target_fps"] == fps

        # The scheduler's source intervals were raised, capped at 4x.
        assert scheduler.multipliers == [2.0, 4.0, 4.0, 4.0]

    def test_further_overruns_stay_at_the_floor(self, fake_clock: FakeClock) -> None:
        governor, scheduler, _ = _governor(fake_clock)
        for _ in range(OVERRUN_WINDOWS * len(FPS_LADDER)):
            _overrun_window(governor, fake_clock)
        assert governor.target_fps == FPS_LADDER[-1] == 5
        assert governor.multiplier == 4.0
        assert scheduler.multipliers[-1] == 4.0

    def test_every_change_is_logged(self, fake_clock: FakeClock) -> None:
        governor, _, log = _governor(fake_clock)
        for _ in range(OVERRUN_WINDOWS):
            _overrun_window(governor, fake_clock)
        steps = _log_events(log, "governor_step")
        assert len(steps) == 1
        assert steps[0]["level"] == 1
        assert steps[0]["fps"] == 12
        assert steps[0]["multiplier"] == 2.0
        assert steps[0]["reason"] == "overrun"

    def test_a_good_window_resets_the_overrun_streak(self, fake_clock: FakeClock) -> None:
        governor, _, _ = _governor(fake_clock)
        _overrun_window(governor, fake_clock)
        _overrun_window(governor, fake_clock)
        _good_window(governor, fake_clock)  # resets the streak
        _overrun_window(governor, fake_clock)
        _overrun_window(governor, fake_clock)
        assert governor.target_fps == 15  # never reached three in a row


class TestGovernorRetarget:
    """``display.fps`` sets the top rung; a live change retargets the governor."""

    def test_a_higher_configured_target_becomes_the_top_rung(self, fake_clock: FakeClock) -> None:
        governor = Governor(None, target_fps=20, clock=fake_clock, nice=lambda _n: None,
                            cpu_reader=lambda: None)
        assert governor.target_fps == 20
        assert governor.level == 0
        # The standard rungs follow below it.
        for _ in range(OVERRUN_WINDOWS):
            _overrun_window(governor, fake_clock)
        assert governor.target_fps == 15

    def test_retarget_resets_to_the_new_top_rung(self, fake_clock: FakeClock) -> None:
        governor, scheduler, _ = _governor(fake_clock)
        for _ in range(OVERRUN_WINDOWS):
            _overrun_window(governor, fake_clock)
        assert governor.target_fps == 12

        governor.retarget(10)
        assert governor.level == 0
        assert governor.target_fps == 10
        assert governor.multiplier == 1.0
        assert scheduler.multipliers[-1] == 1.0


class TestGovernorHeadroom:
    def _drive_to_floor(self, governor: Governor, clock: FakeClock) -> None:
        for _ in range(OVERRUN_WINDOWS * len(FPS_LADDER)):
            _overrun_window(governor, clock)
        assert governor.target_fps == 5

    def test_59s_of_headroom_does_not_step_up(self, fake_clock: FakeClock) -> None:
        governor, _, _ = _governor(fake_clock)
        self._drive_to_floor(governor, fake_clock)
        # 12 windows of 5 s is exactly 60 s; stop one short.
        for _ in range(int(HEADROOM_S // WINDOW_S) - 1):
            _good_window(governor, fake_clock)
        assert governor.target_fps == 5

    def test_60s_of_headroom_steps_back_up(self, fake_clock: FakeClock) -> None:
        governor, scheduler, log = _governor(fake_clock)
        self._drive_to_floor(governor, fake_clock)
        before = len(scheduler.multipliers)

        for _ in range(int(HEADROOM_S // WINDOW_S)):
            _good_window(governor, fake_clock)

        assert governor.target_fps == 8
        assert governor.status()["level"] == FPS_LADDER.index(8)
        steps = _log_events(log, "governor_step")
        assert steps[-1]["reason"] == "headroom"
        assert steps[-1]["fps"] == 8
        # Stepping back up also updates the scheduler multiplier (8 fps -> 4x cap).
        assert scheduler.multipliers[before:] == [4.0]

    def test_headroom_steps_up_one_rung_at_a_time_to_the_top(
        self, fake_clock: FakeClock
    ) -> None:
        governor, _, _ = _governor(fake_clock)
        self._drive_to_floor(governor, fake_clock)
        for expected in (8, 10, 12, 15):
            for _ in range(int(HEADROOM_S // WINDOW_S)):
                _good_window(governor, fake_clock)
            assert governor.target_fps == expected
        # At the top rung a further minute of headroom changes nothing.
        for _ in range(int(HEADROOM_S // WINDOW_S)):
            _good_window(governor, fake_clock)
        assert governor.target_fps == 15
        assert governor.multiplier == 1.0
