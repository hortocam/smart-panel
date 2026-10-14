"""Unit tests for gauge aggregation (T090, US4).

Covers ``smart_panel/plugins/gauges/aggregation.py``: the five aggregations
(``last``, ``avg``, ``min``, ``max``, ``sum``) over a window of 10..86400
seconds, ``last`` ignoring the window, the sample buffer bounded at 3,600
points by averaging older samples into buckets, correct results across bucket
boundaries, and stability across a wall-clock jump or a suspend/resume.

Windows are measured on the **monotonic** clock (``research.md`` decision 3), so
the local clock here can jump its wall time without moving its monotonic time.
"""

from __future__ import annotations

import pytest

from smart_panel.plugins.gauges.aggregation import (
    AGGREGATIONS,
    DEFAULT_CAPACITY,
    WINDOW_MAX_S,
    WINDOW_MIN_S,
    AggregationError,
    SampleBuffer,
)


class _Clock:
    """A clock whose wall time can jump independently of its monotonic time."""

    def __init__(self, monotonic: float = 0.0, wall: float = 1_700_000_000.0) -> None:
        self._monotonic = float(monotonic)
        self._wall = float(wall)

    def monotonic(self) -> float:
        return self._monotonic

    def time(self) -> float:
        return self._wall

    def advance(self, seconds: float) -> None:
        """Move both clocks forward (an ordinary elapsed interval)."""
        self._monotonic += float(seconds)
        self._wall += float(seconds)

    def jump_wall(self, seconds: float) -> None:
        """Step the wall clock only (an NTP correction)."""
        self._wall += float(seconds)


def test_the_five_supported_aggregations_are_named() -> None:
    assert set(AGGREGATIONS) == {"last", "avg", "min", "max", "sum"}
    assert (WINDOW_MIN_S, WINDOW_MAX_S) == (10, 86400)


def test_each_aggregation_over_a_window() -> None:
    # Samples are a minute apart, so a window selects a known subset of them.
    buffer = SampleBuffer()
    for offset, value in enumerate([10.0, 20.0, 30.0, 40.0]):
        buffer.add(1000.0 + 60.0 * offset, value)

    now = 1180.0
    assert buffer.aggregate("last", 60, now) == 40.0
    assert buffer.aggregate("avg", 10, now) == pytest.approx(40.0)
    assert buffer.aggregate("min", 10, now) == pytest.approx(40.0)
    assert buffer.aggregate("max", 10, now) == pytest.approx(40.0)
    assert buffer.aggregate("sum", 10, now) == pytest.approx(40.0)

    assert buffer.aggregate("avg", 130, now) == pytest.approx(30.0)
    assert buffer.aggregate("min", 130, now) == pytest.approx(20.0)
    assert buffer.aggregate("max", 130, now) == pytest.approx(40.0)
    assert buffer.aggregate("sum", 130, now) == pytest.approx(90.0)

    assert buffer.aggregate("avg", 300, now) == pytest.approx(25.0)
    assert buffer.aggregate("sum", 300, now) == pytest.approx(100.0)


def test_the_window_edge_is_inclusive() -> None:
    """A sample exactly ``window_s`` old is inside; one more second is out."""
    buffer = SampleBuffer()
    buffer.add(100.0, 5.0)
    assert buffer.aggregate("avg", 10, 110.0) == pytest.approx(5.0)
    assert buffer.aggregate("avg", 10, 111.0) is None


def test_last_ignores_the_window() -> None:
    buffer = SampleBuffer()
    buffer.add(0.0, 11.0)
    buffer.add(1.0, 12.0)
    # An hour later nothing is inside a 60 s window, but ``last`` is unaffected.
    assert buffer.aggregate("avg", 60, 3600.0) is None
    assert buffer.aggregate("last", 60, 3600.0) == 12.0


def test_an_empty_buffer_reports_no_value() -> None:
    buffer = SampleBuffer()
    for rule in AGGREGATIONS:
        assert buffer.aggregate(rule, 60, 0.0) is None
    assert buffer.last() is None
    assert len(buffer) == 0


def test_clear_drops_every_sample() -> None:
    buffer = SampleBuffer()
    buffer.add(0.0, 1.0)
    buffer.add(1.0, 2.0)
    assert len(buffer) == 2
    buffer.clear()
    assert len(buffer) == 0
    assert buffer.aggregate("last", 60, 1.0) is None


def test_windows_outside_the_supported_range_are_refused() -> None:
    buffer = SampleBuffer()
    buffer.add(0.0, 1.0)
    for window in (0, -1, WINDOW_MIN_S - 1, WINDOW_MAX_S + 1):
        with pytest.raises(AggregationError):
            buffer.aggregate("avg", window, 0.0)
    with pytest.raises(AggregationError):
        buffer.aggregate("median", 60, 0.0)
    # ``last`` needs no window at all, so a nonsensical one is not consulted.
    assert buffer.aggregate("last", 0, 0.0) == 1.0


