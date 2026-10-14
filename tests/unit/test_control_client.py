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
then gives no answer is :class:`RunnerNotAnswering` (``timeout``, exit 1),
whether the silence falls on the hello handshake, on the request that follows,
or because the runner closed the connection it had just accepted. The streamed
``logs --follow`` path follows the same split, keeping ``ENOENT``/
``ECONNREFUSED`` at exit 3 (``contracts/cli.md``, "Streaming commands").
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading
from collections.abc import Callable, Iterator
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


def _accepting_server(
    path: Path, handler: Callable[[socket.socket], None]
) -> tuple[socket.socket, threading.Thread]:
    """A listening socket plus a thread that runs ``handler`` on one connection."""
    sock = _listen(path)

    def serve() -> None:
        try:
            connection, _ = sock.accept()
        except OSError:
            return
        with connection:
            handler(connection)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return sock, thread


def test_a_follow_with_a_missing_socket_is_still_runner_not_running(
    short_socket_dir: Path,
) -> None:
    """There is no listener at all: the follow keeps the documented exit 3.

    ``contracts/cli.md``, "Streaming commands", pins ``ENOENT``/``ECONNREFUSED``
    on the follow path to 3 ``runner_not_running``; the silence split must not
    move it.
    """
    with pytest.raises(client.RunnerUnavailable) as excinfo:
        with ControlClient(timeout=1, socket_path=short_socket_dir / "gone.sock") as ctl:
            list(ctl.stream("logs.tail", {"follow": True}))
    assert excinfo.value.code == "runner_not_running"
    assert not isinstance(excinfo.value, client.RunnerNotAnswering)


def test_a_follow_of_a_silent_socket_is_not_reported_as_no_runner(
    short_socket_dir: Path,
) -> None:
    """The socket accepts the connect and never answers the hello.

    ``timeout`` bounds the connect and hello handshake only, so this is the
    follow's one *bounded* silence: a runner that is there — starved, or
    mid-teardown, still holding its single-instance lock — and did not answer is
    ``timeout`` (exit 1), not ``runner_not_running`` (exit 3).
    """
    path = short_socket_dir / "control.sock"
    sock = _listen(path)
    try:
        with pytest.raises(client.RunnerNotAnswering) as excinfo:
            with ControlClient(timeout=0.2, socket_path=path) as ctl:
                list(ctl.stream("logs.tail", {"follow": True}))
    finally:
        sock.close()
    assert excinfo.value.code == "timeout"
    assert not isinstance(excinfo.value, client.RunnerUnavailable)
    assert "did not answer" in excinfo.value.message


def test_a_runner_that_closes_during_the_handshake_is_not_reported_as_no_runner(
    short_socket_dir: Path,
) -> None:
    """Accepted, then closed before the hello was answered: a runner is there.

    The connect succeeded, so a runner *is* running — it is going away, not
    absent. Only ``ENOENT``/``ECONNREFUSED`` mean "there is no runner" (exit 3);
    a connection that was accepted and then closed carries ``timeout`` (exit 1).
    """
    path = short_socket_dir / "control.sock"

    def handler(connection: socket.socket) -> None:
        connection.makefile("rb").readline()  # the hello request
        # close without answering it

    sock, thread = _accepting_server(path, handler)
    try:
        with pytest.raises(client.RunnerNotAnswering) as excinfo:
            with ControlClient(timeout=1, socket_path=path) as ctl:
                ctl.call("runner.status")
    finally:
        sock.close()
        thread.join(timeout=1.0)
    assert excinfo.value.code == "timeout"
    assert not isinstance(excinfo.value, client.RunnerUnavailable)


def test_a_runner_that_closes_after_the_hello_is_not_reported_as_no_runner(
    short_socket_dir: Path,
) -> None:
    """Handshake answered, then the connection closed: still not "no runner"."""
    path = short_socket_dir / "control.sock"
    hello_reply = protocol.encode_line(protocol.hello_reply("0.1.0", 1).to_obj())

    def handler(connection: socket.socket) -> None:
        reader = connection.makefile("rb")
        reader.readline()  # the hello request
        connection.sendall(hello_reply)
        reader.readline()  # the command request
        # close without answering it

    sock, thread = _accepting_server(path, handler)
    try:
        with pytest.raises(client.RunnerNotAnswering) as excinfo:
            with ControlClient(timeout=1, socket_path=path) as ctl:
                ctl.call("runner.status")
    finally:
        sock.close()
        thread.join(timeout=1.0)
    assert excinfo.value.code == "timeout"
    assert not isinstance(excinfo.value, client.RunnerUnavailable)


def test_a_follow_of_a_runner_that_closes_the_handshake_is_not_reported_as_no_runner(
    short_socket_dir: Path,
) -> None:
    """The same accepted-then-closed shape on the streaming path."""
    path = short_socket_dir / "control.sock"

    def handler(connection: socket.socket) -> None:
        connection.makefile("rb").readline()  # the hello request
        # close without answering it

    sock, thread = _accepting_server(path, handler)
    try:
        with pytest.raises(client.RunnerNotAnswering) as excinfo:
            with ControlClient(timeout=1, socket_path=path) as ctl:
                list(ctl.stream("logs.tail", {"follow": True}))
    finally:
        sock.close()
        thread.join(timeout=1.0)
    assert excinfo.value.code == "timeout"
    assert not isinstance(excinfo.value, client.RunnerUnavailable)
