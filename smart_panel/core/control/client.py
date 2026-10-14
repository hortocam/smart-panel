"""The CLI-side control client with its spool fallback (T048).

``ControlClient`` speaks the handshake and request/response of
``contracts/control-protocol.md`` over ``<runtime>/control.sock`` and, when the
runner is not there, may spool a *spoolable* verb instead of failing
(``contracts/cli.md``, "Push commands that were spooled ...").

Two things are deliberately strict. First, the impersonation guard: before
connecting, the runtime directory and the socket must be owned by this account,
by root, or by the uid in ``$SMART_PANEL_RUNNER_UID`` — otherwise another local
account could stand up a socket at the shared path and answer as the runner.
Second, the CLI never creates the runtime directory: with no runtime spool the
owner's push waits in ``<home>/spool-pending`` (imported by the runner on start),
and any other account is refused with exit code 3.

The client also keeps two "no answer" shapes apart, because the CLI's start/stop
waiting turns on the difference. A refused connect (``ENOENT``,
``ECONNREFUSED``) is :class:`RunnerUnavailable` — exit 3, no runner — while a
connection that is *accepted* and then gives no answer (the hello handshake
lives inside :meth:`ControlClient.connect`) is :class:`RunnerNotAnswering` —
exit 1 ``timeout``, a runner that is there but starved, mid-teardown, or gone
the moment it accepted, still holding its single-instance lock. That split
covers every path out of the client, the streamed ``logs --follow`` included.

A connection that *breaks* rather than closes is in the accepted bucket too: an
``EPIPE`` or ``ECONNRESET`` from a send or a read on a socket the runner
accepted — a teardown racing the exchange, in ``connect``'s hello, ``call``'s
request or the ``stream`` loop — is :class:`RunnerNotAnswering`, never
:class:`RunnerUnavailable` and never a raw ``OSError``. Only the
``sock.connect`` refusal itself means "there is no runner", so only that
refusal spools. A clean EOF is still the pinned normal end of ``logs --follow``
(exit 0); a reset is not an EOF.
"""

from __future__ import annotations

import errno
import itertools
import os
import socket
from pathlib import Path
from typing import Any

from smart_panel import __version__
from smart_panel.core import paths
from smart_panel.core.control import protocol, spool
from smart_panel.core.control.spool import SpoolFull as SpoolFull  # re-exported for the CLI

CLIENT_NAME = f"smart-panel-cli/{__version__}"
DEFAULT_TIMEOUT_S = 5.0
_SPOOLABLE_ERRNOS = (errno.ENOENT, errno.ECONNREFUSED)


class ControlClientError(Exception):
    """A client-side failure; :attr:`code` is the error code the CLI reports."""

    code = "internal"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.message = message
        if code is not None:
            self.code = code
        super().__init__(message)


class RunnerUnavailable(ControlClientError):
    """No runner is listening and the command cannot be saved for later."""

    code = "runner_not_running"


class RunnerNotAnswering(ControlClientError):
    """A runner accepted the connection but gave no answer.

    Deliberately not :class:`RunnerUnavailable`: there the socket is not there
    at all, while here the connect succeeded, so a runner *is* running — it is
    starved, half-way through teardown, or closes the connection it just
    accepted, and still holds the single-instance lock. Reporting that as
    ``runner_not_running`` (exit 3) tells the caller the runner is gone and
    walks a second ``start`` into the lock the live process still holds, so this
    carries the documented ``timeout`` code (exit 1) instead: "the runner did
    not answer", not "there is no runner".
    """

    code = "timeout"


class ImpersonationRefused(ControlClientError):
    """The runtime directory or socket is owned by an unrelated account."""

    code = "not_permitted"


class SpoolRefused(ControlClientError):
    """The runner is down and this account may not create the runtime directory."""

    code = "runner_not_running"


def _is_silence(exc: OSError) -> bool:
    """Whether ``exc`` is an unanswered socket rather than a refused one.

    ``socket.timeout`` is :class:`TimeoutError`, which is an :class:`OSError`:
    a live-but-silent runner reaches the same ``except OSError`` arm as a
    missing socket, so the two have to be told apart explicitly here.
    """
    return isinstance(exc, TimeoutError)


