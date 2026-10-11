"""Frame sinks: where a composed frame goes (T027).

``PanelSink`` is the protocol; ``HidSink`` (the real panel, T028) is on another
card and lives in this module. This card implements the no-panel sinks:

* ``FileSink`` writes the composed canvas to ``latest.png`` atomically (temp
  file + ``os.replace``), so a reader never sees a half-written frame, and
  numbered ``frame-NNNNNN.png`` files when asked.
* ``NullSink`` accepts frames and discards them.

The protocol's ``send`` carries one frame in the sink's native form: the list
of 1024-byte HID packets for the panel sink, or the composed canvas image for a
visual sink. A panel sink is fed encoded packets because the encoder owns the
rotation and size fitting; a visual sink is fed the canvas directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from PIL import Image


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
