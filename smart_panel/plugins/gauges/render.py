"""Gauge rendering: a grid of per-gauge cached layers (T098, US4).

``research.md`` decision 13: Pillow only, and gauge dials (and other shapes) are
drawn at **3x** and downsampled for antialiasing, with each gauge's layer cached
until its value, thresholds or stale state change. This module owns that cache;
the plugin owns the data.

The grid comes from the gauge count (``model.grid_for``: 1x1 up to 3x2, or an
explicit ``{cols, rows}`` layout). Text is never drawn below ``min_text_px``:
``value_size`` and ``label_size`` are clamped up to ``frame.min_text_px``.

A stale or never-published reading is one visual state (FR-036): the value is
dimmed toward the background and the digits are replaced by ``no data``, so a
gauge never shows a plausible-looking old number.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from PIL import Image as PILImage
from PIL import ImageDraw

from smart_panel.plugins.gauges.model import (
    NO_DATA_TEXT,
    Gauge,
    grid_for,
)

__all__ = [
    "GaugeReader",
    "GaugeRenderer",
    "LABEL_COLOR",
    "STALE_DIM",
    "TRACK_COLOR",
    "VALUE_COLOR",
    "dim",
]

VALUE_COLOR = (255, 255, 255, 255)
LABEL_COLOR = (154, 165, 177, 255)
TRACK_COLOR = (60, 69, 78, 255)
STALE_DIM = 0.5

_DEFAULT_BG = (16, 20, 24, 255)
_SUPERSAMPLE = 3
_SPARKLINE_POINTS = 60
_DIAL_START_DEG = 135.0
_DIAL_SWEEP_DEG = 270.0
_MIN_SHAPE_PX = 8

# The CSS-style colour names the SDK's ``color`` setting accepts, as RGBA.
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

_Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class GaugeReader:
    """What one gauge cell shows this frame.

    Attributes:
        value: The aggregated value, or ``None`` when nothing is in the window.
        stale: True when the source stopped reporting (FR-036) or the bus says
            the topic is stale or was never set.
        history: The recent samples for a ``sparkline``, oldest first.
    """

    value: float | None = None
    stale: bool = False
    history: tuple[float, ...] = field(default=())


def dim(
    color: tuple[int, int, int, int],
    background: tuple[int, int, int, int],
    factor: float = STALE_DIM,
) -> tuple[int, int, int, int]:
    """Blend ``color`` ``factor`` of the way toward ``background``."""
    share = min(1.0, max(0.0, float(factor)))
    return tuple(
        int(round(channel * (1.0 - share) + base * share))
        for channel, base in zip(color, background, strict=True)
    )  # type: ignore[return-value]


def _rgba(value: Any, fallback: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Parse a validated colour value (or an RGBA tuple) as RGBA."""
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
    """Resolve a font through ``frame.fonts`` (the harness and runner provide one)."""
    fonts = getattr(frame, "fonts", None)
    if fonts is not None and hasattr(fonts, "get"):
        return fonts.get(name, size_px)
    from PIL import ImageFont

    return ImageFont.load_default()


