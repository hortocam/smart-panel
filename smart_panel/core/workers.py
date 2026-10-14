"""Per-instance render workers with deadlines and health states (T029).

Each plugin instance gets exactly one render worker: a thread with a
"latest-result" slot, so a slow render never queues up stale frames (research.md
decision 3). The runner drives it with :meth:`RenderWorker.submit` every frame
and :meth:`RenderWorker.check_deadlines` on each pass.

Health (contract ``plugin-sdk.md``):

* a render past the soft deadline (250 ms) is logged;
* a render past the hard deadline (2 s) marks the instance ``stalled`` and keeps
  the last good image; the worker is not asked to render again until the
  in-flight call returns (a thread cannot be killed, so it is abandoned);
* a render that raises, returns the wrong type or returns the wrong size keeps
  the previous image and marks the instance ``degraded`` with a ``last_error``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from smart_panel.sdk.context import FrameContext, SystemClock
from smart_panel.sdk.plugin import Image as RenderImage
from smart_panel.sdk.plugin import Inactive, Unchanged

SOFT_DEADLINE_S = 0.25
HARD_DEADLINE_S = 2.0


class InstanceState(StrEnum):
    """Runtime state of one plugin instance (``data-model.md``)."""

    STARTING = "starting"
    OK = "ok"
    DEGRADED = "degraded"
    STALLED = "stalled"
    FAILED = "failed"
    DISABLED = "disabled"


@dataclass
class WorkerStatus:
    """Snapshot of a worker's health."""

    state: InstanceState
    last_error: str | None = None
    last_render_s: float | None = None


