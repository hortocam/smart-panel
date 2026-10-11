"""Unit tests for ``smart_panel.core.layout`` and ``smart_panel.core.defaults`` (T025).

The canvas is fixed at 1920x462; a layout is rejected with a message that
names the offending region so an agent editing the layout gets an actionable
error.
"""

from __future__ import annotations

import pytest

from smart_panel.core.defaults import default_layout
from smart_panel.core.layout import CANVAS, Layout, LayoutError, Overlay, Region, validate


def test_canvas_is_the_fixed_panel_size() -> None:
    assert CANVAS == (1920, 462)


def test_default_layout_is_valid() -> None:
    layout = default_layout()
    validate(layout)
    assert {region.name for region in layout.regions} == {"sidebar", "main", "crawl"}
    assert [overlay.region for overlay in layout.overlays] == ["main"]


def test_duplicate_region_names_are_rejected() -> None:
    layout = Layout(
        regions=[
            Region("main", 0, 0, 100, 100, "a"),
            Region("main", 300, 0, 100, 100, "b"),
        ],
        overlays=[],
    )
    with pytest.raises(LayoutError) as excinfo:
        validate(layout)
    assert "main" in str(excinfo.value)


def test_region_outside_the_canvas_is_rejected() -> None:
    layout = Layout(regions=[Region("main", 1900, 0, 100, 100, "a")], overlays=[])
    with pytest.raises(LayoutError) as excinfo:
        validate(layout)
    assert "main" in str(excinfo.value)


def test_region_below_the_minimum_size_is_rejected() -> None:
    layout = Layout(regions=[Region("tiny", 0, 0, 32, 200, "a")], overlays=[])
    with pytest.raises(LayoutError) as excinfo:
        validate(layout)
    assert "tiny" in str(excinfo.value)
    assert "64" in str(excinfo.value)


def test_overlapping_regions_are_rejected() -> None:
    layout = Layout(
        regions=[
            Region("main", 0, 0, 400, 200, "a"),
            Region("crawl", 200, 100, 400, 200, "b"),
        ],
        overlays=[],
    )
    with pytest.raises(LayoutError) as excinfo:
        validate(layout)
    message = str(excinfo.value)
    assert "crawl" in message or "main" in message


def test_adjacent_regions_do_not_count_as_overlapping() -> None:
    layout = Layout(
        regions=[
            Region("left", 0, 0, 100, 200, "a"),
            Region("right", 100, 0, 100, 200, "b"),
        ],
        overlays=[],
    )
    validate(layout)


def test_overlay_must_target_an_existing_region() -> None:
    layout = Layout(
        regions=[Region("main", 0, 0, 200, 200, "a")],
        overlays=[Overlay("ghost", "alerts")],
    )
    with pytest.raises(LayoutError) as excinfo:
        validate(layout)
    assert "ghost" in str(excinfo.value)


def test_overlay_on_an_existing_region_is_accepted() -> None:
    layout = Layout(
        regions=[Region("main", 0, 0, 200, 200, "a")],
        overlays=[Overlay("main", "alerts", z=1)],
    )
    validate(layout)
