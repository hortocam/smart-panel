"""Unit tests for gauge rendering (T092, US4).

Covers ``smart_panel/plugins/gauges/render.py`` and the grid/threshold parts of
``model.py``: grids for 1 to 6 gauges (1x1 up to 3x2), the four styles ``dial``,
``bar``, ``numeric`` and ``sparkline``, thresholds that change a gauge's colour
at the right value and recover, a stale or ``no_data`` reading that dims the
value and shows ``no data``, text never below ``min_text_px`` (with configured
sizes below the floor clamped up), and per-gauge layers cached until the value,
thresholds or stale state change.
"""

from __future__ import annotations

import pytest
from PIL import Image as PILImage

from smart_panel.core.fonts import FontRegistry
from smart_panel.plugins.gauges import GaugesPlugin
from smart_panel.plugins.gauges.model import Gauge, GaugeError, grid_for
from smart_panel.plugins.gauges.render import (
    LABEL_COLOR,
    VALUE_COLOR,
    GaugeReader,
    GaugeRenderer,
)
from smart_panel.sdk.context import FrameContext
from smart_panel.sdk.settings import validate

REGION = (1728, 382)
WARN = "#E6A700"
CRIT = "#D62839"


def _frame(*, size: tuple[int, int] = REGION, min_text_px: int = 24) -> FrameContext:
    return FrameContext(
        size=size,
        now=0.0,
        wall=0.0,
        frame_index=1,
        fonts=FontRegistry(),
        icons=None,
        bus=None,
        min_text_px=min_text_px,
    )


def _settings(**overrides: object) -> dict:
    return validate(GaugesPlugin.manifest.settings, overrides)


def _gauge(index: int = 0, **overrides: object) -> Gauge:
    fields: dict = {
        "id": f"g{index}",
        "label": f"G{index}",
        "topic": f"sys.g{index}",
        "aggregation": "last",
        "style": "numeric",
        "unit": "%",
        "range": [0, 100],
    }
    fields.update(overrides)
    return Gauge.from_dict(fields)


def _reading(value: float | None, *, stale: bool = False, history: tuple = ()) -> GaugeReader:
    return GaugeReader(value=value, stale=stale, history=tuple(history))


def _ink(image: PILImage.Image) -> int:
    """The number of pixels that differ from the image's most common colour."""
    colours = image.convert("RGB").getcolors(maxcolors=1 << 20) or []
    background = max(colours, key=lambda pair: pair[0])[1]
    return sum(count for count, colour in colours if colour != background)


def _count(image: PILImage.Image, colour: tuple[int, int, int]) -> int:
    """How many pixels of ``image`` are exactly ``colour`` (RGB)."""
    counts = image.convert("RGB").getcolors(maxcolors=1 << 20) or []
    return sum(count for count, rgb in counts if rgb == colour)


def _crop(image: PILImage.Image, box: tuple[int, int, int, int]) -> PILImage.Image:
    x, y, w, h = box
    return image.crop((x, y, x + w, y + h))


def _cell_image(renderer: GaugeRenderer, gauge: Gauge, reading: GaugeReader, **settings: object):
    """Render one gauge alone in its own canvas (one cell = the whole region)."""
    return renderer.render(_frame(), [gauge], [reading], _settings(**settings))


# ------------------------------------------------------------------- the grid


def test_the_grid_grows_with_the_gauge_count() -> None:
    assert grid_for(0) == (1, 1)
    assert grid_for(1) == (1, 1)
    assert grid_for(2) == (2, 1)
    assert grid_for(3) == (3, 1)
    assert grid_for(4) == (2, 2)
    assert grid_for(5) == (3, 2)
    assert grid_for(6) == (3, 2)
    with pytest.raises(GaugeError):
        grid_for(7)


