"""The frame compositor: region layers, overlays, dirty tracking (T030).

The compositor owns the fixed 1920x462 canvas. It keeps a cached RGBA layer per
region, composes the canvas by pasting the region layers and then drawing each
overlay over the whole of its target region, and tracks which regions changed
since the last compose so a static frame costs nothing to rebuild
(``research.md`` decision 14).

Overlay semantics follow the render contract (``contracts/plugin-sdk.md``):

* an ``Image`` result replaces the layer;
* ``Inactive`` draws nothing (the layer is cleared);
* ``Unchanged`` reuses the cached layer.

A region whose instance is ``degraded`` or ``stalled`` gets a small error badge
in its top-right corner, drawn last so it stays visible over an overlay. The
badge colour distinguishes the two states using the alert palette
(``data-model.md``): amber for ``degraded``, red for ``stalled``.

The compositor never mutates a plugin image; it converts each layer to RGBA and
clips it to its rectangle. The image returned by :meth:`composite` is cached and
reused across calls, so callers must treat it as read-only (the encoder and both
image sinks only read it).
"""

from __future__ import annotations

from typing import Any

from PIL import Image as PILImage
from PIL import ImageDraw

from smart_panel.core.layout import CANVAS, Layout, Region, validate
from smart_panel.core.workers import InstanceState
from smart_panel.sdk.plugin import Image as RenderImage
from smart_panel.sdk.plugin import Inactive, Unchanged

__all__ = [
    "Compositor",
    "DEFAULT_BACKGROUND",
    "ERROR_BADGE_DEGRADED",
    "ERROR_BADGE_STALLED",
    "ERROR_BADGE_MARGIN",
    "ERROR_BADGE_SIZE",
]

DEFAULT_BACKGROUND = (0, 0, 0, 255)
ERROR_BADGE_DEGRADED = (230, 167, 0, 255)  # amber, matching warning
ERROR_BADGE_STALLED = (214, 40, 57, 255)  # red, matching critical
ERROR_BADGE_SIZE = 20
ERROR_BADGE_MARGIN = 4
_BADGE = (InstanceState.DEGRADED, InstanceState.STALLED)


