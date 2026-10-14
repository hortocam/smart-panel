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

The same holds when the connection *breaks* rather than closes: an ``EPIPE`` or
``ECONNRESET`` from a send or a read on a socket the runner accepted — a
teardown racing the exchange — is the close-after-accept bucket too, in
``connect``'s hello, in ``call``'s request and in the ``stream`` loop. Only the
``sock.connect`` refusal itself means "there is no runner", so the spool
fallback is for that refusal and nothing else.

A clean EOF on an open follow is deliberately *not* one of these shapes: it is
the pinned normal end of ``logs --follow`` (exit 0) and the reset handling
leaves it alone.
"""

from __future__ import annotations

import os
import shutil
import socket
import struct
import tempfile
import threading
import time
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


# ------------------------------------------- a runner that resets the connection


def _linger_reset(connection: socket.socket) -> None:
    """Close ``connection`` with an immediate RST, not the FIN of ``close()``.

    ``SO_LINGER`` with a zero timeout is how one end aborts a connection; the
    far end then sees ``ECONNRESET``/``EPIPE`` instead of a clean EOF, which is
    the *other* shape's test. This is a runner dying with the socket up — a
    teardown racing the exchange — the residual the close/silence split left
    unclassified.

    Should a platform ignore ``SO_LINGER`` and send the graceful FIN anyway, the
    close degrades into the accepted-then-closed shape and every assertion below
    still holds (it is the same close-after-accept bucket), so the tests are not
    platform-specific.
    """
    connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    connection.close()


def _bound_but_not_listening(path: Path) -> socket.socket:
    """A socket file with no listener: ``connect`` is refused with ``ECONNREFUSED``."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    return sock


def test_a_hello_that_resets_is_not_reported_as_no_runner(short_socket_dir: Path) -> None:
    """Accepted, then reset under the hello exchange: still not "no runner".

    An ``EPIPE``/``ECONNRESET`` on a connection the runner *accepted* is the
    same condition as its close and its silence — a runner is there, mid
    teardown — so it is ``timeout`` (exit 1), never ``runner_not_running``
    (exit 3) for a connection that was accepted.
    """
    path = short_socket_dir / "control.sock"

    def handler(connection: socket.socket) -> None:
        _linger_reset(connection)  # accepted, then reset without reading the hello

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


def test_a_request_that_resets_is_not_reported_as_no_runner(short_socket_dir: Path) -> None:
    """Handshake answered, then the request's send or read hits a reset.

    The request's ``EPIPE`` (the send landed after the reset) and its
    ``ECONNRESET`` (the reset landed first) mean the same thing — the runner was
    there — so both carry ``timeout`` (exit 1), not a raw ``OSError`` out of the
    client and not exit 3.
    """
    path = short_socket_dir / "control.sock"
    hello_reply = protocol.encode_line(protocol.hello_reply("0.1.0", 1).to_obj())

    def handler(connection: socket.socket) -> None:
        connection.makefile("rb").readline()  # the hello request
        connection.sendall(hello_reply)
        _linger_reset(connection)  # reset instead of reading the request

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


def test_a_reset_before_the_request_is_not_reported_as_no_runner(
    short_socket_dir: Path,
) -> None:
    """The send path on its own: the runner dies between hello and request.

    The handshake is completed first and the teardown is driven by the test, so
    the request's ``_send`` — not its read — is the call that raises ``EPIPE``,
    which is what a one-shot command sees when the runner dies mid-exchange.
    That is still the close-after-accept shape, not "there is no runner".
    """
    path = short_socket_dir / "control.sock"
    hello_reply = protocol.encode_line(protocol.hello_reply("0.1.0", 1).to_obj())
    handshaken = threading.Event()
    reset_now = threading.Event()
    reset_done = threading.Event()

    def handler(connection: socket.socket) -> None:
        connection.makefile("rb").readline()  # the hello request
        connection.sendall(hello_reply)
        handshaken.set()
        reset_now.wait(5.0)
        _linger_reset(connection)
        reset_done.set()

    sock, thread = _accepting_server(path, handler)
    ctl = ControlClient(timeout=1, socket_path=path)
    try:
        ctl.connect()  # establish the connection, hello answered
        assert handshaken.wait(1.0)
        reset_now.set()
        assert reset_done.wait(1.0)
        time.sleep(0.1)  # the reset has reached the client's socket by now
        with pytest.raises(client.RunnerNotAnswering) as excinfo:
            ctl.call("runner.status")  # its _send is the call that raises
    finally:
        ctl.close()
        reset_now.set()
        sock.close()
        thread.join(timeout=1.0)
    assert excinfo.value.code == "timeout"
    assert not isinstance(excinfo.value, client.RunnerUnavailable)


