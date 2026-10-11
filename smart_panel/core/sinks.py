"""Frame sinks: where a composed frame goes (T027, T028).

``PanelSink`` is the protocol every sink satisfies:

* ``FileSink`` writes the composed canvas to ``latest.png`` atomically (temp
  file + ``os.replace``), so a reader never sees a half-written frame, and
  numbered ``frame-NNNNNN.png`` files when asked (T027).
* ``NullSink`` accepts frames and discards them (T027).
* ``HidSink`` drives the real panel through :mod:`panel_driver.device` (T028):
  it opens with retries, sends CRTDIS + CRTLIG on every successful open,
  reapplies brightness, and reopens on the first mid-stream write failure.

The protocol's ``send`` carries one frame in the sink's native form: the list
of 1024-byte HID packets for the panel sink, or the composed canvas image for a
visual sink. A panel sink is fed encoded packets because the encoder owns the
rotation and size fitting; a visual sink is fed the canvas directly.

Every ``dev.write`` stays inside :mod:`panel_driver.device` (constitution
Principle II): ``HidSink`` only calls ``init_display``/``write_frame``/
``set_backlight`` from that module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from PIL import Image

from panel_driver import device as _device
from panel_driver.protocol import validate_brightness
from smart_panel.sdk.context import SystemClock

# Open retry backoff (research.md decision 17): 1, 2, 4, 8, then 10 s cap.
DEFAULT_BACKOFF = (1.0, 2.0, 4.0, 8.0, 10.0)
# A power-cycle hint is reported only after the *open* keeps failing this long.
UNRESPONSIVE_AFTER_S = 60.0


class SinkState(StrEnum):
    """Connection state reported by :meth:`PanelSink.state`."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    UNRESPONSIVE = "unresponsive"


@dataclass
class SinkStatus:
    """Snapshot returned by :meth:`PanelSink.state`."""

    state: SinkState
    last_error: str | None = None
    frames: int = 0


@runtime_checkable
class PanelSink(Protocol):
    """The interface the runner drives to get frames onto a target."""

    def open(self) -> None:
        """Open the target (idempotent)."""
        ...

    def send(self, packets: Any) -> None:
        """Deliver one frame (packets for the panel, the canvas for a visual sink)."""
        ...

    def set_brightness(self, n: int) -> None:
        """Apply a backlight brightness (0-100)."""
        ...

    def close(self) -> None:
        """Release the target."""
        ...

    def state(self) -> SinkStatus:
        """Return the current connection state, last error and frame count."""
        ...