class ControlClient:
    """A single connection to the runner, opened lazily by :meth:`call`.

    Args:
        timeout: Seconds to wait for the connect and for each reply.
        socket_path: Override the socket location; defaults to
            ``<runtime>/control.sock``.
        client_name: The identifier sent in the hello.
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        socket_path: str | os.PathLike[str] | None = None,
        client_name: str = CLIENT_NAME,
    ) -> None:
        self.timeout = float(timeout)
        self.socket_path = (
            Path(socket_path) if socket_path is not None else paths.control_sock()
        )
        self.client_name = client_name
        self.server_hello: protocol.Hello | None = None
        self._sock: socket.socket | None = None
        self._reader: Any = None
        self._ids = itertools.count(1)

    def __enter__(self) -> ControlClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- connection -------------------------------------------------------- #

    def _runner_uid(self) -> int:
        """The runner's uid: ``$SMART_PANEL_RUNNER_UID`` else this account."""
        raw = os.environ.get("SMART_PANEL_RUNNER_UID")
        if raw:
            try:
                return int(raw)
            except ValueError:
                pass
        return os.geteuid()

    def _guard_owner(self) -> None:
        """Refuse a runtime directory or socket owned by an unrelated account."""
        allowed = {os.geteuid(), 0, self._runner_uid()}
        for path in (self.socket_path.parent, self.socket_path):
            try:
                owner = path.stat().st_uid
            except FileNotFoundError:
                continue
            if owner not in allowed:
                raise ImpersonationRefused(
                    f"{path} is owned by uid {owner}, not this account, root or the runner"
                )

    def connect(self) -> None:
        """Open the socket and complete the hello handshake.

        Raises:
            ImpersonationRefused: The path is owned by an unrelated account.
            OSError: The ``connect`` itself was refused (``ENOENT`` when there
                is no socket at all, ``ECONNREFUSED`` when nothing is
                listening) — the only "there is no runner" shape.
            RunnerNotAnswering: The socket accepted but the hello went
                unanswered, the runner closed the connection first, or the
                hello's send/read raised ``EPIPE``/``ECONNRESET``.
            ControlClientError: The runner answered the handshake with an error.
        """
        self._guard_owner()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(self.socket_path))
        except OSError:
            sock.close()
            raise
        self._sock = sock
        self._reader = sock.makefile("rb")
        try:
            self._send(protocol.hello_request(self.client_name).to_obj())
            reply = self._receive()
        except OSError as exc:
            # The hello rides on a connection the runner accepted, so a send or
            # read failure here is never "no runner": silence keeps the caller's
            # own arm (``_not_answering``), and every other ``OSError`` —
            # ``EPIPE``/``ECONNRESET`` from a teardown racing the exchange — is
            # the close-after-accept bucket.
            self.close()
            if _is_silence(exc):
                raise
            raise self._closed_after_accept() from exc
        if reply is None:
            self.close()
            raise self._closed_after_accept()
        if "hello" not in reply:
            response = protocol.Response.from_obj(reply)
            self.close()
            if response.error is not None:
                raise ControlClientError(response.error.message, code=response.error.code)
            raise ControlClientError("the runner sent an unexpected handshake reply")
        self.server_hello = protocol.parse_hello(reply)

    def close(self) -> None:
        """Close the connection, if one is open."""
        if self._reader is not None:
            try:
                self._reader.close()
            except OSError:
                pass
            self._reader = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # -- requests ---------------------------------------------------------- #

    def call(
        self,
        cmd: str,
        args: dict[str, Any] | None = None,
        *,
        spoolable: bool = False,
        received_at: float | None = None,
    ) -> protocol.Response:
        """Send one command and return its response.

        Args:
            cmd: The namespaced command, for example ``runner.status``.
            args: The command's arguments.
            spoolable: Whether the verb's manifest marks it spoolable. When the
                connect fails with ``ENOENT`` or ``ECONNREFUSED`` and this is
                true, the command is spooled instead of raising.
            received_at: Receive time to record for a spooled command.

        Raises:
            RunnerUnavailable: No runner answers and the command is not spoolable.
            RunnerNotAnswering: The socket accepted the connection but the
                runner did not answer in time, closed it, or the request's
                send/read raised ``EPIPE``/``ECONNRESET`` (``timeout``, exit 1).
            SpoolRefused: No runner answers, the runtime directory is gone, and
                this account may not create it.
            SpoolFull: The spool already holds the maximum number of entries.
        """
        if self._sock is None:
            try:
                self.connect()
            except OSError as exc:
                if spoolable and exc.errno in _SPOOLABLE_ERRNOS:
                    return self._spool(cmd, args, received_at=received_at)
                if _is_silence(exc):
                    raise self._not_answering(exc) from exc
                raise RunnerUnavailable(f"the runner is not running: {exc}") from exc
        request = protocol.Request(cmd=cmd, args=dict(args or {}), id=next(self._ids))
        try:
            self._send(request.to_obj())
            reply = self._receive()
        except OSError as exc:
            # Same split as the handshake, now mid-session: the connection was
            # established, so a request send or read that raises ``EPIPE``/
            # ``ECONNRESET`` is the close-after-accept bucket (``timeout``, exit
            # 1), not a raw ``OSError`` and never "there is no runner".
            if _is_silence(exc):
                raise self._not_answering(exc) from exc
            raise self._closed_after_accept() from exc
        if reply is None:
            raise self._closed_after_accept()
        return protocol.Response.from_obj(reply)

    def _not_answering(self, cause: OSError) -> RunnerNotAnswering:
        """The error for a socket that accepted the connect but stayed silent.

        The handshake lives inside :meth:`connect`, so both a connect timeout
        and an unanswered hello (and an unanswered command) arrive here as an
        :class:`OSError`; all three mean the same thing — a runner is there and
        is not answering — and none of them mean "no runner".
        """
        return RunnerNotAnswering(
            f"the runner did not answer the control socket within {self.timeout:g} s: {cause}"
        )

    def _closed_after_accept(self) -> RunnerNotAnswering:
        """The error for a runner that accepted the connection, then lost it.

        The same bucket as silence: the connect succeeded, so a runner *is*
        there — it is gone the moment it accepted (mid-teardown, or a handler
        that failed), or it died with the socket up so a later send or read met
        ``EPIPE``/``ECONNRESET`` — and none of that is the ``ENOENT``/
        ``ECONNREFUSED`` "there is no runner" of exit 3, but the documented
        ``timeout`` (exit 1).
        """
        return RunnerNotAnswering(
            "the runner accepted the control connection and then closed it without answering"
        )

    def stream(self, cmd: str, args: dict[str, Any] | None = None) -> Any:
        """Send one command and yield each event line until the server closes.

        Used for ``logs.tail --follow``: the server answers with a sequence of
        ``{"event": "log", ...}`` objects (``contracts/control-protocol.md``)
        rather than a single response, so the raw decoded objects are yielded.

        ``timeout`` bounds the connect and hello handshake only. A follow is
        expected to stay parked while the server has nothing to say, so the
        socket is switched to blocking reads for the stream itself
        (``contracts/cli.md``, "Streaming commands") — an idle gap must not exit
        the follow. A *refused* connect (``ENOENT``/``ECONNREFUSED``) is mapped
        to :class:`RunnerUnavailable` so a follow with no runner reports
        ``runner_not_running`` (exit 3), while a connect that was *accepted* and
        then gave no answer — a silent hello, a close, or a hello that reset —
        is :class:`RunnerNotAnswering` (``timeout``, exit 1), exactly as
        :meth:`call` splits them.

        The pinned streaming table gives exit 0 to "the runner closes the stream
        (EOF)", and that is a clean end of the read below (``reply is None``).
        A *reset* is not an EOF: the runner's socket was torn down under the
        follow (``EPIPE``/``ECONNRESET`` on the request or on an event read), so
        it is the close-after-accept class — ``timeout`` (exit 1), the same
        code the same errno gets on the one-shot path. Deciding it the other
        way would report a broken stream as a normal end of ``--follow``.

        Raises:
            RunnerUnavailable: No runner is listening on the socket.
            RunnerNotAnswering: The socket accepted but the answer never came,
                or the connection broke mid-stream (``EPIPE``/``ECONNRESET``).
            ImpersonationRefused: The path is owned by an unrelated account.
            ControlClientError: The runner answered the handshake with an error.
        """
        if self._sock is None:
            try:
                self.connect()
            except OSError as exc:
                if _is_silence(exc):
                    raise self._not_answering(exc) from exc
                raise RunnerUnavailable(f"the runner is not running: {exc}") from exc
        assert self._sock is not None
        self._sock.settimeout(None)
        request = protocol.Request(cmd=cmd, args=dict(args or {}), id=next(self._ids))
        try:
            self._send(request.to_obj())
        except OSError as exc:
            # The follow's request rides the same established connection as a
            # one-shot's, so a send that raises EPIPE/ECONNRESET is the
            # close-after-accept class too, never a raw OSError out of stream().
            # The split is kept uniform with call()/connect() even though the
            # socket is blocking (so no timeout) by this point.
            if _is_silence(exc):
                raise self._not_answering(exc) from exc
            raise self._closed_after_accept() from exc
        while True:
            try:
                reply = self._receive()
            except OSError as exc:
                # The socket is blocking now, so a teardown (EPIPE/ECONNRESET) is
                # what reaches this, and it is not the pinned clean EOF: a follow
                # is ended normally by the runner *closing* the stream (exit 0),
                # while a reset broke it. That is the same class as the one-shot
                # request's reset (``timeout``, exit 1) — an errno does not change
                # meaning because the socket streams — on the same split the rest
                # of the client uses, whether or not the read can time out.
                if _is_silence(exc):
                    raise self._not_answering(exc) from exc
                raise self._closed_after_accept() from exc
            if reply is None:
                return
            yield reply

    def _spool(
        self, cmd: str, args: dict[str, Any] | None, *, received_at: float | None
    ) -> protocol.Response:
        runtime_spool = self.socket_path.parent / "spool"
        if runtime_spool.is_dir():
            target = runtime_spool
        elif os.geteuid() == self._runner_uid():
            target = paths.pending_spool()
            target.mkdir(parents=True, exist_ok=True)
        else:
            raise SpoolRefused(
                "the runner is not running and this account may not create the runtime directory"
            )
        path = spool.write_entry(target, cmd, args or {}, received_at=received_at)
        return protocol.Response.success(
            None, {"saved_for_later": True, "spool_id": path.stem}
        )

    def _send(self, obj: dict[str, Any]) -> None:
        assert self._sock is not None
        self._sock.sendall(protocol.encode_line(obj))

    def _receive(self) -> dict[str, Any] | None:
        assert self._reader is not None
        raw = self._reader.readline(protocol.MAX_LINE_BYTES + 2)
        if not raw:
            return None
        return protocol.decode_line(raw)