def test_a_non_numeric_sample_is_refused() -> None:
    buffer = SampleBuffer()
    for bad in ("12", None, [1], True):
        with pytest.raises(AggregationError):
            buffer.add(0.0, bad)


def test_the_buffer_is_bounded_and_older_samples_are_averaged_into_buckets() -> None:
    buffer = SampleBuffer()
    for index in range(DEFAULT_CAPACITY + 200):
        buffer.add(float(index), 7.0)

    assert len(buffer) <= DEFAULT_CAPACITY, "the ring must be bounded at 3,600 points"
    now = float(DEFAULT_CAPACITY + 199)
    # Averaging a constant into buckets must not change the aggregate.
    assert buffer.aggregate("avg", WINDOW_MAX_S, now) == pytest.approx(7.0)
    assert buffer.aggregate("min", WINDOW_MAX_S, now) == pytest.approx(7.0)
    assert buffer.aggregate("max", WINDOW_MAX_S, now) == pytest.approx(7.0)


def test_the_buffer_stays_bounded_over_a_long_run() -> None:
    buffer = SampleBuffer()
    for index in range(DEFAULT_CAPACITY * 4):
        buffer.add(float(index), float(index % 7))
        assert len(buffer) <= DEFAULT_CAPACITY


def test_aggregation_is_exact_across_bucket_boundaries() -> None:
    """A window inside the un-bucketed tail is exact; an older window still answers."""
    buffer = SampleBuffer()
    for index in range(7200):
        buffer.add(float(index), float(index))

    now = 7199.0
    recent = [now - offset for offset in range(10, -1, -1)]
    assert buffer.aggregate("avg", 10, now) == pytest.approx(sum(recent) / len(recent))
    assert buffer.aggregate("sum", 10, now) == pytest.approx(sum(recent))
    assert buffer.aggregate("min", 10, now) == pytest.approx(recent[0])
    assert buffer.aggregate("max", 10, now) == pytest.approx(now)

    # A window that straddles the oldest buckets still answers, and stays inside
    # the data's own range (no double counting, no NaN).
    whole = buffer.aggregate("avg", WINDOW_MAX_S, now)
    assert whole is not None
    assert 0.0 <= whole <= 7199.0


def test_the_newest_samples_are_kept_at_full_resolution() -> None:
    """Bucketing must only ever fold the *old* end of the buffer."""
    buffer = SampleBuffer()
    for index in range(DEFAULT_CAPACITY + 1000):
        buffer.add(float(index), float(index))
    now = float(DEFAULT_CAPACITY + 999)
    recent = [now - offset for offset in range(10, -1, -1)]
    assert buffer.aggregate("max", 10, now) == pytest.approx(now)
    assert buffer.aggregate("avg", 10, now) == pytest.approx(sum(recent) / len(recent))


def test_windows_use_the_monotonic_clock_not_wall_time() -> None:
    clock = _Clock()
    buffer = SampleBuffer()
    for index in range(3):
        buffer.add(clock.monotonic(), 10.0 * index)
        clock.advance(60.0)

    # A wall-clock jump (an NTP correction) must not move a single sample.
    clock.jump_wall(3600.0)
    now = clock.monotonic() - 1.0  # 179 s
    # The samples at 60 s and 120 s are the ones inside a 130 s window.
    assert buffer.aggregate("avg", 130, now) == pytest.approx(15.0)
    assert buffer.aggregate("sum", 130, now) == pytest.approx(30.0)

    # A suspend/resume: monotonic time jumps forward, so the old samples age out.
    clock.advance(7200.0)
    assert buffer.aggregate("avg", WINDOW_MAX_S, clock.monotonic()) == pytest.approx(10.0)
    assert buffer.aggregate("avg", 60, clock.monotonic()) is None
    assert buffer.aggregate("last", 60, clock.monotonic()) == 20.0


def test_window_values_lists_the_samples_inside_the_window_oldest_first() -> None:
    buffer = SampleBuffer()
    for offset, value in enumerate([1.0, 2.0, 3.0, 4.0]):
        buffer.add(500.0 + offset, value)
    assert buffer.window_values(10, 504.0) == [1.0, 2.0, 3.0, 4.0]
    assert buffer.window_values(1, 504.0) == [4.0]
    assert buffer.window_values(3, 504.0) == [2.0, 3.0, 4.0]
    assert buffer.window_values(10, 900.0) == []