def test_an_explicit_layout_overrides_the_automatic_grid() -> None:
    assert grid_for(4, "auto") == (2, 2)
    assert grid_for(4, {"cols": 4, "rows": 1}) == (4, 1)
    # A layout too small for the count is refused rather than drawn overlapping.
    with pytest.raises(GaugeError):
        grid_for(6, {"cols": 2, "rows": 1})
    with pytest.raises(GaugeError):
        grid_for(2, {"cols": 0, "rows": 2})


def test_six_gauges_fill_a_three_by_two_grid() -> None:
    gauges = [_gauge(index) for index in range(6)]
    readings = [_reading(50.0) for _ in gauges]
    image = GaugeRenderer().render(_frame(), gauges, readings, _settings())
    for row in range(2):
        for column in range(3):
            cell = _crop(
                image,
                (
                    column * REGION[0] // 3,
                    row * REGION[1] // 2,
                    REGION[0] // 3,
                    REGION[1] // 2,
                ),
            )
            assert _ink(cell) > 0, f"cell ({column},{row}) is empty"


def test_no_gauges_paint_the_background_only() -> None:
    image = GaugeRenderer().render(_frame(), [], [], _settings(bg="#101418"))
    assert image.size == REGION
    assert image.convert("RGB").getpixel((5, 5)) == (16, 20, 24)
    assert _ink(image) == 0


# ----------------------------------------------------------------- the styles


def test_every_style_draws_content_and_they_are_all_different() -> None:
    frames: dict[str, list] = {}
    for style in ("dial", "bar", "numeric", "sparkline"):
        styled = _gauge(style=style)
        image = _cell_image(GaugeRenderer(), styled, _reading(42.0, history=(10, 20, 30, 42)))
        assert image.size == REGION
        assert _ink(image) > 0, f"style {style} drew nothing"
        frames[style] = list(image.getdata())
    assert len({tuple(data) for data in frames.values()}) == 4, (
        "each style must be visually distinct"
    )


def test_a_numeric_gauge_shows_its_value_and_unit() -> None:
    image = _cell_image(GaugeRenderer(), _gauge(style="numeric"), _reading(42.0))
    assert _count(image, (255, 255, 255)) > 0
    # The formatted value carries the unit (``42%``); a different value differs.
    other = _cell_image(GaugeRenderer(), _gauge(style="numeric"), _reading(7.0))
    assert list(image.getdata()) != list(other.getdata())


def test_a_sparkline_uses_the_recent_history() -> None:
    gauge = _gauge(style="sparkline")
    flat = _cell_image(GaugeRenderer(), gauge, _reading(50.0, history=(50, 50, 50, 50)))
    ramp = _cell_image(GaugeRenderer(), gauge, _reading(50.0, history=(0, 20, 40, 50)))
    assert _ink(flat) > 0 and _ink(ramp) > 0
    assert list(flat.getdata()) != list(ramp.getdata())


# --------------------------------------------------------------- thresholds


def test_a_threshold_changes_the_colour_at_the_right_value_and_recovers() -> None:
    gauge = _gauge(
        style="numeric",
        thresholds=[{"at": 75, "color": WARN}, {"at": 90, "color": CRIT}],
    )
    renderer = GaugeRenderer()
    assert gauge.threshold_color(50.0) is None, "below the first threshold: the default colour"
    assert gauge.threshold_color(75.0) == WARN
    assert gauge.threshold_color(89.9) == WARN
    assert gauge.threshold_color(90.0) == CRIT
    assert gauge.threshold_color(100.0) == CRIT

    low = _cell_image(renderer, gauge, _reading(50.0))
    assert _count(low, (230, 167, 0)) == 0 and _count(low, (214, 40, 57)) == 0
    assert _count(low, (255, 255, 255)) > 0

    warning = _cell_image(renderer, gauge, _reading(78.0))
    assert _count(warning, (230, 167, 0)) > 0, "the warning colour must be drawn"

    critical = _cell_image(renderer, gauge, _reading(95.0))
    assert _count(critical, (214, 40, 57)) > 0, "the critical colour must be drawn"

    recovered = _cell_image(renderer, gauge, _reading(30.0))
    assert _count(recovered, (230, 167, 0)) == 0
    assert _count(recovered, (214, 40, 57)) == 0
    assert _count(recovered, (255, 255, 255)) > 0