class RenderWorker:
    """One render thread for one plugin instance.

    Args:
        plugin: The plugin instance (its ``render`` is called on the worker).
        clock: Monotonic clock used for the deadlines (injectable for tests).
        log: Optional structured logger (``logging.RunnerLog``-like).
        size: Expected region size; validated against an ``Image`` result.
            Defaults to ``plugin.ctx.region_size`` when available.
        name: Worker name for logs; defaults to the plugin's name/class.
        soft_deadline_s / hard_deadline_s: The two deadline thresholds.
    """

    def __init__(
        self,
        plugin: Any,
        *,
        clock: Any = None,
        log: Any = None,
        size: tuple[int, int] | None = None,
        name: str | None = None,
        soft_deadline_s: float = SOFT_DEADLINE_S,
        hard_deadline_s: float = HARD_DEADLINE_S,
    ) -> None:
        self.plugin = plugin
        self.name = name or getattr(plugin, "name", None) or type(plugin).__name__
        self._clock = clock if clock is not None else SystemClock()
        self._log = log
        self._soft = soft_deadline_s
        self._hard = hard_deadline_s
        if size is None:
            ctx = getattr(plugin, "ctx", None)
            size = getattr(ctx, "region_size", None)
        self._size: tuple[int, int] | None = (int(size[0]), int(size[1])) if size else None

        self._lock = threading.RLock()
        self._state = InstanceState.STARTING
        self._last_error: str | None = None
        self._last_image: Any = None
        self._last_render_s: float | None = None

        self._request: FrameContext | None = None
        self._render_started: float | None = None
        self._soft_logged = False

        self._wake = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name=f"render-{self.name}", daemon=True
        )
        self._thread.start()

    # ----------------------------------------------------------------- driving

    def submit(self, frame: FrameContext) -> None:
        """Hand the newest frame to the worker (latest wins); never blocks."""
        with self._lock:
            if not self._running or self._state in (InstanceState.DISABLED, InstanceState.FAILED):
                return
            self._request = frame
        self._idle.clear()
        self._wake.set()

    def check_deadlines(self, now: float | None = None) -> bool:
        """Log/press the deadlines; return ``True`` when the worker is stalled."""
        current = self._clock.monotonic() if now is None else now
        with self._lock:
            started = self._render_started
        if started is None:
            return False
        elapsed = current - started
        if elapsed > self._soft and not self._soft_logged:
            self._soft_logged = True
            if self._log is not None:
                self._log.warning(
                    "render_slow",
                    worker=self.name,
                    elapsed_s=elapsed,
                    soft_deadline_s=self._soft,
                )
        if elapsed > self._hard:
            with self._lock:
                if self._state != InstanceState.DISABLED:
                    self._state = InstanceState.STALLED
            return True
        return False

    def wait_idle(self, timeout: float | None = 2.0) -> bool:
        """Block until the in-flight render (and any queued frame) has finished."""
        return self._idle.wait(timeout)

    # ------------------------------------------------------------------ health

    @property
    def state(self) -> InstanceState:
        with self._lock:
            return self._state

    @property
    def last_image(self) -> Any:
        with self._lock:
            return self._last_image

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    @property
    def size(self) -> tuple[int, int] | None:
        """The expected region size an ``Image`` result is validated against.

        ``None`` means the instance has no region, so the output size is not
        checked (the frame's own size is used for rendering).
        """
        return self._size

    def set_size(self, size: tuple[int, int] | None) -> None:
        """Update the expected region size (the layout can change live)."""
        self._size = (int(size[0]), int(size[1])) if size else None

    @property
    def in_flight(self) -> bool:
        """Whether a render is currently executing."""
        with self._lock:
            return self._render_started is not None

    def status(self) -> WorkerStatus:
        return WorkerStatus(self.state, self.last_error, self._last_render_s)

    def disable(self) -> None:
        """Mark the instance disabled; it accepts no further renders."""
        with self._lock:
            self._state = InstanceState.DISABLED
        self._idle.set()

    def mark_failed(self, message: str) -> None:
        """Mark the instance failed (for example a start or manifest error)."""
        with self._lock:
            self._state = InstanceState.FAILED
            self._last_error = message
        self._idle.set()

    def stop(self, timeout: float | None = 2.0) -> None:
        """Stop the worker thread (a stuck render keeps running, as documented)."""
        self._running = False
        self._wake.set()
        self._thread.join(timeout)
        self._idle.set()

    # ------------------------------------------------------------------- worker

    def _loop(self) -> None:
        while self._running:
            self._wake.wait(0.05)
            self._wake.clear()
            with self._lock:
                frame = self._request
                self._request = None
                if frame is None:
                    continue
                self._render_started = self._clock.monotonic()
                self._soft_logged = False
                started = self._render_started
            try:
                result = self.plugin.render(frame)
                self._handle_result(result)
            except Exception as exc:  # a misbehaving plugin must not kill the worker
                self._mark_degraded(str(exc))
            finally:
                finished = self._clock.monotonic()
                with self._lock:
                    self._last_render_s = finished - started
                    self._render_started = None
                    more = self._request is not None
                if not more:
                    self._idle.set()

    def _handle_result(self, result: Any) -> None:
        if isinstance(result, RenderImage):
            image = result.image
            if self._size is not None and tuple(image.size) != self._size:
                self._mark_degraded(
                    f"invalid output size: {tuple(image.size)} for a {self._size} region"
                )
                return
            with self._lock:
                self._last_image = image
                self._state = InstanceState.OK
                self._last_error = None
        elif isinstance(result, (Unchanged, Inactive)):
            with self._lock:
                if isinstance(result, Inactive):
                    self._last_image = None
                self._state = InstanceState.OK
                self._last_error = None
        else:
            self._mark_degraded(
                f"invalid output: render returned {type(result).__name__}, not a RenderResult"
            )

    def _mark_degraded(self, message: str) -> None:
        with self._lock:
            self._state = InstanceState.DEGRADED
            self._last_error = message


def bind_alert_router(worker: RenderWorker, router: Any) -> None:
    """Attach the generic alert router to the instance ``worker`` renders (T132).

    Every instance the runner starts goes through here, so ``ctx.alert`` called
    from a plugin's render thread or from a ``ctx.spawn`` task reaches the same
    single router the control channel uses — the router resolves the
    ``alert_sink`` instance from the live instance map by manifest capability,
    and the core still names no plugin.

    A plugin that lacks the ``alert_source`` capability is refused inside
    ``ctx.alert`` itself, before the handler is reached.
    """
    ctx = getattr(getattr(worker, "plugin", None), "ctx", None)
    if ctx is not None:
        ctx.alert_handler = None if router is None else router.route
