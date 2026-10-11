"""Unit tests for ``smart_panel.core.compositor`` (T030).

The compositor owns the fixed 1920x462 canvas. It caches one RGBA layer per
region, pastes the region layers, then draws overlays on top of their whole
target region, and stamps a small error badge in the corner of any region whose
instance is ``degraded`` or ``stalled``. Overlay semantics (``plugin-sdk.md``):
an ``Image`` is drawn, ``Inactive`` draws nothing, and ``Unchanged`` reuses the
cached layer. Recomposing is skipped entirely when nothing changed.

These tests use a small canvas and flat colours so a pixel read is enough to
prove which layer landed where.
"""

from __future__ import annotations

from PIL import Image as PILImage

from smart_panel.core.compositor import (
    DEFAULT_BACKGROUND,
    ERROR_BADGE_DEGRADED,
    ERROR_BADGE_STALLED,
    Compositor,
)
from smart_panel.core.layout import CANVAS, Layout, Overlay, Region
from smart_panel.core.workers import InstanceState
from smart_panel.sdk.plugin import Image as RenderImage
from smart_panel.sdk.plugin import Inactive, Unchanged

# A small valid layout: sidebar (left column), main (top right), crawl (bottom
# right), and the alerts overlay covering only ``main``. Every region is >= 64 px
# per side and inside the 400x300 canvas.
CANVAS_SIZE = (400, 300)
SIDEBAR = Region("sidebar", 0, 0, 100, 300, "sidebar")
MAIN = Region("main", 100, 0, 300, 220, "main")
CRAWL = Region("crawl", 100, 220, 300, 80, "crawl")

BLACK = (0, 0, 0, 255)
BLUE = (0, 0, 255, 255)
GREEN = (0, 255, 0, 255)
RED = (255, 0, 0, 255)
YELLOW = (255, 255, 0, 255)


def _layout() -> Layout:
    return Layout(
        regions=[SIDEBAR, MAIN, CRAWL],
        overlays=[Overlay(region="main", instance="alerts")],
    )


def _solid(size: tuple[int, int], color: tuple[int, int, int, int]) -> PILImage.Image:
    return PILImage.new("RGBA", size, color)


def _compositor(**kwargs: object) -> Compositor:
    return Compositor(_layout(), size=CANVAS_SIZE, **kwargs)


def _px(image: PILImage.Image, x: int, y: int) -> tuple[int, int, int, int]:
    pixel = image.getpixel((x, y))
    assert isinstance(pixel, tuple)  # the canvas is always RGBA
    red, green, blue, alpha = pixel
    return (int(red), int(green), int(blue), int(alpha))


# --------------------------------------------------------------------- canvas


def test_default_canvas_is_the_fixed_panel_size() -> None:
    comp = Compositor(_layout())
    assert comp.size == CANVAS
    assert CANVAS == (1920, 462)


def test_composite_returns_an_rgba_canvas_of_the_requested_size() -> None:
    comp = _compositor()
    canvas = comp.composite()
    assert canvas.size == CANVAS_SIZE
    assert canvas.mode == "RGBA"


def test_regions_without_a_layer_show_the_background() -> None:
    comp = _compositor()
    canvas = comp.composite()
    assert _px(canvas, 50, 50) == DEFAULT_BACKGROUND
    assert _px(canvas, 300, 100) == DEFAULT_BACKGROUND


# -------------------------------------------------------------------- regions


def test_region_layers_are_pasted_at_their_rectangles() -> None:
    comp = _compositor()
    comp.update("sidebar", RenderImage(_solid((100, 300), BLUE)))
    comp.update("main", RenderImage(_solid((300, 220), GREEN)))
    comp.update("crawl", RenderImage(_solid((300, 80), YELLOW)))
    canvas = comp.composite()

    assert _px(canvas, 50, 50) == BLUE  # inside sidebar
    assert _px(canvas, 250, 100) == GREEN  # inside main
    assert _px(canvas, 250, 250) == YELLOW  # inside crawl


def test_unchanged_keeps_the_previous_layer() -> None:
    comp = _compositor()
    comp.update("main", RenderImage(_solid((300, 220), GREEN)))
    comp.composite()

    comp.update("main", Unchanged())
    canvas = comp.composite()
    assert _px(canvas, 250, 100) == GREEN


def test_inactive_region_clears_the_layer() -> None:
    comp = _compositor()
    comp.update("main", RenderImage(_solid((300, 220), GREEN)))
    comp.composite()

    comp.update("main", Inactive())
    canvas = comp.composite()
    assert _px(canvas, 250, 100) == DEFAULT_BACKGROUND