class FileSink:
    """Write the composed canvas to PNG files (the no-panel target)."""

    def __init__(
        self,
        directory: str | os.PathLike[str],
        *,
        keep_frames: bool = False,
        filename: str = "latest.png",
    ) -> None:
        self.directory = Path(directory)
        self.keep_frames = keep_frames
        self.filename = filename
        self._frames = 0
        self._brightness = 50
        self._last_error: str | None = None
        self._open = False

    def open(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._open = True

    def send(self, canvas: Image.Image) -> None:
        """Write ``canvas`` to ``latest.png`` and, if asked, a numbered frame."""
        if not self._open:
            self.open()
        self._atomic_save(canvas, self.directory / self.filename)
        self._frames += 1
        if self.keep_frames:
            self._atomic_save(canvas, self.directory / f"frame-{self._frames:06d}.png")

    def set_brightness(self, n: int) -> None:
        self._brightness = n

    def close(self) -> None:
        self._open = False

    def state(self) -> SinkStatus:
        return SinkStatus(SinkState.CONNECTED, self._last_error, self._frames)

    def _atomic_save(self, image: Image.Image, target: Path) -> None:
        temp = target.with_name(target.name + ".tmp")
        try:
            image.convert("RGB").save(temp, format="PNG")
            os.replace(temp, target)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise


class NullSink:
    """Accept frames for testing and discard them."""

    def __init__(self) -> None:
        self._frames = 0
        self._brightness = 50
        self._last_error: str | None = None
        self._open = False

    def open(self) -> None:
        self._open = True

    def send(self, frame: Any) -> None:
        self._frames += 1

    def set_brightness(self, n: int) -> None:
        self._brightness = n

    def close(self) -> None:
        self._open = False

    def state(self) -> SinkStatus:
        return SinkStatus(SinkState.CONNECTED, self._last_error, self._frames)


class HidSink:
    """Drive the real panel, recovering from the intermittent write stall (T028).

    Hardware-measured policy (T129, 2026-10-09): the panel intermittently stops
    accepting OUT reports after 30-600 frames with **no USB disconnect** and
    independent of rate and byte order, and reopening the handle recovers it on
    the **first** attempt every time. So the reopen is triggered by the *first*
    mid-stream write failure, not after a 3-failure threshold — a threshold would
    burn the stall window on retries that cannot succeed on the dead handle.

    ``panel_unresponsive`` with a power-cycle hint is reported only when the
    *open* keeps failing (more than 60 s of continuous failure); a mid-stream
    stall that reopens successfully recovers immediately.

    Args:
        brightness: Initial backlight brightness (0-100), reapplied on every open.
        clock: Monotonic clock for the backoff and the unresponsive window.
        log: Optional structured logger.
        open_device: Callable returning an open device; defaults to
            ``panel_driver.device.open_device`` (tests inject a fake).
        backoff: Retry delays in seconds after successive open failures.
        unresponsive_after_s: Continuous-open-failure window before the
            power-cycle hint.
    """

    def __init__(
        self,
        *,
        brightness: int = 50,
        clock: Any = None,
        log: Any = None,
        open_device: Any = None,
        backoff: tuple[float, ...] = DEFAULT_BACKOFF,
        unresponsive_after_s: float = UNRESPONSIVE_AFTER_S,
        init_display: Any = None,
        write_frame: Any = None,
        set_backlight: Any = None,
    ) -> None:
        validate_brightness(brightness)
        self._brightness = brightness
        self._clock = clock if clock is not None else SystemClock()
        self._log = log
        self._open_device = open_device if open_device is not None else _device.open_device
        self._init_display = init_display if init_display is not None else _device.init_display
        self._write_frame = write_frame if write_frame is not None else _device.write_frame
        self._set_backlight = set_backlight if set_backlight is not None else _device.set_backlight
        self._backoff = tuple(backoff) or DEFAULT_BACKOFF
        self._unresponsive_after_s = unresponsive_after_s

        self._dev: Any = None
        self._state = SinkState.DISCONNECTED
        self._last_error: str | None = None
        self._last_open_error: str | None = None
        self._frames = 0
        self._attempt: int | None = None
        self._next_attempt_at = 0.0
        self._failure_since: float | None = None

    # ------------------------------------------------------------------ driving

    def open(self) -> None:
        """Open the panel if needed, honoring the retry backoff (never raises)."""
        if self._dev is not None and self._state == SinkState.CONNECTED:
            return
        now = self._clock.monotonic()
        if now < self._next_attempt_at:
            return
        self._attempt_open(now)

    def send(self, packets: Any) -> None:
        """Write one frame, reopening on the first mid-stream write failure."""
        if self._dev is None:
            self.open()
        if self._dev is None:
            return  # still down; the frame is dropped and the runner retries
        try:
            self._write_frame(self._dev, packets)
            self._frames += 1
        except Exception as exc:
            self._last_error = str(exc)
            self._reopen(str(exc))
            if self._dev is not None:
                try:
                    self._write_frame(self._dev, packets)
                    self._frames += 1
                except Exception as retry_exc:
                    self._last_error = str(retry_exc)

    def set_brightness(self, n: int) -> None:
        """Validate and apply brightness, reapplying it on the open handle."""
        validate_brightness(n)
        self._brightness = n
        if self._dev is not None:
            try:
                self._set_backlight(self._dev, n)
                self._last_error = None
            except Exception as exc:
                self._last_error = str(exc)

    def close(self) -> None:
        """Release the handle and report disconnected."""
        self._close_device()
        self._state = SinkState.DISCONNECTED

    def state(self) -> SinkStatus:
        return SinkStatus(self._state, self._last_error, self._frames)

    # --------------------------------------------------------------- internals

    def _attempt_open(self, now: float) -> None:
        try:
            device = self._open_device()
            # CRTDIS + CRTLIG: the init sequence is required before any frame, and
            # re-sending it on every (re)open also reapplies the brightness.
            self._init_display(device, self._brightness)
            self._dev = device
            self._state = SinkState.CONNECTED
            self._last_error = None
            self._attempt = None
            self._failure_since = None
        except Exception as exc:
            self._dev = None
            self._state = SinkState.DISCONNECTED
            self._last_open_error = str(exc)
            self._last_error = str(exc)
            if self._failure_since is None:
                self._failure_since = now
            self._schedule_next(now)
            self._maybe_unresponsive(now)

    def _reopen(self, why: str) -> None:
        """Close the dead handle and try to reopen immediately (T129 policy)."""
        self._close_device()
        self._state = SinkState.DISCONNECTED
        now = self._clock.monotonic()
        if self._failure_since is None:
            self._failure_since = now
        self._next_attempt_at = now  # attempt on the first failure, no backoff wait
        if self._log is not None:
            self._log.warning("panel_reopen", error=why)
        self._attempt_open(now)

    def _close_device(self) -> None:
        device, self._dev = self._dev, None
        if device is not None:
            try:
                close = getattr(device, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass

    def _schedule_next(self, now: float) -> None:
        index = self._attempt if self._attempt is not None else 0
        delay = self._backoff[min(index, len(self._backoff) - 1)]
        self._next_attempt_at = now + delay
        self._attempt = index + 1

    def _maybe_unresponsive(self, now: float) -> None:
        if self._failure_since is None:
            return
        elapsed = now - self._failure_since
        if elapsed > self._unresponsive_after_s:
            self._state = SinkState.UNRESPONSIVE
            self._last_error = (
                f"{self._last_open_error} (panel unresponsive after {elapsed:.0f}s of "
                "failed opens; power-cycle the panel)"
            )
