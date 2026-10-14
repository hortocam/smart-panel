"""The built-in plugin registry (T061, T072, T088, T100).

The loader reads this mapping first (``PluginLoader._load_builtin_registry``),
then entry points, then the drop-in directory. It lists the built-ins that ship
with the project: ``text`` (US1), ``alerts`` (US2, T072), ``crawl`` and
``sources`` (US3, T088), ``gauges`` (US4, T100) and ``sidebar`` (US5, T109).

:func:`builtin_instances` is the default-config seed: every registered built-in
gets one enabled instance, so a fresh install has a valid config the moment it
starts (T061). US4 adds the two pieces of default *data* the spec asks for out
of the box (FR-033): the ``sources`` instance starts with a ``sys`` ``system``
source, and the ``gauges`` instance starts with four gauges — CPU, MEM, TEMP and
DISK — over that source's topics. The gauges live in the ``main`` region of
``smart_panel.core.defaults`` (the only core module that names built-ins).

The registry imports every built-in, which in turn import only
``smart_panel.sdk`` (FR-043), so this module stays on the SDK side of the
boundary.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from smart_panel.plugins.alerts import AlertsPlugin
from smart_panel.plugins.crawl import CrawlPlugin
from smart_panel.plugins.gauges import GaugesPlugin
from smart_panel.plugins.sidebar import SidebarPlugin
from smart_panel.plugins.sources import SourcesPlugin
from smart_panel.plugins.text import TextPlugin
from smart_panel.sdk.plugin import Plugin

BUILTIN_PLUGINS: dict[str, type[Plugin]] = {
    TextPlugin.manifest.name: TextPlugin,
    AlertsPlugin.manifest.name: AlertsPlugin,
    CrawlPlugin.manifest.name: CrawlPlugin,
    SourcesPlugin.manifest.name: SourcesPlugin,
    GaugesPlugin.manifest.name: GaugesPlugin,
    SidebarPlugin.manifest.name: SidebarPlugin,
}

# The system source the default config starts with, and the four gauges over its
# topics (US4 independent test: "Four system-metric gauges appear in a four-up
# layout and update at their rates"). ``disk:/`` is the root filesystem; the
# temperature metric is requested, and a host without a sensor simply shows the
# gauge's ``no data`` state (FR-036).
DEFAULT_SYSTEM_SOURCE: dict[str, Any] = {
    "type": "system",
    "interval_s": 2,
    "settings": {"metrics": ["cpu", "mem", "temp", "disk:/"]},
}

DEFAULT_GAUGES: list[dict[str, Any]] = [
    {
        "id": "cpu",
        "label": "CPU",
        "topic": "sys.cpu",
        "aggregation": "avg",
        "window_s": 60,
        "unit": "%",
        "range": [0, 100],
        "style": "dial",
        "thresholds": [
            {"at": 75, "color": "#E6A700"},
            {"at": 90, "color": "#D62839"},
        ],
    },
    {
        "id": "mem",
        "label": "MEM",
        "topic": "sys.mem",
        "aggregation": "avg",
        "window_s": 60,
        "unit": "%",
        "range": [0, 100],
        "style": "bar",
        "thresholds": [
            {"at": 80, "color": "#E6A700"},
            {"at": 95, "color": "#D62839"},
        ],
    },
    {
        "id": "temp",
        "label": "TEMP",
        "topic": "sys.temp",
        "aggregation": "last",
        "window_s": 60,
        "unit": "C",
        "range": [0, 100],
        "style": "numeric",
        "thresholds": [
            {"at": 70, "color": "#E6A700"},
            {"at": 85, "color": "#D62839"},
        ],
    },
    {
        "id": "disk",
        "label": "DISK",
        "topic": "sys.disk:/",
        "aggregation": "max",
        "window_s": 60,
        "unit": "%",
        "range": [0, 100],
        "style": "sparkline",
        "thresholds": [
            {"at": 85, "color": "#E6A700"},
            {"at": 95, "color": "#D62839"},
        ],
    },
]

# Instance name -> the settings a fresh install starts that instance with.
DEFAULT_INSTANCE_SETTINGS: dict[str, dict[str, Any]] = {
    "sources": {"sources": {"sys": DEFAULT_SYSTEM_SOURCE}},
    "gauges": {"gauges": DEFAULT_GAUGES},
    # US5: the sidebar starts with no configured entries; the built-in alert
    # entry is implicit (T109).
    "sidebar": {"entries": []},
}


def builtin_plugins() -> dict[str, type[Plugin]]:
    """Return a copy of the built-in plugin classes by name."""
    return dict(BUILTIN_PLUGINS)


def builtin_instances() -> dict[str, dict[str, Any]]:
    """One default instance per registered built-in, for the default config.

    Every instance starts enabled with its schema defaults; ``sources`` and
    ``gauges`` additionally carry the US4 defaults (the ``sys`` system source and
    the four CPU/MEM/TEMP/DISK gauges). ``crawl`` keeps the ticker defaults, so a
    fresh install validates with no endpoints configured.
    """
    return {
        name: {
            "plugin": name,
            "enabled": True,
            "settings": deepcopy(DEFAULT_INSTANCE_SETTINGS.get(name, {})),
        }
        for name in sorted(BUILTIN_PLUGINS)
    }


__all__ = [
    "BUILTIN_PLUGINS",
    "DEFAULT_GAUGES",
    "DEFAULT_INSTANCE_SETTINGS",
    "DEFAULT_SYSTEM_SOURCE",
    "AlertsPlugin",
    "CrawlPlugin",
    "GaugesPlugin",
    "SidebarPlugin",
    "SourcesPlugin",
    "TextPlugin",
    "builtin_instances",
    "builtin_plugins",
]
