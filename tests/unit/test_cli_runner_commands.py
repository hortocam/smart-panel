"""Unit tests for the runner CLI's start/stop waiting helpers (T055).

``_wait_for_stop`` must not report "stopped" while the previous runner still
holds its single-instance lock: ``restart`` spawns the replacement right after
and would otherwise start it into a lock the old process has not released, so it
exits without ever sending a frame. These pin the liveness check that makes the
wait mean "the process is really gone", and the timeout diagnostic that tells a
stalled start apart from one whose child is already dead or a runner that never
answered at all.

The last group pins the other half: a runner that *accepted the connection but
did not answer* (``timeout``) is still a runner. Treating it as gone is what let
a ``start`` hand its child a lock a live-but-silent runner still held, and let
``stop`` report success over a process that had not stopped.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from smart_panel.cli.commands import runner as runner_cmd
from smart_panel.cli.output import CliError
from smart_panel.core import paths

_DEAD_PID = 999_999_999  # far above any real pid on Linux or macOS


class _FakeCtx:
    """A :class:`CliContext` stand-in: a script of outcomes, one per ``call``.

    Each entry is the ``code`` to raise as a :class:`CliError`, or a dict to
    return as the ``runner.status`` payload. The last entry repeats, so a poll
    loop can keep seeing the same outcome.
    """

    def __init__(self, script: list[Any], *, timeout: float = 0.05) -> None:
        self.timeout = timeout
        self._script = list(script)
        self.calls: list[str] = []

    def call(self, cmd: str, args: dict[str, Any] | None = None, *, spoolable: bool = False):
        self.calls.append(cmd)
        outcome = self._script[0]
        if len(self._script) > 1:
            self._script.pop(0)
        if isinstance(outcome, str):
            raise CliError(f"{cmd} failed: {outcome}", code=outcome)
        return outcome


class _FakeChild:
    """A ``Popen`` stand-in: ``poll()`` reports the status it was built with."""

    def __init__(self, status: int | None) -> None:
        self._status = status

    def poll(self) -> int | None:
        return self._status


# ------------------------------------------------------------- process liveness


def test_runner_process_alive_is_true_for_a_live_pid(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(tmp_path))
    paths.runner_pid().write_text(f"{os.getpid()}\n", encoding="utf-8")
    assert runner_cmd._runner_process_alive() is True


def test_runner_process_alive_is_false_without_a_pid_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(tmp_path))
    assert runner_cmd._runner_process_alive() is False


def test_runner_process_alive_is_false_for_a_dead_pid(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(tmp_path))
    paths.runner_pid().write_text(f"{_DEAD_PID}\n", encoding="utf-8")
    assert runner_cmd._runner_process_alive() is False


def test_runner_process_alive_is_false_for_a_garbage_pid_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(tmp_path))
    paths.runner_pid().write_text("not-a-pid\n", encoding="utf-8")
    assert runner_cmd._runner_process_alive() is False


# ------------------------------------------------------------- start diagnostic


def test_start_diagnostic_names_an_unanswered_socket() -> None:
    assert "never answered" in runner_cmd._start_diagnostic(None)


def test_start_diagnostic_without_a_child_is_unchanged() -> None:
    assert runner_cmd._start_diagnostic(None) == " (the runner never answered the control socket)"


def test_start_diagnostic_reports_the_last_status() -> None:
    detail = runner_cmd._start_diagnostic(
        {
            "state": "starting",
            "frames": {"sent": 0},
            "plugins": {"text": {"state": "starting"}},
            "errors": [],
        }
    )
    assert "state='starting'" in detail
    assert "sent=0" in detail
    assert "'text'" in detail


def test_start_diagnostic_reports_a_child_that_is_still_coming_up() -> None:
    detail = runner_cmd._start_diagnostic(None, _FakeChild(None))
    assert "never answered" in detail
    assert "the child is still running" in detail


def test_start_diagnostic_reports_a_child_that_exited() -> None:
    detail = runner_cmd._start_diagnostic(None, _FakeChild(6))
    assert "the child exited with status 6" in detail


# --------------------------------------------------------- start first-frame wait


def test_wait_for_first_frame_fails_fast_when_the_child_exited(tmp_path: Path) -> None:
    """A dead child is never coming up: say so, with its own message."""
    log = tmp_path / "start-stderr.log"
    log.write_text("smart-panel: another runner already holds /tmp/x/runtime/runner.lock\n")
    ctx = _FakeCtx(["runner_not_running"])

    with pytest.raises(CliError) as excinfo:
        runner_cmd._wait_for_first_frame(ctx, 5.0, child=_FakeChild(6), stderr_log=log)

    # The child is this same CLI, so status 6 is the documented conflict code.
    assert excinfo.value.code == "conflict"
    assert "exited with status 6" in excinfo.value.message
    assert "another runner already holds" in excinfo.value.message


def test_wait_for_first_frame_keeps_polling_while_the_child_is_being_slow() -> None:
    """A silent-but-alive child is "not up yet", so the wait runs to the deadline."""
    ctx = _FakeCtx(["timeout", {"state": "running", "frames": {"sent": 3}}])

    data = runner_cmd._wait_for_first_frame(ctx, 5.0, child=_FakeChild(None))

    assert data["frames"]["sent"] == 3
    assert ctx.calls == ["runner.status", "runner.status"]


def test_wait_for_first_frame_reports_a_live_child_at_the_deadline() -> None:
    ctx = _FakeCtx(["runner_not_running"])

    with pytest.raises(CliError) as excinfo:
        runner_cmd._wait_for_first_frame(ctx, 0.05, child=_FakeChild(None))

    assert excinfo.value.code == "timeout"
    assert "the child is still running" in excinfo.value.message


# ------------------------------------------------------------- "is it running?"


def test_is_running_is_true_for_a_silent_but_present_runner() -> None:
    """The socket answered the connect: a runner is there, lock and all."""
    assert runner_cmd._is_running(_FakeCtx(["timeout"])) is True


def test_is_running_is_false_with_no_runner() -> None:
    assert runner_cmd._is_running(_FakeCtx(["runner_not_running"])) is False


# ------------------------------------------------------------------ stop wait


def test_cmd_stop_does_not_report_a_live_but_silent_runner_as_gone(monkeypatch) -> None:
    """A wedged runner has not stopped, so the wait must not call it stopped."""
    monkeypatch.setattr(runner_cmd, "_runner_process_alive", lambda: True)
    ctx = _FakeCtx(["timeout"], timeout=0.05)

    with pytest.raises(CliError) as excinfo:
        runner_cmd.cmd_stop(ctx, ["--timeout", "0.05"])

    assert excinfo.value.code == "timeout"
    assert "did not stop" in excinfo.value.message
    assert ctx.calls[0] == "runner.stop"


def test_cmd_stop_waits_out_a_runner_mid_teardown(monkeypatch) -> None:
    """Mid-teardown the socket is closed while the pid file is still live."""
    monkeypatch.setattr(runner_cmd, "_runner_process_alive", lambda: True)
    ctx = _FakeCtx(["runner_not_running"], timeout=0.05)

    with pytest.raises(CliError) as excinfo:
        runner_cmd.cmd_stop(ctx, ["--timeout", "0.05"])

    assert excinfo.value.code == "timeout"
    assert "did not stop" in excinfo.value.message


def test_cmd_stop_with_no_runner_at_all_keeps_the_documented_exit_three(monkeypatch) -> None:
    monkeypatch.setattr(runner_cmd, "_runner_process_alive", lambda: False)
    ctx = _FakeCtx(["runner_not_running"])

    with pytest.raises(CliError) as excinfo:
        runner_cmd.cmd_stop(ctx, [])

    assert excinfo.value.code == "runner_not_running"
    assert ctx.calls == ["runner.stop"]
