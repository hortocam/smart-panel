"""Crawl rendering: one pre-rendered strip and a moving window (T086, US3).

``research.md`` decision 14: the text strip is pre-rendered once per item set and
a window is cropped from it every frame, so a frame costs one paste rather than a
text layout. ``FR-025``/``FR-026`` require a seamless horizontal loop; the position
is derived from ``frame.now`` (never from a frame count), so a dropped frame
cannot make the ticker jump (``plugin-sdk.md``: "time-based animation must use
``frame.now``").

Two more contract details live here: an item set is swapped **only at the loop
seam** (a new headline never appears mid-scroll), and text is never drawn below
``frame.min_text_px``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from PIL import Image as PILImage
from PIL import ImageDraw

from smart_panel.plugins.crawl.model import (
    DEFAULT_SEPARATOR,
    SPEED_DEFAULT,
    clean_text,
    strip_control,
)

__all__ = ["CrawlRenderer"]

_NAMED_COLOURS: dict[str, tuple[int, int, int, int]] = {
    "black": (0, 0, 0, 255),
    "white": (255, 255, 255, 255),
    "red": (255, 0, 0, 255),
    "green": (0, 128, 0, 255),
    "blue": (0, 0, 255, 255),
    "yellow": (255, 255, 0, 255),
    "orange": (255, 165, 0, 255),
    "purple": (128, 0, 128, 255),
    "grey": (128, 128, 128, 255),
    "gray": (128, 128, 128, 255),
    "cyan": (0, 255, 255, 255),
    "magenta": (255, 0, 255, 255),
    "transparent": (0, 0, 0, 0),
}


def _rgba(value: Any, fallback: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Parse a validated colour value as RGBA."""
    text = str(value).strip()
    if text.startswith("#"):
        digits = text[1:]
        try:
            if len(digits) == 6:
                return (
                    int(digits[0:2], 16),
                    int(digits[2:4], 16),
                    int(digits[4:6], 16),
                    255,
                )
            if len(digits) == 8:
                return (
                    int(digits[0:2], 16),
                    int(digits[2:4], 16),
                    int(digits[4:6], 16),
                    int(digits[6:8], 16),
                )
        except ValueError:
            pass
    return _NAMED_COLOURS.get(text.lower(), fallback)


def _font(frame: Any, name: str, size_px: int) -> Any:
    fonts = getattr(frame, "fonts", None)
    if fonts is not None and hasattr(fonts, "get"):
        return fonts.get(name, size_px)
    from PIL import ImageFont

    return ImageFont.load_default()