class Compositor:
    """Compose region layers and overlays onto the fixed canvas.

    Args:
        layout: The regions and overlays to draw.
        size: Canvas size in pixels; defaults to the fixed panel size.
        background: Opaque RGBA colour shown wherever no layer covers a pixel.

    The layout is validated on construction (and by :meth:`set_layout`), so an
    overlay that targets a missing region fails fast rather than at compose time.
    """

    def __init__(
        self,
        layout: Layout,
        *,
        size: tuple[int, int] = CANVAS,
        background: tuple[int, int, int, int] = DEFAULT_BACKGROUND,
    ) -> None:
        self._size = (int(size[0]), int(size[1]))
        self._background = tuple(background)
        self._layout = Layout()
        # One cached RGBA layer per *instance*; ``None`` means "draw nothing".
        self._layers: dict[str, PILImage.Image | None] = {}
        self._states: dict[str, InstanceState] = {}
        self._dirty: set[str] = set()
        self._canvas: PILImage.Image | None = None
        self._region_by_instance: dict[str, Region] = {}
        self._region_by_name: dict[str, Region] = {}
        self.set_layout(layout)

    # ------------------------------------------------------------------ layout

    @property
    def size(self) -> tuple[int, int]:
        """The canvas size in pixels."""
        return self._size

    @property
    def layout(self) -> Layout:
        """The layout currently being composed."""
        return self._layout

    def set_layout(self, layout: Layout) -> None:
        """Swap in a new layout and drop layers it no longer references."""
        validate(layout)
        self._layout = layout
        self._region_by_instance = {region.instance: region for region in layout.regions}
        self._region_by_name = {region.name: region for region in layout.regions}
        referenced = set(self._region_by_instance)
        referenced.update(overlay.instance for overlay in layout.overlays)
        self._layers = {name: layer for name, layer in self._layers.items() if name in referenced}
        self._states = {name: state for name, state in self._states.items() if name in referenced}
        self._canvas = None
        self._dirty = set()

    # ------------------------------------------------------------------ input

    def update(self, instance: str, result: Any = None) -> None:
        """Record a fresh render result for ``instance``.

        ``result`` is a :class:`RenderResult` (``Image``, ``Unchanged`` or
        ``Inactive``), a ``PIL.Image`` (a convenience for the worker's cached
        image), or ``None`` (equivalent to ``Inactive``).
        """
        kind, image = _normalise(result)
        region = self._region_for(instance)

        if kind == "unchanged":
            return
        if kind == "inactive":
            changed = self._layers.get(instance) is not None
            self._layers[instance] = None
        else:  # "image"
            assert image is not None  # kind "image" always carries an image
            self._layers[instance] = _to_rgba(image)
            changed = True

        if changed and region is not None:
            self._dirty.add(region.name)

    def set_state(self, instance: str, state: InstanceState) -> None:
        """Record the health of ``instance``; drives the error badge."""
        region = self._region_for(instance)
        was_badged = self._states.get(instance) in _BADGE
        self._states[instance] = state
        now_badged = state in _BADGE
        if region is not None and was_badged != now_badged:
            self._dirty.add(region.name)

    def dirty_regions(self) -> frozenset[str]:
        """Region names changed by :meth:`update`/:meth:`set_state` since the last compose.

        The first :meth:`composite` after construction always builds the canvas
        (there is no cache yet); ``dirty_regions`` therefore reports only the
        changes the caller has made since the previous compose.
        """
        return frozenset(self._dirty)

    # ---------------------------------------------------------------- compose

    def composite(self) -> PILImage.Image:
        """Return the composed canvas, reusing the cache when nothing changed."""
        if self._canvas is not None and not self._dirty:
            return self._canvas

        canvas = PILImage.new("RGBA", self._size, self._background)
        for region in self._layout.regions:
            self._paste(canvas, region, self._layers.get(region.instance))
        for overlay in sorted(self._layout.overlays, key=lambda item: item.z):
            target = self._region_by_name[overlay.region]
            self._paste(canvas, target, self._layers.get(overlay.instance))
        for region in self._layout.regions:
            state = self._states.get(region.instance)
            if state is not None and state in _BADGE:
                _draw_badge(canvas, region, state)

        self._canvas = canvas
        self._dirty.clear()
        return canvas

    # ----------------------------------------------------------------- helpers

    def _region_for(self, instance: str) -> Region | None:
        """The region an instance affects: its own, or the overlay's target."""
        own = self._region_by_instance.get(instance)
        if own is not None:
            return own
        for overlay in self._layout.overlays:
            if overlay.instance == instance:
                return self._region_by_name.get(overlay.region)
        return None

    def _paste(
        self, canvas: PILImage.Image, region: Region, layer: PILImage.Image | None
    ) -> None:
        # Clip into a region-sized patch so a mis-sized layer cannot bleed into
        # a neighbouring region.
        if layer is None:
            return
        patch = PILImage.new("RGBA", (region.w, region.h), (0, 0, 0, 0))
        patch.paste(layer, (0, 0), layer)
        canvas.paste(patch, (region.x, region.y), patch)


def _normalise(result: Any) -> tuple[str, PILImage.Image | None]:
    """Reduce a render result to ``(kind, image)`` with kind in image/unchanged/inactive."""
    if result is None or isinstance(result, Inactive):
        return "inactive", None
    if isinstance(result, Unchanged):
        return "unchanged", None
    if isinstance(result, RenderImage):
        return "image", result.image
    if isinstance(result, PILImage.Image):
        return "image", result
    raise ValueError(
        f"unsupported render result {type(result).__name__}; "
        "expected Image, Unchanged, Inactive, PIL.Image or None"
    )


def _to_rgba(image: PILImage.Image) -> PILImage.Image:
    """Return ``image`` as RGBA without mutating the original."""
    if image.mode == "RGBA":
        return image
    return image.convert("RGBA")


def _draw_badge(canvas: PILImage.Image, region: Region, state: InstanceState) -> None:
    """Stamp a small error badge in the region's top-right corner."""
    colour = ERROR_BADGE_STALLED if state == InstanceState.STALLED else ERROR_BADGE_DEGRADED
    size = min(ERROR_BADGE_SIZE, region.w, region.h)
    left = region.x + region.w - size - ERROR_BADGE_MARGIN
    top = region.y + ERROR_BADGE_MARGIN
    right = left + size - 1
    bottom = top + size - 1

    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.rounded_rectangle([left, top, right, bottom], radius=3, fill=colour)

    mid = left + size // 2
    white = (255, 255, 255, 255)
    draw.rectangle([mid - 1, top + 4, mid + 1, bottom - 6], fill=white)
    draw.rectangle([mid - 1, bottom - 4, mid + 1, bottom - 2], fill=white)
