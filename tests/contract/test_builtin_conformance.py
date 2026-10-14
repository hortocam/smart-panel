"""Contract test: every built-in and the sample plugin conforms to the SDK (T110, US6).

Quickstart **Scenario G** and FR-047 require the SDK conformance suite
(``smart_panel.sdk.testing.assert_conforms``) to run against **every built-in
plugin** — ``text``, ``sources``, ``alerts``, ``crawl``, ``gauges``, ``sidebar``
— and against the sample third-party plugin (``examples/plugins/clock.py``,
T113). ``assert_conforms`` is what makes the plugin contract real: a manifest
valid, declared settings that validate, declared commands, a render that returns
an allowed ``RenderResult`` at the exact region size, a prompt ``stop``, and no
``smart_panel.core`` import.

The built-ins are read from the shipped registry, so a new built-in is covered
the moment it is registered (the parametrization is derived, not hand-listed).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from smart_panel.plugins import BUILTIN_PLUGINS
from smart_panel.sdk.plugin import Manifest, Plugin
from smart_panel.sdk.testing import PluginHarness, assert_conforms

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_PLUGIN = REPO_ROOT / "examples" / "plugins" / "clock.py"


def _load_sample() -> type[Plugin]:
    """Import ``examples/plugins/clock.py`` as a drop-in would and return its class."""
    assert SAMPLE_PLUGIN.is_file(), (
        f"the sample plugin {SAMPLE_PLUGIN} is missing; quickstart Scenario G copies it "
        "into <home>/plugins/"
    )
    spec = importlib.util.spec_from_file_location("sample_clock_plugin", SAMPLE_PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    classes = [
        obj
        for obj in vars(module).values()
        if isinstance(obj, type)
        and issubclass(obj, Plugin)
        and obj is not Plugin
        and hasattr(obj, "manifest")
    ]
    assert len(classes) == 1, f"expected exactly one plugin class in {SAMPLE_PLUGIN}"
    return classes[0]


# The built-in registry plus the sample plugin, as (param id, class) pairs. The
# manifest name is the parametrization id, so a failure names the plugin.
SAMPLE_CLOCK = _load_sample()
CONFORMANCE_CASES: list[tuple[str, type[Plugin]]] = [
    *sorted(BUILTIN_PLUGINS.items()),
    (SAMPLE_CLOCK.manifest.name, SAMPLE_CLOCK),
]


def test_the_builtin_registry_names_all_six_stories() -> None:
    """A missing built-in would silently drop a story from the conformance run."""
    assert set(BUILTIN_PLUGINS) == {
        "text",
        "sources",
        "alerts",
        "crawl",
        "gauges",
        "sidebar",
    }


def test_the_sample_plugin_is_part_of_the_run() -> None:
    assert SAMPLE_CLOCK.manifest.name == "clock"
    assert SAMPLE_CLOCK.manifest.animated is True, "a clock is rendered every frame"
    assert "format" in {setting.name for setting in SAMPLE_CLOCK.manifest.settings}
    assert "crash" in {command.name for command in SAMPLE_CLOCK.manifest.commands}


@pytest.mark.parametrize(
    ("name", "plugin_class"), CONFORMANCE_CASES, ids=[case[0] for case in CONFORMANCE_CASES]
)
def test_plugin_conforms_to_the_sdk(name: str, plugin_class: type[Plugin]) -> None:
    assert plugin_class.manifest.name == name
    assert_conforms(plugin_class)


def test_assert_conforms_rejects_a_non_conforming_plugin() -> None:
    """Negative control: a plugin that ignores the contract must fail the suite.

    Without this, a scanner that silently stopped checking (for example one that
    crashed and was swallowed) would make ``test_plugin_conforms_to_the_sdk``
    pass for the wrong reason.
    """

    class WrongSize(Plugin):
        manifest = Manifest(name="wrong-size", summary="returns the wrong size")

        def render(self, frame: object) -> object:
            from PIL import Image as PILImage

            from smart_panel.sdk.plugin import Image as RenderImage

            return RenderImage(PILImage.new("RGBA", (3, 3)))

    with pytest.raises(AssertionError):
        assert_conforms(WrongSize)

    # ...and the harness the suite uses is itself exercised here.
    harness = PluginHarness(WrongSize, size=(10, 10))
    try:
        assert harness.render() is not None
    finally:
        harness.stop()