def test_the_highest_reached_threshold_wins() -> None:
    gauge = _gauge(thresholds=[{"at": 75, "color": WARN}, {"at": 90, "color": CRIT}])
    assert gauge.threshold_color(95.0) == CRIT


# ------------------------------------------------------------ stale/no data


def test_a_stale_reading_dims_the_value_and_shows_no_data() -> None:
    gauge = _gauge(style="numeric")
    renderer = GaugeRenderer()
    fresh = _cell_image(renderer, gauge, _reading(42.0))
    stale = _cell_image(renderer, gauge, _reading(42.0, stale=True))
    no_data = _cell_image(renderer, gauge, _reading(None))

    assert _count(fresh, (255, 255, 255)) > 0
    assert _count(stale, (255, 255, 255)) == 0, "a stale value must not be drawn in full colour"
    assert list(stale.getdata()) == list(no_data.getdata()), (
        "a stale reading and a never-set topic render the same 'no data' state"
    )
    assert _ink(stale) > 0, "the 'no data' text must still be visible"

    def peak(image: PILImage.Image) -> int:
        return max(max(pixel[:3]) for pixel in image.convert("RGB").getdata())

    assert peak(stale) < peak(fresh), "the stale state must be dimmed"


def test_a_no_data_gauge_keeps_its_label_legible() -> None:
    gauge = _gauge(style="dial")
    image = _cell_image(GaugeRenderer(), gauge, _reading(None))
    assert _count(image, LABEL_COLOR[:3]) > 0, "the label is drawn even with no data"


# ------------------------------------------------------------- the text floor


def test_the_value_and_label_sizes_are_clamped_up_to_min_text_px() -> None:
    gauge = _gauge(style="numeric")
    renderer = GaugeRenderer()
    floor = 32

    def render(**settings: object):
        return renderer.render(
            _frame(min_text_px=floor), [gauge], [_reading(42.0)], _settings(**settings)
        )

    assert list(render(value_size=12, label_size=32).getdata()) == list(
        render(value_size=floor, label_size=32).getdata()
    ), "a value_size below min_text_px must be clamped up"
    assert list(render(value_size=72, label_size=12).getdata()) == list(
        render(value_size=72, label_size=floor).getdata()
    ), "a label_size below min_text_px must be clamped up"


def test_the_default_sizes_are_the_contract_defaults() -> None:
    settings = _settings()
    assert settings["value_size"] == 72
    assert settings["label_size"] == 28
    assert settings["bg"] == "#101418"


# ------------------------------------------------------------------- caching


def test_a_layer_is_cached_until_the_value_thresholds_or_stale_state_change() -> None:
    gauge = _gauge(
        style="dial", thresholds=[{"at": 50, "color": WARN}, {"at": 90, "color": CRIT}]
    )
    renderer = GaugeRenderer()
    frame = _frame()
    settings = _settings()
    renderer.render(frame, [gauge], [_reading(10.0)], settings)
    assert renderer.builds == 1

    renderer.render(frame, [gauge], [_reading(10.0)], settings)
    assert renderer.builds == 1, "an unchanged value must reuse the cached layer"

    renderer.render(frame, [gauge], [_reading(60.0)], settings)
    assert renderer.builds == 2, "crossing a threshold must rebuild the layer"

    renderer.render(frame, [gauge], [_reading(60.0)], settings)
    assert renderer.builds == 2

    renderer.render(frame, [gauge], [_reading(60.0, stale=True)], settings)
    assert renderer.builds == 3, "the stale state must rebuild the layer"

    renderer.render(frame, [gauge], [_reading(None)], settings)
    assert renderer.builds == 3, "no data after stale reuses the same 'no data' layer"


def test_the_label_colour_is_dimmer_than_the_value_colour() -> None:
    assert max(VALUE_COLOR[:3]) > max(LABEL_COLOR[:3])
