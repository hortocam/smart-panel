"""Rotate, JPEG-encode with a quality ladder, and build frame packets (T026).

The wire frame (32-byte header + JPEG) must fit the big-endian uint16 length
field, so the JPEG may be at most 65,503 bytes (``research.md`` decision 2).
The encoder starts each frame at the quality that last fit, walks *down* the
ladder on overflow, and steps back *up* after a streak of small frames.
"""

from __future__ import annotations

import io
from typing import Any

from panel_driver.protocol import PREAMBLE_LEN, build_frame_packets
from panel_driver.rotation import to_panel_native

FRAME_LIMIT = 65535
MAX_JPEG_BYTES = FRAME_LIMIT - PREAMBLE_LEN  # 65,503
DEFAULT_START_QUALITY = 70
DEFAULT_MIN_QUALITY = 30
QUALITY_STEP = 10
DEFAULT_RECOVER_STREAK = 10


class FrameTooLarge(RuntimeError):
    """The frame would not fit even at the lowest quality on the ladder."""


def _build_ladder(start_quality: int, min_quality: int) -> tuple[int, ...]:
    if not 1 <= min_quality <= start_quality <= 100:
        raise ValueError(
            f"invalid quality range: start={start_quality}, min={min_quality}"
        )
    rungs = list(range(start_quality, min_quality - 1, -QUALITY_STEP))
    if rungs[-1] != min_quality:
        rungs.append(min_quality)
    return tuple(rungs)


class Encoder:
    """Stateful frame encoder that adapts JPEG quality to the size limit.

    Args:
        start_quality: First quality tried on a fresh encoder (config
            ``display.jpeg_quality.start``, default 70).
        min_quality: Lowest rung (config ``display.jpeg_quality.min``, default 30).
        recover_streak: Consecutive small frames before stepping one rung back
            up (default 10).
    """

    def __init__(
        self,
        start_quality: int = DEFAULT_START_QUALITY,
        min_quality: int = DEFAULT_MIN_QUALITY,
        recover_streak: int = DEFAULT_RECOVER_STREAK,
    ) -> None:
        self.ladder = _build_ladder(start_quality, min_quality)
        self._index = 0
        self._recover_streak = max(1, recover_streak)
        self._streak = 0
        self.last_quality: int | None = None
        self.last_jpeg_bytes: int | None = None

    def encode(self, canvas: Any) -> list[bytes]:
        """Encode a composed canvas into CRTDRA packets.

        Args:
            canvas: The composed 1920x462 image.

        Returns:
            A list of 1024-byte packets from
            :func:`panel_driver.protocol.build_frame_packets`.

        Raises:
            FrameTooLarge: The frame overflows even at the lowest rung. The
                caller keeps the last good frame and reports ``frame_too_large``.
        """
        previous_index = self._index
        data, quality = self._encode_fitting(canvas)
        self.last_quality = quality
        self.last_jpeg_bytes = len(data)
        self._update_quality(previous_index)
        return build_frame_packets(data)

    def _encode_fitting(self, canvas: Any) -> tuple[bytes, int]:
        panel = to_panel_native(canvas, clockwise=True).convert("RGB")
        for index in range(self._index, len(self.ladder)):
            quality = self.ladder[index]
            data = _encode_jpeg(panel, quality)
            if len(data) <= MAX_JPEG_BYTES:
                self._index = index
                return data, quality
        lowest = self.ladder[-1]
        raise FrameTooLarge(
            f"frame does not fit at the lowest quality {lowest} "
            f"(JPEG must be at most {MAX_JPEG_BYTES} bytes)"
        )

    def _update_quality(self, previous_index: int) -> None:
        if self._index > previous_index:
            # We stepped down to fit: reset the recovery streak.
            self._streak = 0
            return
        if self._index == 0:
            self._streak = 0
            return
        self._streak += 1
        if self._streak >= self._recover_streak:
            self._index -= 1
            self._streak = 0


def _encode_jpeg(image: Any, quality: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()