class GaugeRenderer:
    """Draw the gauges into one region-sized image, caching each gauge's layer."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[tuple[Any, ...], PILImage.Image]] = {}
        self.builds = 0
        """How many gauge layers have been rasterised (a cache-miss counter)."""

    def forget(self) -> None:
        """Drop every cached layer (used on stop and on a layout change)."""
        self._cache.clear()

    # ----------------------------------------------------------------- render

    def render(
        self,
        frame: Any,
        gauges: Sequence[Gauge],
        readings: Sequence[GaugeReader],
        settings: Mapping[str, Any],
    ) -> PILImage.Image:
        """Return a ``frame.size`` image with every gauge in its grid cell."""
        width, height = int(frame.size[0]), int(frame.size[1])
        background = _rgba(settings.get("bg", "#101418"), _DEFAULT_BG)
        canvas = PILImage.new("RGBA", (width, height), background)
        if not gauges:
            return canvas

        columns, rows = grid_for(len(gauges), settings.get("layout", "auto"))
        cell_width = max(1, width // columns)
        cell_height = max(1, height // rows)
        floor = int(getattr(frame, "min_text_px", 0) or 0)
        value_size = max(int(settings.get("value_size") or 72), floor)
        label_size = max(int(settings.get("label_size") or 28), floor)
        font_name = str(settings.get("font") or "DejaVuSans-Bold")

        for index, gauge in enumerate(gauges):
            reader = readings[index] if index < len(readings) else GaugeReader()
            state: tuple[float | None, bool]
            history: tuple[float, ...]
            if reader.stale or reader.value is None:
                # One visual state for stale and for no data (FR-036): the layer
                # is keyed by the state, so the two share a cache entry.
                state, history = (None, True), ()
            else:
                state = (float(reader.value), False)
                history = tuple(
                    round(float(point), 3) for point in reader.history
                )[-_SPARKLINE_POINTS:]
            key = (
                cell_width,
                cell_height,
                value_size,
                label_size,
                font_name,
                background,
                state,
                history,
                gauge.label,
                gauge.unit,
                gauge.style,
                gauge.range,
                tuple((threshold.at, threshold.color) for threshold in gauge.thresholds),
            )
            entry = self._cache.get(gauge.id)
            if entry is None or entry[0] != key:
                layer = self._build(
                    frame,
                    gauge,
                    state,
                    history,
                    cell_width,
                    cell_height,
                    value_size,
                    label_size,
                    font_name,
                    background,
                )
                self._cache[gauge.id] = (key, layer)
                self.builds += 1
            else:
                layer = entry[1]
            column, row = index % columns, index // columns
            canvas.paste(layer, (column * cell_width, row * cell_height))
        return canvas

    # ------------------------------------------------------------------ layer

    def _build(
        self,
        frame: Any,
        gauge: Gauge,
        state: tuple[float | None, bool],
        history: Sequence[float],
        width: int,
        height: int,
        value_size: int,
        label_size: int,
        font_name: str,
        background: tuple[int, int, int, int],
    ) -> PILImage.Image:
        """Rasterise one gauge's cell (label, shape and value)."""
        value, stale = state
        no_data = stale or value is None
        layer = PILImage.new("RGBA", (width, height), background)
        pad = max(2, min(width, height) // 24)
        label_font = _font(frame, font_name, label_size)
        value_font = _font(frame, font_name, value_size)
        draw = ImageDraw.Draw(layer)

        top = float(pad)
        if gauge.label:
            _centred_text(
                draw,
                (float(pad), top, float(width - pad), top + label_size * 1.6),
                gauge.label,
                label_font,
                LABEL_COLOR,
            )
            top += label_size * 1.6
        body: _Box = (
            float(pad),
            top,
            float(max(pad + 1, width - pad)),
            float(max(top + 1, height - pad)),
        )

        value_color = dim(VALUE_COLOR, background) if no_data else self._value_color(gauge, value)
        if gauge.style == "dial":
            self._dial(layer, gauge, body, value, no_data, value_color)
        elif gauge.style == "bar":
            self._bar(layer, gauge, body, value, no_data, value_color)
        elif gauge.style == "sparkline":
            self._sparkline(layer, body, history, gauge, value_color, no_data)

        text = NO_DATA_TEXT if no_data else gauge.format_value(value)
        if gauge.style == "sparkline":
            inner: _Box = (body[0], body[1], body[2], body[1] + (body[3] - body[1]) * 0.45)
        else:
            inner = body
        _centred_text(draw, inner, text, value_font, value_color)
        return layer

    @staticmethod
    def _value_color(gauge: Gauge, value: float | None) -> tuple[int, int, int, int]:
        """The threshold colour reached, or the default value colour."""
        threshold = gauge.threshold_color(value)
        if threshold is None:
            return VALUE_COLOR
        return _rgba(threshold, VALUE_COLOR)

    # ----------------------------------------------------------------- shapes

    def _dial(
        self,
        layer: PILImage.Image,
        gauge: Gauge,
        box: _Box,
        value: float | None,
        no_data: bool,
        color: tuple[int, int, int, int],
    ) -> None:
        """A 270-degree ring drawn at 3x and downsampled (T098)."""
        width = max(_MIN_SHAPE_PX, int(box[2] - box[0]))
        height = max(_MIN_SHAPE_PX, int(box[3] - box[1]))
        size = max(_MIN_SHAPE_PX, min(width, height))
        scale = _SUPERSAMPLE
        big = PILImage.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(big)
        stroke = max(scale, int(size * scale * 0.12))
        margin = stroke // 2 + scale
        ring = (margin, margin, size * scale - margin, size * scale - margin)
        draw.arc(
            ring,
            start=_DIAL_START_DEG,
            end=_DIAL_START_DEG + _DIAL_SWEEP_DEG,
            fill=TRACK_COLOR,
            width=stroke,
        )
        if not no_data and value is not None:
            sweep = _DIAL_SWEEP_DEG * gauge.fraction(value)
            if sweep >= 1.0:
                draw.arc(
                    ring,
                    start=_DIAL_START_DEG,
                    end=_DIAL_START_DEG + sweep,
                    fill=color,
                    width=stroke,
                )
        downsampled = big.resize((size, size), PILImage.Resampling.LANCZOS)
        layer.paste(
            downsampled,
            (int(box[0] + (width - size) // 2), int(box[1] + (height - size) // 2)),
            downsampled,
        )

    def _bar(
        self,
        layer: PILImage.Image,
        gauge: Gauge,
        box: _Box,
        value: float | None,
        no_data: bool,
        color: tuple[int, int, int, int],
    ) -> None:
        """A horizontal bar drawn at 3x and downsampled (T098)."""
        width = max(_MIN_SHAPE_PX, int(box[2] - box[0]))
        height = max(_MIN_SHAPE_PX, int(box[3] - box[1]))
        bar_height = max(6, int(height * 0.28))
        scale = _SUPERSAMPLE
        big = PILImage.new("RGBA", (width * scale, bar_height * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(big)
        box_big = (0, 0, width * scale - 1, bar_height * scale - 1)
        radius = (bar_height * scale) // 2
        draw.rounded_rectangle(box_big, radius=radius, fill=TRACK_COLOR)
        if not no_data and value is not None:
            filled = int((width * scale - 1) * gauge.fraction(value))
            if filled >= 1:
                draw.rounded_rectangle(
                    (0, 0, max(radius, filled), bar_height * scale - 1),
                    radius=radius,
                    fill=color,
                )
        downsampled = big.resize((width, bar_height), PILImage.Resampling.LANCZOS)
        layer.paste(
            downsampled,
            (int(box[0]), int(box[1] + (height - bar_height) // 2)),
            downsampled,
        )

    def _sparkline(
        self,
        layer: PILImage.Image,
        box: _Box,
        history: Sequence[float],
        gauge: Gauge,
        color: tuple[int, int, int, int],
        no_data: bool,
    ) -> None:
        """A short line graph of the recent history, scaled to the gauge's range."""
        points = [float(point) for point in history][-_SPARKLINE_POINTS:]
        if no_data or len(points) < 2:
            return
        width = int(box[2] - box[0])
        height = int(box[3] - box[1])
        area_top = box[1] + height * 0.5
        area_height = max(_MIN_SHAPE_PX, int(box[3] - area_top))
        low, high = gauge.range
        span = high - low if high > low else 1.0
        scale = _SUPERSAMPLE
        step = max(1, (width * scale) // max(1, len(points) - 1))
        plotted = []
        for index, point in enumerate(points):
            share = min(1.0, max(0.0, (point - low) / span))
            plotted.append(
                (
                    index * step,
                    (area_height - 1) * scale - int((area_height - 1) * scale * share),
                )
            )
        big = PILImage.new("RGBA", (max(1, width * scale), area_height * scale), (0, 0, 0, 0))
        ImageDraw.Draw(big).line(plotted, fill=color, width=2 * scale, joint="curve")
        downsampled = big.resize((max(1, width), area_height), PILImage.Resampling.LANCZOS)
        layer.paste(downsampled, (int(box[0]), int(area_top)), downsampled)


def _centred_text(
    draw: ImageDraw.ImageDraw,
    box: _Box,
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
