"""Sidebar rendering: an icon, a label and a count chip per entry (T107, US5).

``research.md`` decision 13: Pillow only. Each entry row is drawn from its entry
and its current reading, and the whole region image is cached until something
visible changes — so a static sidebar costs one composite, not one rasterisation
per frame.

What one row shows:

* the icon at the left, resolved through ``frame.icons`` (``core/icons.py``); an
  unusable icon falls back to the generic icon and is recorded in
  :attr:`SidebarRenderer.fallbacks`, which the plugin lists in ``status`` (the
  entry is dropped again when the icon resolves — a cached region re-checks its
  fallbacks on the next frame — so ``status`` names only the icons that are
  unusable now);
* the label (at most 12 characters, truncated to fit) next to it, and
* a count chip on the right: ``chip_bg``/``chip_fg`` by default, the missed
  alert's severity colour for the built-in alert entry, ``?`` while the count is
  stale or unknown, hidden at zero when ``hide_when_zero`` is set.

A count that *changes* gets a brief highlight (``HIGHLIGHT_S`` seconds of the
frame's clock) so an eye watching the panel notices it (US5 scenario 2). Text is
never drawn below ``frame.min_text_px``: ``label_size`` and ``chip_size`` are
clamped up to it.

Only ``smart_panel.sdk``, ``PIL`` and this package are imported (FR-043).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from PIL import Image as PILImage
from PIL import ImageDraw

from smart_panel.plugins.sidebar.model import (
    ALERT_ENTRY_ID,
    SEVERITY_COLORS,
    STALE_TEXT,
    SidebarEntry,
    entry_height,
)
from smart_panel.sdk.plugin import Image as RenderImage
from smart_panel.sdk.plugin import Unchanged

__all__ = [
    "DEFAULT_BG",
    "HIGHLIGHT_COLOR",
    "HIGHLIGHT_S",
    "LABEL_COLOR",
    "SidebarReading",
    "SidebarRenderer",
    "chip_text",
    "contrast_text",
    "icon_box",
    "rgba",
]

HIGHLIGHT_S = 0.8
HIGHLIGHT_COLOR = (255, 209, 102, 255)  # #FFD166
LABEL_COLOR = (154, 165, 177, 255)
DEFAULT_BG = (11, 14, 17, 255)  # #0B0E11
DEFAULT_CHIP_FG = (255, 255, 255, 255)  # #FFFFFF
DEFAULT_CHIP_BG = (214, 40, 57, 255)  # #D62839
GENERIC_COLOUR = (120, 130, 140, 255)

_ICON_MIN_PX = 16
_ICON_MAX_PX = 64
_ROW_MIN_PX = 8
_MISSING = object()

# The CSS-style colour names the SDK's ``color`` setting accepts, as RGBA.
_NAMED: dict[str, tuple[int, int, int, int]] = {
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


@dataclass(frozen=True)
class SidebarReading:
    """What one entry shows this frame.

    Attributes:
        count: The current count, or ``None`` when the topic is unknown.
        stale: True when the topic is stale or was never published.
        severity: ``max_severity`` of the missed alerts (the alert entry only).
    """

    count: int | None = None
    stale: bool = False
    severity: str | None = None


def rgba(value: Any, fallback: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Parse a colour setting (``#RRGGBB``, ``#RRGGBBAA`` or a name) as RGBA."""
    if isinstance(value, (tuple, list)) and len(value) in (3, 4):
        channels = [int(channel) for channel in value]
        while len(channels) < 4:
            channels.append(255)
        return (channels[0], channels[1], channels[2], channels[3])
    text = str(value).strip()
    if text.startswith("#"):
        digits = text[1:]
        try:
            if len(digits) == 6:
                return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16), 255)
            if len(digits) == 8:
                return (
                    int(digits[0:2], 16),
                    int(digits[2:4], 16),
                    int(digits[4:6], 16),
                    int(digits[6:8], 16),
                )
        except ValueError:
            pass
    return _NAMED.get(text.lower(), fallback)


