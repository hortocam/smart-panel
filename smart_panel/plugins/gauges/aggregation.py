"""Bounded sample buffer and the five gauge aggregations (T096, US4).

``research.md`` decision 12: each gauge keeps an in-memory ring of
``(timestamp, value)`` samples, bounded by a hard cap of 3,600 points, and the
supported aggregations are ``last``, ``avg``, ``min``, ``max`` and ``sum`` over
a window of 10 s to 24 h. Samples are never persisted — after a restart the
window refills.

Timestamps are **monotonic** seconds (``research.md`` decision 3: time-based
behaviour uses ``frame.now``, never wall time), so an NTP step or a timezone
change cannot move a sample into or out of a window, and a suspend/resume simply
ages the old samples out.

When the buffer would exceed its cap the *oldest* entries are folded pairwise
into weighted buckets (``(timestamp, value, weight)`` pairs are averaged, so a
bucket represents ``weight`` samples at its mean value). The newest entries stay
at full resolution, and because a bucket's value is the mean of its members, an
``avg`` over a constant series is unchanged by compaction while ``sum`` stays
proportional to the number of samples represented.
"""

from __future__ import annotations

__all__ = [
    "AGGREGATIONS",
    "DEFAULT_CAPACITY",
    "WINDOW_MAX_S",
    "WINDOW_MIN_S",
    "AggregationError",
    "SampleBuffer",
]

DEFAULT_CAPACITY = 3600
WINDOW_MIN_S = 10
WINDOW_MAX_S = 86400
AGGREGATIONS = ("last", "avg", "min", "max", "sum")


class AggregationError(ValueError):
    """A sample or an aggregation request did not conform."""


def _number(value: object, what: str) -> float:
    """Return ``value`` as a float, refusing booleans, strings and the like."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AggregationError(f"{what} must be a number, got {value!r}")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        raise AggregationError(f"{what} must be a finite number, got {value!r}")
    return number


class SampleBuffer:
    """A bounded ring of ``(timestamp, value)`` samples and its aggregations.

    Args:
        capacity: Most entries to keep before the oldest are bucketed.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise AggregationError(f"capacity must be a positive integer, got {capacity!r}")
        self._capacity = int(capacity)
        self._entries: list[list[float]] = []

    # ------------------------------------------------------------- inspection

    def __len__(self) -> int:
        """The number of entries (a bucketed entry counts once)."""
        return len(self._entries)

    @property
    def capacity(self) -> int:
        """The hard cap on the number of entries."""
        return self._capacity

    def clear(self) -> None:
        """Drop every sample."""
        self._entries = []

    def last(self) -> float | None:
        """The newest sample's value, or ``None`` when the buffer is empty."""
        if not self._entries:
            return None
        return self._entries[-1][1]

    # --------------------------------------------------------------- sampling

    def add(self, timestamp: float, value: float) -> None:
        """Append one sample, folding the oldest entries when the cap is crossed."""
        moment = _number(timestamp, "a sample timestamp")
        self._entries.append([moment, _number(value, "a sample value"), 1.0])
        if len(self._entries) > self._capacity:
            self._compact()

    # ----------------------------------------------------------- aggregations

    def aggregate(self, rule: str, window_s: float, now: float) -> float | None:
        """Apply ``rule`` to the samples inside ``window_s`` of ``now``.

        ``last`` ignores the window entirely (data-model: "``window_s`` ...
        ignored for ``last``"). Every other rule uses only the samples whose
        timestamp is at or after ``now - window_s``, and returns ``None`` when
        that window is empty (the caller renders its ``no data`` state).

        Raises:
            AggregationError: The rule is unknown, or the window is outside
                10..86400 seconds.
        """
        if rule not in AGGREGATIONS:
            raise AggregationError(
                f"unknown aggregation {rule!r}; expected one of {list(AGGREGATIONS)}"
            )
        if rule == "last":
            return self.last()
        window = _number(window_s, "window_s")
        if window < WINDOW_MIN_S or window > WINDOW_MAX_S:
            raise AggregationError(
                f"window_s must be between {WINDOW_MIN_S} and {WINDOW_MAX_S}, got {window_s!r}"
            )
        moment = _number(now, "now")
        entries = self._inside(window, moment)
        if not entries:
            return None
        values = [entry[1] for entry in entries]
        if rule == "min":
            return min(values)
        if rule == "max":
            return max(values)
        weighted = sum(entry[1] * entry[2] for entry in entries)
        if rule == "sum":
            return weighted
        weight = sum(entry[2] for entry in entries)
        return weighted / weight

    def window_values(self, window_s: float, now: float) -> list[float]:
        """The representative values inside the window, oldest first.

        This is what a ``sparkline`` gauge draws; a bucketed entry contributes
        its mean value once.
        """
        window = _number(window_s, "window_s")
        if window <= 0:
            return []
        moment = _number(now, "now")
        return [entry[1] for entry in self._inside(window, moment)]

    # -------------------------------------------------------------- internals

    def _inside(self, window: float, now: float) -> list[list[float]]:
        """The entries at or after ``now - window`` (the window edge is inclusive)."""
        edge = now - window
        return [entry for entry in self._entries if entry[0] >= edge]

    def _compact(self) -> None:
        """Fold adjacent entries from the oldest end into weighted buckets."""
        folded: list[list[float]] = []
        entries = self._entries
        index = 0
        while index + 1 < len(entries):
            first, second = entries[index], entries[index + 1]
            weight = first[2] + second[2]
            folded.append(
                [
                    (first[0] * first[2] + second[0] * second[2]) / weight,
                    (first[1] * first[2] + second[1] * second[2]) / weight,
                    weight,
                ]
            )
            index += 2
        if index < len(entries):
            # The newest sample (an odd one out) keeps full resolution.
            folded.append(entries[-1])
        self._entries = folded
        if len(self._entries) > self._capacity:  # pragma: no cover - tiny capacities
            self._compact()