class CrawlRenderer:
    """A cached text strip and the window currently visible in it."""

    def __init__(self) -> None:
        self._key: tuple[Any, ...] | None = None
        self._strip: PILImage.Image | None = None
        self._width = 0
        self._speed = SPEED_DEFAULT
        self._epoch = 0.0
        self._pending: tuple[tuple[Any, ...], PILImage.Image, int, int, float] | None = None

    # ------------------------------------------------------------- inspection

    @property
    def strip_texts(self) -> tuple[str, ...]:
        """The item texts currently on the strip (the previously adopted set)."""
        if self._key is None:
            return ()
        return tuple(self._key[0])

    @property
    def strip_width(self) -> int:
        """The width in pixels of the pre-rendered strip (0 before the first render)."""
        return int(self._width)

    def invalidate(self) -> None:
        """Forget the cached strip (used on stop)."""
        self._key = None
        self._strip = None
        self._width = 0
        self._pending = None

    def next_seam(self, now: float) -> float:
        """The instant the current strip completes its next whole loop."""
        if self._strip is None or self._width <= 0 or self._speed <= 0:
            return float(now)
        travelled = (float(now) - self._epoch) * self._speed
        loops = math.ceil(travelled / self._width - 1e-9)
        if loops <= 0:
            loops = 1
        return self._epoch + loops * self._width / self._speed

    def placeholder_image(
        self, frame: Any, settings: Mapping[str, Any]
    ) -> PILImage.Image:
        """The static ``placeholder`` frame, shown while there were never any items."""
        width, height = int(frame.size[0]), int(frame.size[1])
        floor = int(getattr(frame, "min_text_px", 0) or 0)
        size_px = max(int(settings.get("size", 44) or 44), floor)
        font = _font(frame, str(settings.get("font", "DejaVuSans-Bold")), size_px)
        fg = _rgba(settings.get("fg", "#FFFFFF"), (255, 255, 255, 255))
        bg = _rgba(settings.get("bg", "#B00020"), (176, 0, 32, 255))
        return self._static(
            width, height, bg, fg, font, str(settings.get("placeholder") or "No headlines")
        )

    # ----------------------------------------------------------------- render

    def render(
        self, frame: Any, texts: Sequence[str], settings: Mapping[str, Any]
    ) -> PILImage.Image:
        """Return a ``frame.size`` image with the strip's window in it."""
        width, height = int(frame.size[0]), int(frame.size[1])
        floor = int(getattr(frame, "min_text_px", 0) or 0)
        size_px = max(int(settings.get("size", 44) or 44), floor)
        font_name = str(settings.get("font", "DejaVuSans-Bold"))
        font = _font(frame, font_name, size_px)
        fg = _rgba(settings.get("fg", "#FFFFFF"), (255, 255, 255, 255))
        bg = _rgba(settings.get("bg", "#B00020"), (176, 0, 32, 255))
        separator = str(settings.get("separator", DEFAULT_SEPARATOR))
        direction = str(settings.get("direction", "left"))
        speed = int(settings.get("speed_px_s", SPEED_DEFAULT) or SPEED_DEFAULT)

        clean = [
            text
            for text in (strip_control(str(item)).strip() for item in texts)
            if text
        ]
        if not clean:
            # Nothing to scroll: paint the region's background only. The placeholder
            # is a separate frame the plugin asks for (``placeholder_image``), drawn
            # only while the crawl has never had an item.
            return PILImage.new("RGBA", (width, height), bg)

        key = (tuple(clean), size_px, font_name, separator, direction, fg, bg)
        if self._key != key:
            strip, strip_width = self._build(clean, separator, font, fg)
            if self._strip is None:
                self._adopt(key, strip, strip_width, speed, float(frame.now))
            else:
                pending_at = float(frame.now)
                if self._pending is None or self._pending[0] != key:
                    self._pending = (key, strip, strip_width, speed, pending_at)

        pending = self._pending
        if pending is not None:
            seam = self._first_seam_after(pending[4])
            if float(frame.now) >= seam:
                self._adopt(pending[0], pending[1], pending[2], pending[3], seam)
                self._pending = None

        strip = self._strip
        assert strip is not None  # adopted above
        canvas = PILImage.new("RGBA", (width, height), bg)
        step = max(1, self._width)
        offset = ((float(frame.now) - self._epoch) * self._speed) % step
        y = max(0, (height - strip.height) // 2)
        # ``left`` scrolls the strip out to the left (paste from ``-offset``); ``right``
        # scrolls it out to the right (paste from ``offset - step``). Both tile by
        # ``step``, so the loop is seamless in either direction and the strip itself
        # is never mirrored (FR-026: the text stays readable).
        x = offset - step if direction == "right" else -offset
        while x < width:
            canvas.paste(strip, (int(x), int(y)), strip)
            x += step
        return canvas

    # -------------------------------------------------------------- internals

    def _adopt(
        self,
        key: tuple[Any, ...],
        strip: PILImage.Image,
        strip_width: int,
        speed: int,
        epoch: float,
    ) -> None:
        self._key = key
        self._strip = strip
        self._width = int(strip_width)
        self._speed = int(speed)
        self._epoch = float(epoch)

    def _first_seam_after(self, moment: float) -> float:
        """The first loop seam at or after ``moment`` for the current strip."""
        if self._width <= 0 or self._speed <= 0:
            return float(moment)
        travelled = (float(moment) - self._epoch) * self._speed
        loops = max(1, math.ceil(travelled / self._width - 1e-9))
        return self._epoch + loops * self._width / self._speed

    def _build(
        self,
        texts: Sequence[str],
        separator: str,
        font: Any,
        fg: tuple[int, int, int, int],
    ) -> tuple[PILImage.Image, int]:
        """Pre-render ``texts`` (joined by ``separator``) into one strip."""
        line = separator.join(texts) + separator
        probe = ImageDraw.Draw(PILImage.new("RGBA", (8, 8)))
        strip_width = max(1, int(math.ceil(probe.textlength(line, font=font))))
        ascent, descent = font.getmetrics()
        height = max(1, int(ascent + descent))
        strip = PILImage.new("RGBA", (strip_width, height), (0, 0, 0, 0))
        ImageDraw.Draw(strip).text((0, 0), line, font=font, fill=fg)
        return strip, strip_width

    def _static(
        self,
        width: int,
        height: int,
        bg: tuple[int, int, int, int],
        fg: tuple[int, int, int, int],
        font: Any,
        text: str,
    ) -> PILImage.Image:
        """The placeholder frame: one centred line, not scrolled."""
        canvas = PILImage.new("RGBA", (width, height), bg)
        line = clean_text(text)
        if not line:
            return canvas
        draw = ImageDraw.Draw(canvas)
        left, top, right, bottom = draw.textbbox((0, 0), line, font=font)
        draw.text(
            ((width - (right - left)) / 2, (height - (bottom - top)) / 2 - top),
            line,
            font=font,
            fill=fg,
        )
        return canvas
