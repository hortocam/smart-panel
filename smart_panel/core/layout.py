"""Regions, overlays and the fixed canvas (T025).

The canvas is a fixed 1920x462 (not configurable). A layout is rejected with a
message that names the offending region, so an agent editing the layout gets an
actionable error. The *default* layout lives in
:mod:`smart_panel.core.defaults` — the only core module that names the built-in
plugins (FR-015).
"""

from __future__ import annotations

from dataclasses import dataclass, field

CANVAS_WIDTH = 1920
CANVAS_HEIGHT = 462
CANVAS = (CANVAS_WIDTH, CANVAS_HEIGHT)
MIN_REGION_PX = 64


class LayoutError(ValueError):
    """A layout rule was violated; the message names the offending region."""


@dataclass(frozen=True)
class Region:
    """A rectangular area of the canvas bound to one plugin instance."""

    name: str
    x: int
    y: int
    w: int
    h: int
    instance: str


@dataclass(frozen=True)
class Overlay:
    """A plugin instance drawn over the whole of a target region."""

    region: str
    instance: str
    z: int = 0


@dataclass
class Layout:
    """The regions and overlays that make up the display."""

    regions: list[Region] = field(default_factory=list)
    overlays: list[Overlay] = field(default_factory=list)


def validate(layout: Layout) -> None:
    """Validate ``layout`` against the layout rules.

    Rules (``data-model.md``): region names are unique; rectangles sit inside
    the 1920x462 canvas; regions may not overlap; each side is at least 64 px;
    every overlay targets an existing region.

    Raises:
        LayoutError: With a message naming the offending region.
    """
    seen: dict[str, Region] = {}
    for region in layout.regions:
        if region.name in seen:
            raise LayoutError(f"region {region.name!r}: duplicate region name")
        seen[region.name] = region
        if region.w < MIN_REGION_PX or region.h < MIN_REGION_PX:
            raise LayoutError(
                f"region {region.name!r}: width {region.w} and height {region.h} "
                f"must each be at least {MIN_REGION_PX} px"
            )
        if (
            region.x < 0
            or region.y < 0
            or region.x + region.w > CANVAS_WIDTH
            or region.y + region.h > CANVAS_HEIGHT
        ):
            raise LayoutError(
                f"region {region.name!r}: rectangle ({region.x},{region.y},{region.w},{region.h}) "
                f"is outside the {CANVAS_WIDTH}x{CANVAS_HEIGHT} canvas"
            )

    regions = list(layout.regions)
    for index, first in enumerate(regions):
        for second in regions[index + 1 :]:
            if _overlaps(first, second):
                raise LayoutError(
                    f"region {first.name!r}: overlaps region {second.name!r}"
                )

    for overlay in layout.overlays:
        if overlay.region not in seen:
            raise LayoutError(
                f"overlay {overlay.region!r}: target region does not exist"
            )


def _overlaps(first: Region, second: Region) -> bool:
    return (
        first.x < second.x + second.w
        and second.x < first.x + first.w
        and first.y < second.y + second.h
        and second.y < first.y + first.h
    )