# ------------------------------------------------------------------- overlays


def test_overlay_covers_only_its_region() -> None:
    comp = _compositor()
    comp.update("sidebar", RenderImage(_solid((100, 300), BLUE)))
    comp.update("main", RenderImage(_solid((300, 220), GREEN)))
    comp.update("crawl", RenderImage(_solid((300, 80), YELLOW)))
    comp.update("alerts", Inactive())
    comp.composite()

    # The alert shows: it must replace the whole main region...
    comp.update("alerts", RenderImage(_solid((300, 220), RED)))
    canvas = comp.composite()
    assert _px(canvas, 250, 100) == RED
    assert _px(canvas, 110, 10) == RED  # top-left of main
    assert _px(canvas, 390, 210) == RED  # bottom-right of main
    # ...and leave sidebar and crawl untouched.
    assert _px(canvas, 50, 50) == BLUE
    assert _px(canvas, 250, 250) == YELLOW


def test_inactive_overlay_draws_nothing() -> None:
    comp = _compositor()
    comp.update("main", RenderImage(_solid((300, 220), GREEN)))
    comp.update("alerts", RenderImage(_solid((300, 220), RED)))
    comp.composite()

    comp.update("alerts", Inactive())
    canvas = comp.composite()
    assert _px(canvas, 250, 100) == GREEN


def test_unchanged_overlay_reuses_its_cached_layer() -> None:
    comp = _compositor()
    comp.update("alerts", RenderImage(_solid((300, 220), RED)))
    comp.composite()

    comp.update("alerts", Unchanged())
    canvas = comp.composite()
    assert _px(canvas, 250, 100) == RED


def test_overlay_is_clipped_to_its_target_region() -> None:
    # A mis-sized overlay image must not bleed outside the region it covers.
    # ``main`` sits above ``crawl``, so an unclipped taller overlay would spill
    # into the crawl strip.
    comp = _compositor()
    comp.update("sidebar", RenderImage(_solid((100, 300), BLUE)))
    comp.update("crawl", RenderImage(_solid((300, 80), YELLOW)))
    comp.update("alerts", RenderImage(_solid((390, 290), RED)))
    canvas = comp.composite()
    assert _px(canvas, 250, 100) == RED  # inside main
    assert _px(canvas, 50, 50) == BLUE  # sidebar is not covered
    assert _px(canvas, 250, 250) == YELLOW  # the crawl strip is not covered


# ------------------------------------------------------------- cached rebuild


def test_unchanged_composite_is_cheap_and_reuses_the_cache() -> None:
    comp = _compositor()
    comp.update("main", RenderImage(_solid((300, 220), GREEN)))
    first = comp.composite()

    comp.update("main", Unchanged())
    second = comp.composite()
    assert second is first  # nothing changed: the canvas object is reused


def test_a_composite_after_a_change_is_a_fresh_canvas() -> None:
    comp = _compositor()
    comp.update("main", RenderImage(_solid((300, 220), GREEN)))
    first = comp.composite()

    comp.update("main", RenderImage(_solid((300, 220), RED)))
    second = comp.composite()
    assert second is not first
    assert _px(second, 250, 100) == RED


# ----------------------------------------------------------- dirty tracking


def test_updates_mark_only_their_region_dirty() -> None:
    comp = _compositor()
    assert comp.dirty_regions() == frozenset()

    comp.update("sidebar", RenderImage(_solid((100, 300), BLUE)))
    assert comp.dirty_regions() == frozenset({"sidebar"})

    comp.update("crawl", RenderImage(_solid((300, 80), YELLOW)))
    assert comp.dirty_regions() == frozenset({"sidebar", "crawl"})


def test_composite_clears_the_dirty_set() -> None:
    comp = _compositor()
    comp.update("main", RenderImage(_solid((300, 220), GREEN)))
    comp.composite()
    assert comp.dirty_regions() == frozenset()


def test_an_overlay_change_marks_its_target_region_dirty() -> None:
    comp = _compositor()
    comp.update("main", RenderImage(_solid((300, 220), GREEN)))
    comp.composite()

    comp.update("alerts", RenderImage(_solid((300, 220), RED)))
    assert comp.dirty_regions() == frozenset({"main"})


# ------------------------------------------------------------- error badges


def _has_badge(canvas: PILImage.Image, region: Region) -> bool:
    """A badge is a non-background, non-region pixel in the region's top-right."""
    corner = canvas.crop((region.x + region.w - 24, region.y, region.x + region.w, region.y + 24))
    palette = {_px(corner, x, y) for x in range(corner.width) for y in range(corner.height)}
    return ERROR_BADGE_DEGRADED in palette or ERROR_BADGE_STALLED in palette


