"""Unit tests for the plugin SDK surface in ``smart_panel.sdk.plugin`` (T018).

``sdk.testing.assert_conforms`` (T031) and the built-in conformance suite
(T110) build on these primitives, so the manifest validation, the render
result variants and the exceptions are pinned here. T018 names no test file in
``tasks.md``; this covers it under constitution Principle IV.
"""

from __future__ import annotations

import pytest
from PIL import Image

from smart_panel.sdk.plugin import (
    CAPABILITIES,
    Command,
    CommandError,
    Inactive,
    Manifest,
    Plugin,
    RenderResult,
    SettingsRejected,
    SourceType,
    Unchanged,
)
from smart_panel.sdk.plugin import (
    Image as ImageResult,
)


def _manifest(**overrides: object) -> Manifest:
    kwargs: dict[str, object] = {
        "name": "text",
        "version": "0.1.0",
        "sdk_version": "1",
        "summary": "static text region",
        "capabilities": {"region"},
    }
    kwargs.update(overrides)
    return Manifest(**kwargs)  # type: ignore[arg-type]


def test_capabilities_are_the_documented_set() -> None:
    assert CAPABILITIES == frozenset(
        {"region", "overlay", "datasource", "alert_source", "alert_sink"}
    )


def test_valid_manifest_passes_validation() -> None:
    _manifest().validate()


@pytest.mark.parametrize("bad_name", ["Bad Name", "1bad", "x", "UPPER", "with/slash"])
def test_manifest_rejects_bad_names(bad_name: str) -> None:
    with pytest.raises(ValueError):
        _manifest(name=bad_name).validate()


def test_manifest_rejects_an_unknown_capability() -> None:
    with pytest.raises(ValueError):
        _manifest(capabilities={"nope"}).validate()


def test_manifest_accepts_commands_source_types_and_sizes() -> None:
    manifest = _manifest(
        capabilities={"datasource"},
        commands=[Command("push", "push a value", spoolable=True)],
        source_types=[SourceType("pushed", settings=[])],
        min_size=(100, 64),
        max_size=(1920, 462),
        animated=True,
    )
    manifest.validate()
    assert manifest.commands[0].spoolable is True
    assert manifest.source_types[0].name == "pushed"


def test_render_result_variants() -> None:
    image = Image.new("RGB", (10, 10))
    assert isinstance(Unchanged(), RenderResult)
    assert isinstance(Inactive(), RenderResult)
    wrapped = ImageResult(image)
    assert isinstance(wrapped, RenderResult)
    assert wrapped.image is image


def test_command_error_carries_a_code_and_message() -> None:
    error = CommandError("not_found", "no such gauge")
    assert error.code == "not_found"
    assert error.message == "no such gauge"


def test_settings_rejected_is_an_exception() -> None:
    assert issubclass(SettingsRejected, Exception)


class _Demo(Plugin):
    manifest = _manifest()


def test_plugin_base_class_defaults() -> None:
    plugin = _Demo(ctx=None, settings={})
    assert isinstance(plugin.render(frame=None), Unchanged)  # type: ignore[arg-type]
    assert plugin.describe_state() == {}
    plugin.start()
    plugin.on_settings_changed({"text": "hi"})
    assert plugin.settings["text"] == "hi"
    plugin.stop()


def test_plugin_base_handle_command_raises_command_error() -> None:
    plugin = _Demo(ctx=None, settings={})
    with pytest.raises(CommandError) as excinfo:
        plugin.handle_command("nope", {})
    assert excinfo.value.code == "invalid_input"
