"""Unit tests for the runner CLI's start/stop waiting helpers (T055).

``_wait_for_stop`` must not report "stopped" while the previous runner still
holds its single-instance lock: ``restart`` spawns the replacement right after
and would otherwise start it into a lock the old process has not released, so it
exits without ever sending a frame. These pin the liveness check that makes the
wait mean "the process is really gone", and the timeout diagnostic that tells a
stalled start apart from a runner that never answered at all.
"""

from __future__ import annotations

import os

from smart_panel.cli.commands import runner as runner_cmd
from smart_panel.core import paths

_DEAD_PID = 999_999_999  # far above any real pid on Linux or macOS


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


def test_start_diagnostic_names_an_unanswered_socket() -> None:
    assert "never answered" in runner_cmd._start_diagnostic(None)


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