def test_a_spoolable_verb_is_not_spooled_for_a_reset_connection(
    short_socket_dir: Path,
) -> None:
    """A reset connection is a runner that is there, so the verb is *not* saved.

    The spool is the answer to ``ENOENT``/``ECONNREFUSED`` — no runner at all.
    An accepted connection that resets is not that, so a spoolable verb must
    fail with ``timeout`` rather than be queued for a runner that never left.
    """
    path = short_socket_dir / "control.sock"
    spool_dir = short_socket_dir / "spool"
    spool_dir.mkdir()

    def handler(connection: socket.socket) -> None:
        _linger_reset(connection)  # accepted, then reset before reading anything

    sock, thread = _accepting_server(path, handler)
    try:
        with pytest.raises(client.RunnerNotAnswering) as excinfo:
            with ControlClient(timeout=1, socket_path=path) as ctl:
                ctl.call("text.set", {"text": "later"}, spoolable=True)
    finally:
        sock.close()
        thread.join(timeout=1.0)
    assert excinfo.value.code == "timeout"
    assert list(spool_dir.iterdir()) == []


def test_a_follow_whose_hello_resets_is_not_reported_as_no_runner(
    short_socket_dir: Path,
) -> None:
    """The accepted-then-reset shape on the streaming path, at the handshake."""
    path = short_socket_dir / "control.sock"

    def handler(connection: socket.socket) -> None:
        _linger_reset(connection)  # accepted, then reset without answering

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


def test_a_follow_whose_request_resets_is_not_reported_as_no_runner(
    short_socket_dir: Path,
) -> None:
    """Handshake answered, then the follow's request send or read resets.

    A reset on an *established* follow is not the pinned clean EOF: a follow
    ends normally when the runner closes the stream (exit 0), while a reset is a
    teardown that broke the stream under the client. It carries ``timeout``
    (exit 1), the same class the one-shot request's reset gets — an ``errno``
    does not change meaning because the socket is streaming.
    """
    path = short_socket_dir / "control.sock"
    hello_reply = protocol.encode_line(protocol.hello_reply("0.1.0", 1).to_obj())

    def handler(connection: socket.socket) -> None:
        connection.makefile("rb").readline()  # the hello request
        connection.sendall(hello_reply)
        _linger_reset(connection)  # reset instead of reading the follow request

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


def test_a_follow_ends_without_an_error_when_the_runner_closes_the_stream(
    short_socket_dir: Path,
) -> None:
    """A clean EOF stays the pinned normal end of ``logs --follow`` (exit 0).

    ``contracts/cli.md``, "Streaming commands", pins "the runner closes the
    stream (EOF)" to 0; the reset handling must not move a graceful close into
    the timeout bucket.
    """
    path = short_socket_dir / "control.sock"
    hello_reply = protocol.encode_line(protocol.hello_reply("0.1.0", 1).to_obj())
    events = [{"event": "log", "n": n} for n in range(3)]

    def handler(connection: socket.socket) -> None:
        reader = connection.makefile("rb")
        reader.readline()  # the hello request
        connection.sendall(hello_reply)
        reader.readline()  # the follow request
        for event in events:
            connection.sendall(protocol.encode_line(event))
        # close without a reset: the normal end of the follow

    sock, thread = _accepting_server(path, handler)
    try:
        with ControlClient(timeout=1, socket_path=path) as ctl:
            received = list(ctl.stream("logs.tail", {"follow": True}))
    finally:
        sock.close()
        thread.join(timeout=1.0)
    assert received == events


def test_a_refused_socket_is_still_runner_not_running(short_socket_dir: Path) -> None:
    """A bound path with no listener is a refusal: exit 3 survives the split.

    ``ECONNREFUSED`` (like ``ENOENT``) is the one shape that means "there is no
    runner"; a bound socket file with nobody listening must keep reporting
    ``runner_not_running``.
    """
    path = short_socket_dir / "control.sock"
    sock = _bound_but_not_listening(path)
    try:
        with pytest.raises(client.RunnerUnavailable) as excinfo:
            with ControlClient(timeout=1, socket_path=path) as ctl:
                ctl.call("runner.status")
    finally:
        sock.close()
    assert excinfo.value.code == "runner_not_running"
    assert not isinstance(excinfo.value, client.RunnerNotAnswering)


def test_a_follow_of_a_refused_socket_is_still_runner_not_running(
    short_socket_dir: Path,
) -> None:
    """The follow path keeps ``ECONNREFUSED`` at the pinned exit 3 too."""
    path = short_socket_dir / "control.sock"
    sock = _bound_but_not_listening(path)
    try:
        with pytest.raises(client.RunnerUnavailable) as excinfo:
            with ControlClient(timeout=1, socket_path=path) as ctl:
                list(ctl.stream("logs.tail", {"follow": True}))
    finally:
        sock.close()
    assert excinfo.value.code == "runner_not_running"
    assert not isinstance(excinfo.value, client.RunnerNotAnswering)
