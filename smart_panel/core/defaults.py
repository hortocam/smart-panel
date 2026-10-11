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
