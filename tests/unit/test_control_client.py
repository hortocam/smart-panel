"""Unit tests for ``ControlClient``'s two "no answer" shapes (T048).

``contracts/cli.md`` gives exit 3 to "no runner is running" and exit 1 to the
``timeout`` code. A socket that accepts the connection but never answers the
hello is a different condition from a missing socket: a runner *is* there — it
is starved, or half-way through teardown, and still holds its single-instance
lock. Reporting ``runner_not_running`` for it is what let a ``start`` walk a
second runner into the lock the live one still held (its child exited 6 and the
parent called that a timeout).

Only a connect refused with ``ENOENT``/``ECONNREFUSED`` — no listener at all —
stays :class:`RunnerUnavailable` (exit 3). A connection that is accepted and
then goes unanswered is :class:`RunnerNotAnswering` (``timeout``, exit 1),
whether the silence falls on the hello handshake or on the request that follows.
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from smart_panel.core.control import client, protocol
from smart_panel.core.control.client import ControlClient


@pytest.fixture
def short_socket_dir() -> Iterator[Path]:
    """A short temp base: AF_UNIX paths are capped near 108 bytes."""
    for candidate in ("/tmp", tempfile.gettempdir()):
        if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            base = Path(tempfile.mkdtemp(prefix="sp-cli-", dir=candidate))
            break
    else:
        raise RuntimeError("no writable temporary directory for the socket tests")
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _listen(path: Path) -> socket.socket:
    """A socket that accepts connections into its backlog and never answers."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen(1)
    return sock


def test_a_missing_socket_is_still_runner_not_running(short_socket_dir: Path) -> None:
    """There is no listener at all: the documented exit-3 condition."""
    with pytest.raises(client.RunnerUnavailable) as excinfo:
        with ControlClient(timeout=1, socket_path=short_socket_dir / "gone.sock") as ctl:
            ctl.call("runner.status")
    assert excinfo.value.code == "runner_not_running"
    assert not isinstance(excinfo.value, client.RunnerNotAnswering)


def test_a_socket_that_never_answers_the_hello_is_not_reported_as_no_runner(
    short_socket_dir: Path,
) -> None:
    """The socket is there and accepts; the runner behind it is not answering."""
    path = short_socket_dir / "control.sock"
    sock = _listen(path)
    try:
        with pytest.raises(client.RunnerNotAnswering) as excinfo:
            with ControlClient(timeout=0.2, socket_path=path) as ctl:
                ctl.call("runner.status")
    finally:
        sock.close()
    assert excinfo.value.code == "timeout"
    assert not isinstance(excinfo.value, client.RunnerUnavailable)
    assert "did not answer" in excinfo.value.message


def test_a_runner_that_answers_the_hello_but_not_the_request_is_not_no_runner(
    short_socket_dir: Path,
) -> None:
    """Silence after a good handshake is the same condition, not "no runner"."""
    path = short_socket_dir / "control.sock"
    sock = _listen(path)
    hello_reply = protocol.encode_line(protocol.hello_reply("0.1.0", 1).to_obj())
    done = threading.Event()

    def serve() -> None:
        connection, _ = sock.accept()
        with connection:
            connection.makefile("rb").readline()  # the hello request
            connection.sendall(hello_reply)
            done.wait(5.0)  # never answer the command itself

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with pytest.raises(client.RunnerNotAnswering) as excinfo:
            with ControlClient(timeout=0.2, socket_path=path) as ctl:
                ctl.call("runner.status")
    finally:
        sock.close()
        done.set()
        thread.join(timeout=1.0)
    assert excinfo.value.code == "timeout"
