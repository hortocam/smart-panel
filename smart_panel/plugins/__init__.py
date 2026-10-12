"""The built-in plugin registry (T061).

The loader reads this mapping first (``PluginLoader._load_builtin_registry``),
then entry points, then the drop-in directory. It lists the built-ins that ship
with the project; later phases add ``alerts``, ``sources``, ``crawl``, ``gauges``
and ``sidebar`` here.

:func:`builtin_instances` is the default-config seed: every registered built-in
gets one enabled instance with its schema defaults, so a fresh install has a
valid config the moment it starts (T061). The registry imports every built-in,
which in turn import only ``smart_panel.sdk`` (FR-043), so this module stays on
the SDK side of the boundary.
"""

from __future__ import annotations

from typing import Any

from smart_panel.plugins.text import TextPlugin
from smart_panel.sdk.plugin import Plugin

BUILTIN_PLUGINS: dict[str, type[Plugin]] = {
    TextPlugin.manifest.name: TextPlugin,
}


def builtin_plugins() -> dict[str, type[Plugin]]:
    """Return a copy of the built-in plugin classes by name."""
    return dict(BUILTIN_PLUGINS)


def builtin_instances() -> dict[str, dict[str, Any]]:
    """One default instance per registered built-in, for the default config."""
    return {
        name: {"plugin": name, "enabled": True, "settings": {}}
        for name in sorted(BUILTIN_PLUGINS)
    }


__all__ = ["BUILTIN_PLUGINS", "TextPlugin", "builtin_instances", "builtin_plugins"]
