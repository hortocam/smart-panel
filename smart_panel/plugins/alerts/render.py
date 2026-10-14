"""The alerts overlay image: severity colours, wrapped text, ``+N queued`` (T070).

The overlay covers exactly its target region (``data-model.md``: "The alert
covers the gauge area; sidebar and crawl stay visible"). Severity background
colours come from the ``colors`` setting (info ``#2E7DD7``, warning ``#E6A700``,
critical ``#D62839``), the text colour is chosen for contrast, the title and
message are wrapped at sizes 56 and 40, and neither is ever drawn below
``frame.min_text_px``. A ``+N queued`` badge appears in the corner while alerts
wait behind the displayed one.

Nothing is displayed when the manager has no current alert: the result is
``Inactive``, "draw nothing" — the region underneath stays visible. That is
deliberately different from a *region* plugin's empty state (see the note in
``contracts/builtin-plugins.md``): ``text set --text ""`` paints its own
background, because a region plugin owns its rectangle, whereas an overlay
draws nothing and lets the layer below show through.

The module imports only ``smart_panel.sdk`` and ``PIL`` (FR-043).
"""

from __future__ import annotations

from typing import Any

from PIL import Image as PILImage
from PIL import ImageDraw

from smart_panel.sdk.plugin import Image as RenderImage
from smart_panel.sdk.plugin import Inactive, Unchanged

__all__ = [
    "BADGE_SIZE",
    "PADDING",
    "SEVERITY_COLORS",
    "AlertRenderer",
    "rgba",
]

SEVERITY_COLORS = {"info": "#2E7DD7", "warning": "#E6A700", "critical": "#D62839"}
PADDING = 24
BADGE_SIZE = 28
BADGE_INSET = 12
_TITLE_FALLBACK = 56
_MESSAGE_FALLBACK = 40

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


def rgba(value: Any, fallback: tuple[int, int, int, int] = (0, 0, 0, 255)):
    """Parse a colour setting as RGBA (``#RRGGBB``, ``#RRGGBBAA`` or a name)."""
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
    return _NAMED.get(text.lower(), fallback)


def contrast_text(background: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Black or white, whichever reads better on ``background``."""
    luminance = 0.299 * background[0] + 0.587 * background[1] + 0.114 * background[2]
    return (0, 0, 0, 255) if luminance > 140 else (255, 255, 255, 255)


class AlertRenderer:
    """Draw the displayed alert; cache the image until something visible changes."""

    def __init__(self) -> None:
        self._key: tuple[Any, ...] | None = None
        self._image: PILImage.Image | None = None

    def invalidate(self) -> None:
        """Forget the cached image (a settings change, or a forced redraw)."""
        self._key = None

    def render(self, frame: Any, manager: Any, settings: dict[str, Any]) -> Any:
        """One render result for the overlay: ``Inactive``, ``Unchanged`` or ``Image``."""
        alert = manager.current()
        if alert is None:
            self._key = None
            self._image = None
            return Inactive()

        queued = len(manager.queued())
        colors = _colors(settings)
        title_size, message_size = _sizes(frame, settings)
        key = (
            tuple(frame.size),
            alert.id,
            alert.severity,
            alert.message,
            alert.title,
            queued,
            title_size,
            message_size,
            tuple(colors.items()),
            str(settings.get("font")),
            int(getattr(frame, "min_text_px", 0) or 0),
        )
        if self._key == key and self._image is not None:
            return Unchanged()

        image = self._draw(frame, alert, queued, settings, colors, title_size, message_size)
        self._key = key
        self._image = image
        return RenderImage(image)

    # ------------------------------------------------------------------ drawing

    def _draw(
        self,
        frame: Any,
        alert: Any,
        queued: int,
        settings: dict[str, Any],
        colors: dict[str, str],
        title_size: int,
        message_size: int,
    ) -> PILImage.Image:
        width, height = int(frame.size[0]), int(frame.size[1])
        background = rgba(colors.get(alert.severity), rgba(SEVERITY_COLORS["info"]))
        foreground = contrast_text(background)
        image = PILImage.new("RGBA", (width, height), background)
        draw = ImageDraw.Draw(image)
        font_name = str(settings.get("font") or "DejaVuSans")

        top = PADDING
        if alert.title:
            font = _font(frame, font_name, title_size)
            for line in _wrap(draw, alert.title, font, width - 2 * PADDING):
                draw.text((PADDING, top), line, font=font, fill=foreground)
                top += _line_height(draw, font, line)
            top += PADDING // 2

        font = _font(frame, font_name, message_size)
        for line in _wrap(draw, alert.message, font, width - 2 * PADDING):
            if top > height - PADDING:
                break
            draw.text((PADDING, top), line, font=font, fill=foreground)
            top += _line_height(draw, font, line)

        if queued > 0:
            _draw_badge(draw, width, height, queued, frame, font_name, foreground)
        return image


def _draw_badge(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    queued: int,
    frame: Any,
    font_name: str,
    foreground: tuple[int, int, int, int],
) -> None:
    size = max(int(getattr(frame, "min_text_px", 0) or 0), BADGE_SIZE)
    font = _font(frame, font_name, size)
    label = f"+{queued} queued"
    left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
    box_w = (right - left) + 2 * 10
    box_h = (bottom - top) + 2 * 6
    x0 = width - box_w - BADGE_INSET
    y0 = height - box_h - BADGE_INSET
    draw.rounded_rectangle(
        [x0, y0, x0 + box_w, y0 + box_h],
        radius=6,
        fill=(0, 0, 0, 180),
        outline=foreground,
        width=2,
    )
    draw.text((x0 + 10 - left, y0 + 6 - top), label, font=font, fill=foreground)


def _colors(settings: dict[str, Any]) -> dict[str, str]:
    configured = settings.get("colors")
    colors = dict(SEVERITY_COLORS)
    if isinstance(configured, dict):
        for severity, value in configured.items():
            if severity in SEVERITY_COLORS and isinstance(value, str):
                colors[severity] = value
    return colors


def _sizes(frame: Any, settings: dict[str, Any]) -> tuple[int, int]:
    floor = int(getattr(frame, "min_text_px", 0) or 0)
    title = int(settings.get("title_size") or _TITLE_FALLBACK)
    message = int(settings.get("message_size") or _MESSAGE_FALLBACK)
    if floor > 0:
        title = max(title, floor)
        message = max(message, floor)
    return title, message


def _font(frame: Any, name: str, size: int) -> Any:
    fonts = getattr(frame, "fonts", None)
    if fonts is not None and hasattr(fonts, "get"):
        return fonts.get(name, size)
    from PIL import ImageFont

    return ImageFont.load_default()  # pragma: no cover - the runner always provides fonts


def _line_height(draw: ImageDraw.ImageDraw, font: Any, text: str) -> int:
    left, top, right, bottom = draw.textbbox((0, 0), text or "Ag", font=font)
    return max(1, int(bottom - top)) * 3 // 2


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: Any, max_width: int) -> list[str]:
    """Greedy word wrap so a long message stays inside the region."""
    if max_width <= 0:
        return []
    lines: list[str] = []
    current = ""
    for word in str(text).split():
        candidate = f"{current} {word}".strip()
        width = draw.textbbox((0, 0), candidate, font=font)[2]
        if current and width > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current or not lines:
        lines.append(current)
    return lines
