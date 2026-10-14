"""Contract test for generic ``ctx.alert`` routing (T131, FR-024).

``contracts/plugin-sdk.md`` gives any plugin that declares the ``alert_source``
capability a ``ctx.alert(...)`` call, and says the core routes it to the
instance declaring ``alert_sink`` "with the same validation as ``alert push``".

This test drives that path four ways:

* through the ``sdk.testing`` harness, where ``ctx.alert`` is available and the
  call lands in the ``alerts`` plugin and shows up in its ``show``/``list``;
* through :class:`~smart_panel.core.plugins.AlertRouter` bound to a real
  :class:`~smart_panel.core.workers.RenderWorker`, so a plugin that raises an
  alert *while rendering* reaches the sink too;
* with a plugin that does **not** declare ``alert_source``: refused with a clear
  error before the handler is reached;
* with no ``alert_sink`` instance configured: refused with a clear message.
"""

from __future__ import annotations

from typing import Any

import pytest

from smart_panel.core.plugins import AlertRouter, AlertRoutingError
from smart_panel.core.workers import RenderWorker, bind_alert_router
from smart_panel.plugins.alerts import AlertsPlugin
from smart_panel.sdk.plugin import (
    CommandError,
    Manifest,
    Plugin,
    Unchanged,
)
from smart_panel.sdk.testing import PluginHarness

MAIN_SIZE = (1728, 382)


class _SourcePlugin(Plugin):
    """A sample alert-source plugin: it may call ``ctx.alert`` and nothing else."""

    manifest = Manifest(
        name="sample-source",
        version="0.1.0",
        summary="a sample alert_source plugin",
        capabilities=frozenset({"alert_source"}),
    )

    def raise_one(self, *args: Any, **kwargs: Any) -> Any:
        return self.ctx.alert(*args, **kwargs)


class _RegionSourcePlugin(_SourcePlugin):
    """An alert source that also renders, so the alert is raised from a worker."""

    manifest = Manifest(
        name="sample-region-source",
        version="0.1.0",
        summary="an alert_source region plugin",
        capabilities=frozenset({"region", "alert_source"}),
    )

    def render(self, frame: Any) -> Any:
        if not getattr(self, "_raised", False):
            self._raised = True
            self.ctx.alert("warning", "raised while rendering", id="r1")
        return Unchanged()


class _PlainPlugin(Plugin):
    """A plugin with no ``alert_source`` capability at all."""

    manifest = Manifest(
        name="plain",
        version="0.1.0",
        summary="no capabilities",
        capabilities=frozenset({"region"}),
    )


class _StubSink:
    """A minimal stand-in for whatever instance declares ``alert_sink``."""

    manifest = Manifest(
        name="sink",
        version="0.1.0",
        summary="a stub sink",
        capabilities=frozenset({"alert_sink"}),
    )

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def handle_command(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, dict(args)))
        return {"id": args.get("id"), "state": "queued", "position": 0}


# ------------------------------------------------------------------- harness


def test_the_harness_exposes_ctx_alert_and_records_the_call() -> None:
    harness = PluginHarness(_SourcePlugin)
    try:
        result = harness.plugin.raise_one("warning", "disk full", title="Disk", id="d1")
        assert result == {"state": "queued"}
        assert harness.alerts == [
            {
                "severity": "warning",
                "message": "disk full",
                "title": "Disk",
                "source": None,
                "id": "d1",
                "ttl": None,
            }
        ]
    finally:
        harness.stop()


def test_an_alert_from_a_plugin_reaches_the_alerts_instance() -> None:
    sink = PluginHarness(AlertsPlugin, size=MAIN_SIZE)
    source = PluginHarness(_SourcePlugin, alert_sink=sink.plugin)
    try:
        pushed = source.plugin.raise_one("critical", "on battery", title="UPS", id="ups-1")
        # The same result shape ``alert push`` returns: it *is* that verb.
        assert pushed["id"] == "ups-1"
        assert pushed["state"] in ("queued", "displayed")
        assert sink.command("show")["current"]["id"] == "ups-1"
        assert [alert["id"] for alert in sink.command("list")["alerts"]] == ["ups-1"]
    finally:
        source.stop()
        sink.stop()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"severity": "bogus", "message": "hello"},
        {"severity": "info", "message": "  "},
        {"severity": "info", "message": "hello", "ttl": 0},
    ],
)
def test_ctx_alert_is_validated_like_alert_push(kwargs: dict[str, Any]) -> None:
    sink = PluginHarness(AlertsPlugin, size=MAIN_SIZE)
    source = PluginHarness(_SourcePlugin, alert_sink=sink.plugin)
    try:
        with pytest.raises(CommandError) as excinfo:
            source.plugin.raise_one(**kwargs)
        assert excinfo.value.code == "invalid_input"
        assert sink.command("list")["alerts"] == []
    finally:
        source.stop()
        sink.stop()


def test_a_plugin_without_the_capability_is_refused() -> None:
    harness = PluginHarness(_PlainPlugin)
    try:
        with pytest.raises(PermissionError) as excinfo:
            harness.plugin.ctx.alert("info", "hello")
        assert "alert_source" in str(excinfo.value)
    finally:
        harness.stop()


# ---------------------------------------------------------------- core router


def test_the_router_binds_to_a_worker_and_a_render_alert_reaches_the_sink() -> None:
    sink = _StubSink()
    harness = PluginHarness(_RegionSourcePlugin, size=MAIN_SIZE)
    try:
        worker = RenderWorker(harness.plugin, clock=harness.clock, size=MAIN_SIZE)
        bind_alert_router(worker, AlertRouter(lambda: {"sink": sink}))
        worker.submit(harness.frame_context())
        assert worker.wait_idle(2.0)
        worker.stop()
        assert sink.calls == [
            ("push", {"severity": "warning", "message": "raised while rendering", "id": "r1"})
        ]
    finally:
        harness.stop()


def test_the_router_fails_clearly_when_no_sink_is_configured() -> None:
    router = AlertRouter(lambda: {})
    assert router.sink() is None
    with pytest.raises(AlertRoutingError) as excinfo:
        router.route("info", "nobody is listening")
    assert "alert_sink" in str(excinfo.value)


def test_the_sink_is_resolved_by_capability_not_by_name() -> None:
    """The core names no plugin: any instance declaring ``alert_sink`` works."""
    first, second = _StubSink(), _StubSink()
    router = AlertRouter(lambda: {"renamed-instance": first, "whatever": second})
    assert router.sink() is first, "resolution must be by manifest capability, not by name"
    assert router.sink() is not second
    assert router.route("info", "hello", id="x") == {"id": "x", "state": "queued", "position": 0}
    assert second.calls == []
