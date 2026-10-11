"""Contexts and clock handed to plugins (T019).

``InstanceContext`` is the long-lived per-instance service bag (bus, kv, db,
scheduler, http, alerts); ``FrameContext`` is the per-frame render input. Both
are constructed by the runner and are read by plugins through the SDK only.
``Sample``/``Freshness`` and the ``Clock`` protocol live here too because the
bus (``smart_panel.core.bus``) and the SDK harness both need them.
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from smart_panel.sdk.plugin import Manifest


class Freshness(StrEnum):
    """Freshness of a bus sample (``data-model.md``)."""

    FRESH = "fresh"
    STALE = "stale"
    NO_DATA = "no_data"


@dataclass(frozen=True)
class Sample:
    """One value read from the bus.

    Attributes:
        value: JSON-compatible value, or ``None`` when the topic was never set.
        ts: Wall-clock seconds of the last update (``None`` for ``no_data``).
        freshness: :class:`Freshness` — ``fresh``, ``stale`` or ``no_data``.
    """

    value: Any = None
    ts: float | None = None
    freshness: Freshness = Freshness.NO_DATA


@runtime_checkable
class Clock(Protocol):
    """Time source; replaced by a fake clock in tests."""

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin; only differences are meaningful."""
        ...

    def time(self) -> float:
        """Wall-clock POSIX seconds."""
        ...


class SystemClock:
    """The real :class:`Clock` (``time.monotonic`` / ``time.time``)."""

    def monotonic(self) -> float:
        return _time.monotonic()

    def time(self) -> float:
        return _time.time()


class ReadOnlyBus:
    """A read-only view of the bus, as exposed on ``FrameContext``.

    ``get`` and ``topics`` are forwarded; ``publish``/``subscribe`` are
    intentionally unavailable so a render function cannot mutate state.
    """

    __slots__ = ("_bus",)

    def __init__(self, bus: Any) -> None:
        self._bus = bus

    def get(self, topic: str) -> Sample:
        return self._bus.get(topic)

    def topics(self, prefix: str = "") -> list[str]:
        return self._bus.topics(prefix)

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(
            f"the frame bus is read-only; {name!r} is not available during render"
        )


@dataclass
class FrameContext:
    """Everything a region/overlay plugin may read while rendering a frame."""

    size: tuple[int, int]
    now: float
    wall: float
    frame_index: int
    fonts: Any = None
    icons: Any = None
    bus: Any = None
    min_text_px: int = 24


@dataclass
class InstanceContext:
    """Long-lived services for one plugin instance.

    The runner injects the concrete handlers (``db_factory``, ``spawn_handler``,
    ``alert_handler``, ...). Methods raise if the handler is not wired, so a
    plugin used outside the runner fails loudly rather than silently.
    """

    name: str
    manifest: Manifest | None = None
    region_size: tuple[int, int] | None = None
    log: Any = None
    bus: Any = None
    kv: Any = None
    clock: Clock = field(default_factory=SystemClock)
    http: Any = None
    db_factory: Callable[[], Any] | None = None
    spawn_handler: Callable[..., Any] | None = None
    request_render_handler: Callable[[], None] | None = None
    notify_handler: Callable[[Any], None] | None = None
    alert_handler: Callable[..., Any] | None = None

    def db(self) -> Any:
        """A private stdlib ``sqlite3`` connection to ``<home>/state/<name>.db``."""
        if self.db_factory is None:
            raise RuntimeError(f"instance {self.name!r} has no database")
        return self.db_factory()

    def spawn(
        self,
        fn: Callable[[], Any],
        interval_s: float | None = None,
        timeout_s: float | None = None,
        *,
        name: str | None = None,
    ) -> Any:
        """Run ``fn`` in the runner's worker pool; one run at a time."""
        if self.spawn_handler is None:
            raise RuntimeError(f"instance {self.name!r} cannot spawn tasks")
        return self.spawn_handler(fn, interval_s, timeout_s, name)

    def request_render(self) -> None:
        """Mark this instance's region dirty."""
        if self.request_render_handler is not None:
            self.request_render_handler()

    def notify(self, event: Any) -> None:
        """Emit an event to the log and ``status``."""
        if self.notify_handler is not None:
            self.notify_handler(event)

    def alert(
        self,
        severity: str,
        message: str,
        title: str | None = None,
        source: str | None = None,
        id: str | None = None,
        ttl: float | None = None,
    ) -> Any:
        """Raise an alert; requires the ``alert_source`` capability.

        The core routes the alert generically to the instance declaring
        ``alert_sink``; there is no alerts logic in the core.
        """
        capabilities = self.manifest.capabilities if self.manifest is not None else frozenset()
        if "alert_source" not in capabilities:
            raise PermissionError(
                f"instance {self.name!r} lacks the 'alert_source' capability required by ctx.alert"
            )
        if self.alert_handler is None:
            raise RuntimeError(f"instance {self.name!r} cannot raise alerts")
        return self.alert_handler(
            severity=severity, message=message, title=title, source=source, id=id, ttl=ttl
        )
