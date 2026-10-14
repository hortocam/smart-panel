"""Default layout for the built-in plugins (T025, FR-015).

This is deliberately the *only* core module that names built-in plugins: the
default layout references the ``sidebar``, ``gauges``, ``crawl`` and ``alerts``
instances by name, but the core itself contains no feature logic. The layout is
filtered to whichever instances exist when the default config is assembled
(T061/T109).
"""

from __future__ import annotations

from smart_panel.core.layout import Layout, Overlay, Region


def default_layout() -> Layout:
    """Return the default 1920x462 layout (sidebar | main | crawl, alerts overlay)."""
    return Layout(
        regions=[
            Region(name="sidebar", x=0, y=0, w=192, h=462, instance="sidebar"),
            Region(name="main", x=192, y=0, w=1728, h=382, instance="gauges"),
            Region(name="crawl", x=192, y=382, w=1728, h=80, instance="crawl"),
        ],
        overlays=[Overlay(region="main", instance="alerts")],
    )


def default_layout_for(instances: object) -> dict[str, list[dict[str, object]]]:
    """The default layout filtered to regions/overlays whose instance exists.

    Until every built-in exists, the default config may only reference instances
    that are actually registered, or it would not validate (T061). ``instances``
    is the set/iterable/mapping of configured instance names.

    An overlay is kept only when *both* its instance and its target region are
    present: the ``alerts`` overlay targets ``main``, which belongs to the
    ``gauges`` instance (Phase 6). Keeping an overlay whose target region was
    filtered out would make the fresh config invalid
    ("overlay 'main': target region does not exist"), so the filter is applied to
    the whole reference, not just the overlay's own instance (T072).
    """
    present = set(instances)  # type: ignore[arg-type]
    layout = default_layout()
    regions = [region for region in layout.regions if region.instance in present]
    region_names = {region.name for region in regions}
    return {
        "regions": [
            {
                "name": r.name,
                "x": r.x,
                "y": r.y,
                "w": r.w,
                "h": r.h,
                "instance": r.instance,
            }
            for r in regions
        ],
        "overlays": [
            {"region": o.region, "instance": o.instance, "z": o.z}
            for o in layout.overlays
            if o.instance in present and o.region in region_names
        ],
    }