def test_degraded_instance_gets_an_error_badge() -> None:
    comp = _compositor()
    comp.update("main", RenderImage(_solid((300, 220), GREEN)))
    comp.composite()
    assert not _has_badge(comp.composite(), MAIN)

    comp.set_state("main", InstanceState.DEGRADED)
    canvas = comp.composite()
    assert _has_badge(canvas, MAIN)


def test_stalled_instance_gets_a_distinct_error_badge() -> None:
    comp = _compositor()
    comp.update("main", RenderImage(_solid((300, 220), GREEN)))
    comp.set_state("main", InstanceState.STALLED)
    canvas = comp.composite()
    top_right = canvas.crop((MAIN.x + MAIN.w - 24, MAIN.y, MAIN.x + MAIN.w, MAIN.y + 24))
    palette = {
        _px(top_right, x, y) for x in range(top_right.width) for y in range(top_right.height)
    }
    assert ERROR_BADGE_STALLED in palette
    assert ERROR_BADGE_DEGRADED not in palette


def test_a_badge_appears_on_top_of_an_overlay() -> None:
    comp = _compositor()
    comp.update("main", RenderImage(_solid((300, 220), GREEN)))
    comp.update("alerts", RenderImage(_solid((300, 220), RED)))
    comp.set_state("main", InstanceState.STALLED)
    canvas = comp.composite()
    # The overlay still covers the region, but the badge is stamped above it.
    assert _px(canvas, 250, 100) == RED
    assert _has_badge(canvas, MAIN)


def test_badge_clears_when_the_instance_recovers() -> None:
    comp = _compositor()
    comp.update("main", RenderImage(_solid((300, 220), GREEN)))
    comp.set_state("main", InstanceState.DEGRADED)
    assert _has_badge(comp.composite(), MAIN)

    comp.set_state("main", InstanceState.OK)
    assert not _has_badge(comp.composite(), MAIN)


def test_a_badge_does_not_touch_other_regions() -> None:
    comp = _compositor()
    comp.update("sidebar", RenderImage(_solid((100, 300), BLUE)))
    comp.update("main", RenderImage(_solid((300, 220), GREEN)))
    comp.set_state("main", InstanceState.STALLED)
    canvas = comp.composite()
    assert _has_badge(canvas, MAIN)
    assert not _has_badge(canvas, SIDEBAR)
    assert _px(canvas, 50, 50) == BLUE


def test_a_badge_marks_its_region_dirty() -> None:
    comp = _compositor()
    comp.update("main", RenderImage(_solid((300, 220), GREEN)))
    comp.composite()
    comp.set_state("main", InstanceState.DEGRADED)
    assert comp.dirty_regions() == frozenset({"main"})


def _badge_palette(canvas: PILImage.Image, region: Region) -> set[tuple[int, int, int, int]]:
    """The distinct colours in the region's top-right corner (where the badge lands)."""
    corner = canvas.crop((region.x + region.w - 24, region.y, region.x + region.w, region.y + 24))
    return {_px(corner, x, y) for x in range(corner.width) for y in range(corner.height)}


def test_degraded_to_stalled_transition_repaints_the_badge() -> None:
    # degraded -> stalled is the normal worker progression. With a warm cache the
    # recompose must repaint the badge red, not keep the stale amber pixel.
    comp = _compositor()
    comp.update("main", RenderImage(_solid((300, 220), GREEN)))
    comp.set_state("main", InstanceState.DEGRADED)
    assert ERROR_BADGE_DEGRADED in _badge_palette(comp.composite(), MAIN)

    comp.set_state("main", InstanceState.STALLED)
    palette = _badge_palette(comp.composite(), MAIN)
    assert ERROR_BADGE_STALLED in palette
    assert ERROR_BADGE_DEGRADED not in palette


def test_stalled_to_degraded_transition_repaints_the_badge() -> None:
    # The reverse badged->badged transition must also invalidate the cache.
    comp = _compositor()
    comp.update("main", RenderImage(_solid((300, 220), GREEN)))
    comp.set_state("main", InstanceState.STALLED)
    assert ERROR_BADGE_STALLED in _badge_palette(comp.composite(), MAIN)

    comp.set_state("main", InstanceState.DEGRADED)
    palette = _badge_palette(comp.composite(), MAIN)
    assert ERROR_BADGE_DEGRADED in palette
    assert ERROR_BADGE_STALLED not in palette
