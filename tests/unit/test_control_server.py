"""Unit tests for the control server's connection handling (T047).

``ControlServer._handle`` runs one thread per connection. A handler failure
that is *not* an ``OSError`` used to end that thread with no record anywhere:
the client saw a silent close (the CLI turns that into ``runner_not_running``,
exit 3, "the runner closed the connection during the handshake") and the runner
log had no line about it. The detached runner also discards its stderr
(``subprocess.DEVNULL`` in ``smart_panel/cli/commands/runner.py``), so the
traceback went nowhere — the failure that made PR #43 need a probe and four CI
runs (run 38488852974).

These tests pin the fix: an unexpected handler failure reaches the runner log
as ``control_connection_failed`` before the connection is closed, while an
ordinary disconnect (``OSError``) stays quiet.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any

import pytest

from smart_panel.core.control import peercred, protocol
from smart_panel.core.control.access import Policy
from smart_panel.core.control.server import ControlServer
from smart_panel.core.logging import RunnerLog


def _echo_dispatch(request: protocol.Request) -> protocol.Response:
    return protocol.Response.success(request.id, {"echo": request.cmd})


def _server(log: RunnerLog) -> ControlServer:
    return ControlServer(
        _echo_dispatch,
        policy_provider=lambda: Policy(runner_uid=os.geteuid()),
        log=log,
    )


def _hello() -> bytes:
    return protocol.encode_line({"hello": {"v": 1, "client": "test/1"}})


def _failures(log: RunnerLog) -> list[dict[str, Any]]:
    return [event for event in log.recent() if event["event"] == "control_connection_failed"]


@pytest.fixture
def log(tmp_path: Path):
    runner_log = RunnerLog(path=tmp_path / "runner.log", ring_size=16)
    try:
        yield runner_log
    finally:
        runner_log.close()


def test_a_connection_handler_failure_reaches_the_log(
    log: RunnerLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plant the production failure: ``get_peer_groups`` raises ``OverflowError``.

    That is exactly what happened on macOS for a real second uid (run
    38488852974): the traceback ran ``server.py:176`` -> ``peercred.py:70`` and
    ended in ``OverflowError: Python int too large to convert to C int``. Before
    the fix the exception escaped ``_handle`` (which caught only ``OSError``),
    so the peer got a bare close and the log stayed silent.
    """
    server = _server(log)

    def boom(uid: int, gid: int) -> list[int]:
        raise OverflowError("Python int too large to convert to C int")

    monkeypatch.setattr(peercred, "get_peer", lambda conn: (os.geteuid(), os.getegid()))
    monkeypatch.setattr(peercred, "get_peer_groups", boom)

    peer_sock, server_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    peer = peer_sock.makefile("rb")
    try:
        peer_sock.sendall(_hello())
        server._handle(server_sock)
        # The client is told nothing and gets a bare close: the symptom that
        # made this invisible. The log is what has to carry the explanation.
        assert peer.readline() == b""
        failures = _failures(log)
        assert failures, "a connection-handler failure left no trace in the log"
        assert "OverflowError" in failures[0]["error"]
        assert "too large" in failures[0]["error"]
    finally:
        peer.close()
        peer_sock.close()


def test_an_ordinary_disconnect_is_not_logged_as_a_failure(
    log: RunnerLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal client disconnect surfaces as ``OSError`` and stays quiet.

    ``get_peer`` documents ``OSError`` for a socket without peer credentials,
    and a client that goes away mid-write raises ``BrokenPipeError``; neither is
    a server defect, so the new log line must not fire for them.
    """

    def no_credentials(conn: socket.socket) -> tuple[int, int]:
        raise OSError("peer credentials unavailable")

    server = _server(log)
    monkeypatch.setattr(peercred, "get_peer", no_credentials)

    peer_sock, server_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    peer = peer_sock.makefile("rb")
    try:
        peer_sock.sendall(_hello())
        server._handle(server_sock)
        assert peer.readline() == b""
        assert _failures(log) == []
    finally:
        peer.close()
        peer_sock.close()
