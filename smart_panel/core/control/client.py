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


class ImpersonationRefused(ControlClientError):
    """The runtime directory or socket is owned by an unrelated account."""

    code = "not_permitted"


class SpoolRefused(ControlClientError):
    """The runner is down and this account may not create the runtime directory."""

    code = "runner_not_running"


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
            OSError: The socket is missing or refusing connections.
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
        self._send(protocol.hello_request(self.client_name).to_obj())
        reply = self._receive()
        if reply is None:
            self.close()
            raise RunnerUnavailable("the runner closed the connection during the handshake")
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
                raise RunnerUnavailable(f"the runner is not running: {exc}") from exc
        request = protocol.Request(cmd=cmd, args=dict(args or {}), id=next(self._ids))
        self._send(request.to_obj())
        reply = self._receive()
        if reply is None:
            raise RunnerUnavailable("the runner closed the connection")
        return protocol.Response.from_obj(reply)

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
