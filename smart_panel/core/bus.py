"""The topic bus: latest value per topic plus freshness (T020).

Consumers (gauges, sidebar, crawl) read a :class:`~smart_panel.sdk.context.Sample`
and turn ``stale``/``no_data`` into their visible stale state. The bus is
thread-safe (publishers are plugin workers and data-source threads) and takes
an injectable clock so freshness transitions are testable without sleeping.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from smart_panel.sdk.context import Freshness, Sample, SystemClock

Subscriber = Callable[[str, Sample], None]


@dataclass
class _Entry:
    value: Any
    ts: float
    ttl_s: float | None


class Subscription:
    """Handle returned by :meth:`Bus.subscribe`; call :meth:`cancel` to stop."""

    def __init__(self, bus: Bus, topic: str, callback: Subscriber) -> None:
        self._bus = bus
        self._topic = topic
        self._callback = callback

    def cancel(self) -> None:
        self._bus.unsubscribe(self._topic, self._callback)


class Bus:
    """A small topic store with freshness and change notifications."""

    def __init__(self, clock: Any = None) -> None:
        self._clock = clock if clock is not None else SystemClock()
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}
        self._subscribers: list[tuple[str, Subscriber]] = []
        self.last_callback_error: str | None = None

    def publish(self, topic: str, value: Any, ttl_s: float | None = None) -> None:
        """Store ``value`` for ``topic`` (fresh for ``ttl_s`` seconds when set)."""
        with self._lock:
            self._entries[topic] = _Entry(value=value, ts=self._clock.time(), ttl_s=ttl_s)
            listeners = [callback for name, callback in self._subscribers if name == topic]
        sample = self.get(topic)
        for callback in listeners:
            try:
                callback(topic, sample)
            except Exception as exc:  # a bad subscriber must not break a publisher
                self.last_callback_error = str(exc)

    def get(self, topic: str) -> Sample:
        """Return the current sample for ``topic`` with its freshness."""
        with self._lock:
            entry = self._entries.get(topic)
        if entry is None:
            return Sample(value=None, ts=None, freshness=Freshness.NO_DATA)
        age = self._clock.time() - entry.ts
        if entry.ttl_s is not None and age > entry.ttl_s:
            return Sample(value=entry.value, ts=entry.ts, freshness=Freshness.STALE)
        return Sample(value=entry.value, ts=entry.ts, freshness=Freshness.FRESH)

    def subscribe(self, topic: str, callback: Subscriber) -> Subscription:
        """Call ``callback(topic, sample)`` whenever ``topic`` is published."""
        with self._lock:
            self._subscribers.append((topic, callback))
        return Subscription(self, topic, callback)

    def unsubscribe(self, topic: str, callback: Subscriber) -> None:
        with self._lock:
            self._subscribers = [
                (name, existing)
                for name, existing in self._subscribers
                if not (name == topic and existing is callback)
            ]

    def topics(self, prefix: str = "") -> list[str]:
        """Sorted topic names, optionally filtered by ``prefix``."""
        with self._lock:
            return sorted(topic for topic in self._entries if topic.startswith(prefix))
