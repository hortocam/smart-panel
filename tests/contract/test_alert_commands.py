"""Contract tests for the ``alert`` verbs (T065).

Shapes come from ``contracts/builtin-plugins.md`` (``alerts``) — the verbs
``push``/``dismiss``/``resolve``/``list``/``show``/``clear-missed``, their
arguments, which of them are ``spoolable``, and the validation that makes an
invalid ``severity``, an empty ``message`` or a ``ttl`` below 1 second exit 2.
A ``message`` over 280 characters is truncated instead, with a warning in the
result. ``show`` (and so every ``status``) publishes ``alerts.missed`` whenever
it is the first caller to notice a timeout. ``assert_conforms`` (FR-047) must
pass for the plugin.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from smart_panel.core.commands import make_dispatch
from smart_panel.core.control import protocol
from smart_panel.core.control.protocol import Request
from smart_panel.plugins.alerts import AlertsPlugin
from smart_panel.plugins.alerts.model import MAX_MESSAGE_CHARS
from smart_panel.sdk.plugin import CommandError
from smart_panel.sdk.testing import PluginHarness, assert_conforms

MAIN_SIZE = (1728, 382)
SPOOLABLE = {"push", "dismiss", "resolve", "clear-missed"}
NOT_SPOOLABLE = {"list", "show"}


class _StubRunner:
    """The sliver of the runner ``plugin.call`` needs, for the exit-code checks."""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin

    def call_plugin(self, instance: str, verb: str, args: Any = None) -> Any:
        return self._plugin.handle_command(verb, dict(args or {}))


def _dispatch(harness: PluginHarness) -> Any:
    return make_dispatch(_StubRunner(harness.plugin))


def _call(harness: PluginHarness, verb: str, **args: Any) -> protocol.Response:
    """The response the control server would write for this verb."""
    request = Request(cmd="plugin.call", args={"instance": "alerts", "verb": verb, "args": args})
    try:
        return _dispatch(harness)(request)
    except protocol.ProtocolError as exc:
        return exc.as_response(request.id)


# ----------------------------------------------------------------- manifest


def test_manifest_declares_the_overlay_and_alert_sink_capabilities() -> None:
    manifest = AlertsPlugin.manifest
    assert manifest.name == "alerts"
    assert {"overlay", "alert_sink"} <= set(manifest.capabilities)
    assert {setting.name for setting in manifest.settings} == {
        "history.retention",
        "durations",
        "max_duration_s",
        "max_wait_s",
        "queue_cap",
        "colors",
        "font",
        "title_size",
        "message_size",
    }
    by_name = {setting.name: setting for setting in manifest.settings}
    assert by_name["durations"].default == {"info": 10, "warning": 20, "critical": 60}
    assert by_name["max_duration_s"].default == 300
    assert by_name["max_wait_s"].default == 120
    assert by_name["queue_cap"].default == 50
    assert by_name["queue_cap"].min == 5 and by_name["queue_cap"].max == 200
    assert by_name["history.retention"].default == "1h"
    assert set(by_name["history.retention"].choices or []) == {"1h", "2h", "4h", "today"}
    assert by_name["colors"].default == {
        "info": "#2E7DD7",
        "warning": "#E6A700",
        "critical": "#D62839",
    }
    assert by_name["title_size"].default == 56 and by_name["message_size"].default == 40


def test_manifest_declares_every_verb_with_its_spoolable_flag() -> None:
    commands = {command.name: command for command in AlertsPlugin.manifest.commands}
    assert set(commands) == SPOOLABLE | NOT_SPOOLABLE
    for name in SPOOLABLE:
        assert commands[name].spoolable is True, name
    for name in NOT_SPOOLABLE:
        assert commands[name].spoolable is False, name

    args = {name: {arg.name for arg in command.args} for name, command in commands.items()}
    assert args["push"] == {"severity", "message", "title", "source", "id", "ttl"}
    assert args["dismiss"] == {"id", "current"}
    assert args["resolve"] == {"id"}
    assert args["list"] == {"state", "limit", "since"}
    assert args["show"] == set()
    assert args["clear-missed"] == {"id"}
    push = {arg.name: arg for arg in commands["push"].args}
    assert set(push["severity"].choices or []) == {"info", "warning", "critical"}


# ------------------------------------------------------------------- results


def test_result_shapes_match_the_builtin_plugin_contract() -> None:
    harness = PluginHarness(AlertsPlugin, size=MAIN_SIZE)
    try:
        pushed = harness.command("push", severity="warning", message="disk full", id="d1")
        assert set(pushed) >= {"id", "state", "position"}
        assert pushed["id"] == "d1"
        assert pushed["state"] in ("queued", "displayed")
        assert isinstance(pushed["position"], int)

        shown = harness.command("show")
        assert set(shown) == {"current", "queue", "missed"}
        assert shown["current"]["id"] == "d1"
        assert shown["missed"] == {"count": 0, "max_severity": None}

        listed = harness.command("list")
        assert set(listed) == {"alerts"}
        assert [alert["id"] for alert in listed["alerts"]] == ["d1"]

        dismissed = harness.command("dismiss", id="d1")
        assert dismissed == {"id": "d1", "outcome": "dismissed"}

        second = harness.command("push", severity="critical", message="again", id="d2")
        resolved = harness.command("resolve", id=second["id"])
        assert resolved == {"id": "d2", "outcome": "resolved", "was": "displayed"}

        assert harness.command("clear-missed") == {"cleared": 0}
    finally:
        harness.stop()


def test_a_long_message_is_truncated_with_a_warning() -> None:
    harness = PluginHarness(AlertsPlugin, size=MAIN_SIZE)
    try:
        pushed = harness.command("push", severity="info", message="x" * 400)
        assert pushed["warning"] == f"message truncated to {MAX_MESSAGE_CHARS} characters"
        assert len(harness.command("list")["alerts"][0]["message"]) == MAX_MESSAGE_CHARS
    finally:
        harness.stop()


@pytest.mark.parametrize(
    "args",
    [
        {"severity": "bogus", "message": "hello"},
        {"severity": "info", "message": "   "},
        {"severity": "info", "message": "hello", "ttl": 0},
        {"message": "hello"},
    ],
)
def test_invalid_push_input_reports_invalid_input_and_exits_2(args: dict[str, Any]) -> None:
    harness = PluginHarness(AlertsPlugin, size=MAIN_SIZE)
    try:
        response = _call(harness, "push", **args)
        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "invalid_input", response.to_obj()
        assert protocol.exit_code_for(response.error.code) == 2
    finally:
        harness.stop()


def test_an_unknown_alert_id_reports_not_found_and_exits_2() -> None:
    harness = PluginHarness(AlertsPlugin, size=MAIN_SIZE)
    try:
        response = _call(harness, "resolve", id="nope")
        assert response.error is not None
        assert response.error.code == "not_found"
        assert protocol.exit_code_for(response.error.code) == 2
    finally:
        harness.stop()


def test_a_plugin_command_error_reports_its_own_code() -> None:
    """A ``CommandError`` code is honoured, so a validation failure is exit 2."""

    class _FailingRunner:
        def call_plugin(self, instance: str, verb: str, args: Any = None) -> Any:
            raise CommandError("invalid_input", "bad input")

    request = Request(
        cmd="plugin.call",
        args={"instance": "alerts", "verb": "push", "args": {}},
    )
    try:
        response = make_dispatch(_FailingRunner())(request)
    except protocol.ProtocolError as exc:
        response = exc.as_response(request.id)
    assert response.error is not None
    assert response.error.code == "invalid_input"
    assert response.exit_code == 2


# --------------------------------------------------------------- conformance


def test_assert_conforms_passes_for_the_alerts_plugin() -> None:
    assert_conforms(AlertsPlugin)


def test_the_plugin_renders_inactive_while_nothing_is_displayed(
    tmp_path: Path,
) -> None:
    harness = PluginHarness(AlertsPlugin, size=MAIN_SIZE)
    try:
        assert harness.render() is None, "an empty overlay must draw nothing"
        harness.command("push", severity="critical", message="covered", ttl=60)
        image = harness.render()
        assert image is not None
        assert image.size == MAIN_SIZE, "the overlay must cover exactly its region"
    finally:
        harness.stop()


def test_the_render_pass_settles_the_countdown_without_waiting_for_a_prune() -> None:
    """A short alert leaves the overlay on the next frame, not at the 60 s prune.

    The retention prune keeps a 60 s cadence; the display countdown must not. A
    clock step far smaller than that cadence has to clear the alert at once, or
    ``clear itself after 60 s`` becomes ``after the next retention pass``.
    """
    harness = PluginHarness(AlertsPlugin, size=MAIN_SIZE)
    try:
        harness.command("push", severity="critical", message="short", ttl=1)
        assert harness.render() is not None
        assert harness.render(advance_s=1.5) is None, "a timed-out alert stayed on screen"
        assert harness.command("show")["missed"]["count"] == 1
    finally:
        harness.stop()


# ---------------------------------------------------- the alerts.missed topic


def test_show_publishes_alerts_missed_when_it_is_the_first_to_notice_a_timeout() -> None:
    """The ``show`` verb is a missed setter too, so it publishes the topic.

    Regression (t_bdb6e057): ``show`` expired the alert through
    ``_advance_locked`` but never published ``alerts.missed``, so a timeout first
    noticed by ``alert show`` stayed invisible to every bus consumer. Nothing
    ticks the plugin here, so ``show`` is the only caller that notices.
    """
    harness = PluginHarness(AlertsPlugin, size=MAIN_SIZE)
    try:
        assert harness.bus.get("alerts.missed").value == {"count": 0, "max_severity": None}
        harness.command("push", severity="warning", message="on battery", id="ups-1", ttl=1)
        harness.clock.advance(2)  # the display time elapses; no render tick yet

        assert harness.command("show")["missed"] == {"count": 1, "max_severity": "warning"}
        assert harness.bus.get("alerts.missed").value == {"count": 1, "max_severity": "warning"}
    finally:
        harness.stop()


def test_status_publishes_alerts_missed_when_it_is_the_first_to_notice_a_timeout() -> None:
    """Every ``status`` reaches ``show`` through ``describe_state``, so it publishes.

    Regression (t_bdb6e057): a ``status`` poll was observed reporting ``missed == 1``
    while the ``alerts.missed`` topic still held the startup ``{count: 0}``, because
    the ``show`` call behind ``describe_state`` marked the alert missed without
    publishing it.
    """
    harness = PluginHarness(AlertsPlugin, size=MAIN_SIZE)
    try:
        harness.command("push", severity="info", message="brief", id="i-1", ttl=1)
        harness.clock.advance(2)  # the display time elapses; no render tick yet

        assert harness.state() == {"current": None, "queued": 0, "missed": 1}
        assert harness.bus.get("alerts.missed").value == {"count": 1, "max_severity": "info"}
    finally:
        harness.stop()