def contrast_text(background: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Black or white, whichever reads better on ``background``."""
    luminance = 0.299 * background[0] + 0.587 * background[1] + 0.114 * background[2]
    return (0, 0, 0, 255) if luminance > 140 else (255, 255, 255, 255)


def chip_text(entry: SidebarEntry, reading: SidebarReading) -> str | None:
    """The chip's text, or ``None`` when the chip is hidden (FR-040).

    ``?`` is the stale/unknown state; the built-in alert entry is hidden at zero
    and never shows ``?`` (its topic has no TTL and, when it has never been
    published, the count is simply zero).
    """
    if entry.id == ALERT_ENTRY_ID:
        if reading.count is None or reading.count <= 0:
            return None
        return entry.display_count(reading.count)
    if reading.stale or reading.count is None:
        return STALE_TEXT
    if reading.count == 0 and entry.hide_when_zero:
        return None
    return entry.display_count(reading.count)


def icon_box(index: int, entry_count: int, size: tuple[int, int]) -> tuple[int, int, int]:
    """The ``(left, top, pixels)`` square an entry's icon is drawn into."""
    height = int(size[1])
    width = int(size[0])
    row = entry_height(entry_count, height)
    pad = max(4, row // 12)
    pixels = max(_ICON_MIN_PX, min(row - 2 * pad, _ICON_MAX_PX, width))
    top = index * row + (row - pixels) // 2
    return pad, max(0, top), pixels


class SidebarRenderer:
    """Draw the sidebar into one region-sized image, cached until it changes.

    The cache is not blindly keyed on the visible content: a cached frame re-checks
    the icons it recorded as unusable (see :meth:`_refresh_fallbacks`), so a file
    repaired on disk is picked up — and its warning dropped — on the next frame rather
    than at the next rebuild.
    """

    def __init__(self) -> None:
        self._key: tuple[Any, ...] | None = None
        self._image: PILImage.Image | None = None
        self._last_count: dict[str, Any] = {}
        self._highlight_until: dict[str, float] = {}
        self.fallbacks: dict[str, str] = {}
        """Icons that fell back to the generic image this run (name -> reason).

        Cleared per icon when it resolves again — on a rebuild *or* by the cached
        frame's fallback re-check — so it names only the icons that are unusable now.
        """
        self.builds = 0
        """How many region images have been rasterised (a cache-miss counter)."""

    def forget(self) -> None:
        """Drop the cached image (a settings change or a stop)."""
        self._key = None
        self._image = None

    def _refresh_fallbacks(
        self, frame: Any, rows: Sequence[tuple[SidebarEntry, SidebarReading]]
    ) -> bool:
        """Re-resolve the icons this region currently records as unusable.

        A cached region skips :meth:`_draw`, and with it the icon resolution, so a file
        repaired on disk would otherwise keep the generic icon — and its ``status``
        warning — until something else forced a rebuild. Only the icons already in
        :attr:`fallbacks` are probed here, so a healthy region does no extra work, and a
        cached *failure* is re-read on every lookup by design anyway.

        Returns:
            True when at least one of them resolves again, so the caller rebuilds the
            region (which redraws the real icon and drops the warning).
        """
        icons = getattr(frame, "icons", None)
        getter = getattr(icons, "get_with_status", None)
        if not callable(getter) or not self.fallbacks:
            return False
        size = (int(frame.size[0]), int(frame.size[1]))
        changed = False
        for index, (entry, _reading) in enumerate(rows):
            name = str(entry.icon)
            if name not in self.fallbacks:
                continue
            _left, _top, pixels = icon_box(index, len(rows), size)
            try:
                _image, ok = getter(name, pixels)
            except Exception:  # a broken registry must not break a frame
                continue
            if ok:
                self.fallbacks.pop(name, None)
                changed = True
        return changed

    # ----------------------------------------------------------------- render

    def render(
        self,
        frame: Any,
        entries: Sequence[SidebarEntry],
        readings: Sequence[SidebarReading],
        settings: Mapping[str, Any],
    ) -> Any:
        """One render result for the region: ``Unchanged`` or ``Image``.

        The region key covers the entry fields and readings the rows draw, so an
        unchanged key normally means ``Unchanged``. It does not cover the *icon files*
        themselves: the recorded fallbacks are re-checked before the cache is trusted,
        so an icon that recovered is redrawn at once (see :meth:`_refresh_fallbacks`),
        while an icon file replaced, repaired differently or corrupted *after* a
        successful draw is re-resolved at the region's next rebuild — any visible
        change, a settings change or a restart (and see ``core/icons.py`` on the stamp's
        limits).
        """
        rows = list(zip(entries, readings, strict=False))
        now = float(getattr(frame, "now", 0.0) or 0.0)
        highlighted: list[bool] = []
        for entry, reading in rows:
            previous = self._last_count.get(entry.id, _MISSING)
            if previous is not _MISSING and previous != reading.count:
                self._highlight_until[entry.id] = now + HIGHLIGHT_S
            self._last_count[entry.id] = reading.count
            highlighted.append(now < self._highlight_until.get(entry.id, float("-inf")))

        size = (int(frame.size[0]), int(frame.size[1]))
        key = (
            size,
            tuple(
                (
                    entry.id,
                    entry.icon,
                    entry.label,
                    entry.hide_when_zero,
                    entry.max_display,
                    reading.count,
                    reading.stale,
                    reading.severity,
                    chip_text(entry, reading),
                    highlight,
                )
                for (entry, reading), highlight in zip(rows, highlighted, strict=False)
            ),
            tuple(_text_sizes(frame, settings)),
            str(settings.get("bg")),
            str(settings.get("chip_fg")),
            str(settings.get("chip_bg")),
            str(settings.get("font")),
            int(getattr(frame, "min_text_px", 0) or 0),
        )
        if (
            self._key == key
            and self._image is not None
            and not self._refresh_fallbacks(frame, rows)
        ):
            return Unchanged()

        image = self._draw(frame, rows, highlighted, settings)
        self._key = key
        self._image = image
        self.builds += 1
        return RenderImage(image)

    # ------------------------------------------------------------------ drawing

    def _draw(
        self,
        frame: Any,
        rows: Sequence[tuple[SidebarEntry, SidebarReading]],
        highlighted: Sequence[bool],
        settings: Mapping[str, Any],
    ) -> PILImage.Image:
        width, height = int(frame.size[0]), int(frame.size[1])
        background = rgba(settings.get("bg"), DEFAULT_BG)
        canvas = PILImage.new("RGBA", (width, height), background)
        draw = ImageDraw.Draw(canvas)
        label_size, chip_size = _text_sizes(frame, settings)
        chip_fg = rgba(settings.get("chip_fg"), DEFAULT_CHIP_FG)
        chip_bg = rgba(settings.get("chip_bg"), DEFAULT_CHIP_BG)
        font_name = str(settings.get("font") or "DejaVuSans")
        label_font = _font(frame, font_name, label_size)
        chip_font = _font(frame, font_name, chip_size)
        row = entry_height(len(rows), height)

        for index, (entry, reading) in enumerate(rows):
            top = index * row
            left, icon_top, pixels = icon_box(index, len(rows), (width, height))
            icon = _icon(frame, entry.icon, pixels, self)
            if icon_top + pixels <= height:
                canvas.paste(icon, (left, icon_top), icon)
            else:  # pragma: no cover - a clipped region keeps the icon inside
                canvas.paste(icon, (left, max(0, height - pixels)), icon)

            text = chip_text(entry, reading)
            chip_right = width - max(4, row // 12)
            if text is not None:
                chip_right = self._draw_chip(
                    draw,
                    entry,
                    reading,
                    text,
                    chip_font,
                    chip_fg,
                    chip_bg,
                    highlighted[index],
                    right=chip_right,
                    top=top,
                    row=row,
                    chip_size=chip_size,
                )

            if entry.label:
                gap = max(4, row // 12)
                label_left = left + pixels + gap
                label_right = chip_right - gap
                if label_right - label_left >= label_size:
                    label = _fit(draw, entry.label, label_font, label_right - label_left)
                    _centred_text(
                        draw,
                        (float(label_left), float(top), float(label_right), float(top + row)),
                        label,
                        label_font,
                        LABEL_COLOR,
                    )
        return canvas

    @staticmethod
    def _draw_chip(
        draw: ImageDraw.ImageDraw,
        entry: SidebarEntry,
        reading: SidebarReading,
        text: str,
        font: Any,
        chip_fg: tuple[int, int, int, int],
        chip_bg: tuple[int, int, int, int],
        highlight: bool,
        *,
        right: int,
        top: int,
        row: int,
        chip_size: int,
    ) -> int:
        """Draw one count chip; return the x it starts at (for the label)."""
        background = chip_bg
        foreground = chip_fg
        severity = reading.severity if entry.id == ALERT_ENTRY_ID else None
        if severity in SEVERITY_COLORS:
            background = rgba(SEVERITY_COLORS[severity], chip_bg)
            foreground = contrast_text(background)
        pad_x = max(4, chip_size // 3)
        pad_y = max(2, chip_size // 5)
        text_width = _text_width(draw, text, font)
        text_height = _text_height(draw, text, font)
        box_w = text_width + 2 * pad_x
        box_h = text_height + 2 * pad_y
        x0 = max(0, right - box_w)
        y0 = top + max(0, (row - box_h) // 2)
        box = (float(x0), float(y0), float(x0 + box_w), float(y0 + box_h))
        radius = max(2, box_h // 3)
        draw.rounded_rectangle(box, radius=radius, fill=background)
        if highlight:
            draw.rounded_rectangle(box, radius=radius, outline=HIGHLIGHT_COLOR, width=2)
        _centred_text(draw, box, text, font, foreground)
        return x0


# ------------------------------------------------------------------- helpers


def _text_sizes(frame: Any, settings: Mapping[str, Any]) -> tuple[int, int]:
    """``(label_size, chip_size)``, never below ``frame.min_text_px``."""
    floor = int(getattr(frame, "min_text_px", 0) or 0)
    label = max(int(settings.get("label_size") or 24), floor)
    chip = max(int(settings.get("chip_size") or 24), floor)
    return label, chip


def _font(frame: Any, name: str, size: int) -> Any:
    """Resolve a font through ``frame.fonts`` (the harness and runner provide one)."""
    fonts = getattr(frame, "fonts", None)
    if fonts is not None and hasattr(fonts, "get"):
        return fonts.get(name, size)
    from PIL import ImageFont

    return ImageFont.load_default()  # pragma: no cover - the runner always provides fonts


def _icon(frame: Any, name: str, pixels: int, renderer: SidebarRenderer) -> PILImage.Image:
    """The icon image for ``name``, recording (or clearing) a generic fallback.

    An icon that resolves again drops its :attr:`SidebarRenderer.fallbacks` entry,
    so the sidebar lists only the icons that are unusable now. A cached region never
    reaches here: :meth:`SidebarRenderer._refresh_fallbacks` covers that case.
    """
    icons = getattr(frame, "icons", None)
    if icons is not None:
        getter = getattr(icons, "get_with_status", None)
        if callable(getter):
            try:
                image, ok = getter(name, pixels)
                if ok:
                    renderer.fallbacks.pop(str(name), None)
                else:
                    renderer.fallbacks[str(name)] = "unusable icon; drew the generic icon"
                return image
            except Exception as exc:  # a broken registry must not break a frame
                renderer.fallbacks[str(name)] = f"{type(exc).__name__}: {exc}"
                return _placeholder(pixels)
        getter = getattr(icons, "get", None)
        if callable(getter):
            try:
                return getter(name, pixels)
            except Exception as exc:  # pragma: no cover - defensive
                renderer.fallbacks[str(name)] = f"{type(exc).__name__}: {exc}"
                return _placeholder(pixels)
    return _placeholder(pixels)


def _placeholder(pixels: int) -> PILImage.Image:
    """A rounded-rect outline, drawn when no icon registry is wired at all."""
    image = PILImage.new("RGBA", (pixels, pixels), (0, 0, 0, 0))
    inset = max(1, pixels // 8)
    ImageDraw.Draw(image).rounded_rectangle(
        [inset, inset, pixels - 1 - inset, pixels - 1 - inset],
        radius=max(1, pixels // 8),
        outline=GENERIC_COLOUR,
        width=max(1, pixels // 16),
    )
    return image


def _fit(draw: ImageDraw.ImageDraw, text: str, font: Any, max_width: int) -> str:
    """Truncate ``text`` with an ellipsis so it fits ``max_width`` pixels."""
    if max_width <= 0 or not text:
        return ""
    if _text_width(draw, text, font) <= max_width:
        return text
    trimmed = text
    while trimmed and _text_width(draw, f"{trimmed}…", font) > max_width:
        trimmed = trimmed[:-1]
    return f"{trimmed}…" if trimmed else ""


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: Any) -> int:
    left, _top, right, _bottom = draw.textbbox((0, 0), text or "0", font=font)
    return max(1, int(round(right - left)))


def _text_height(draw: ImageDraw.ImageDraw, text: str, font: Any) -> int:
    _left, top, _right, bottom = draw.textbbox((0, 0), text or "0", font=font)
    return max(1, int(round(bottom - top)))


def _centred_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[float, float, float, float],
    text: str,
    font: Any,
    fill: tuple[int, int, int, int],
) -> None:
    """Draw ``text`` centred in ``box`` (no-op for empty text)."""
    if not text:
        return
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    width, height = right - left, bottom - top
    x = box[0] + (box[2] - box[0] - width) / 2 - left
    y = box[1] + (box[3] - box[1] - height) / 2 - top
    draw.text((x, y), text, font=font, fill=fill)
