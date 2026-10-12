"""The runner-side control socket (T047).

One Unix stream socket at ``<runtime>/control.sock`` serves every account
(``contracts/control-protocol.md``). A connection is authorized by kernel peer
credentials, not by file permissions: a denied peer gets exactly one
``not_permitted`` response, a ``denied uid=<n>`` log line, then the socket is
closed. Commands that change the allow-list or secrets need the runner's uid or
root on top of that.

Commands from every connection run through one serialized queue (a single
worker), so two commands that arrive together get a defined order and never
interleave. Every command has a deadline (2 s by default); on overrun the client
is told ``timeout`` while the worker finishes in the background. The deadline
bounds the response, not the worker: the command is not cancelled, so a stuck
one still holds the serialized worker and delays the commands behind it.
``logs.tail --follow`` is the one streaming command: the dispatch returns an
iterator and its events are written as they arrive, until the client closes.
"""

from __future__ import annotations

import os
import socket
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from smart_panel import __version__
from smart_panel.core import paths
from smart_panel.core.control import access, peercred, protocol

SOCKET_MODE = 0o666
DEFAULT_COMMAND_TIMEOUT_S = 2.0
_ACCEPT_TIMEOUT_S = 0.25
_BACKLOG = 16
_READ_SIZE = protocol.MAX_LINE_BYTES + 2


class _LineTooLong(Exception):
    """A line past the 1 MiB limit; the stream cannot be resynchronised."""


class ControlServer:
    """The control socket server.

    Args:
        dispatch: ``dispatch(request) -> Response``, or an iterator of event
            objects for a streaming command. It runs on the serialized worker.
        policy_provider: Returns the current :class:`~.access.Policy`.
        socket_path: Override the socket location; defaults to
            ``<runtime>/control.sock``.
        log: Optional runner log for denials and dispatch failures.
        plugins_rev: The plugin-set revision announced in the hello; an int or a
            callable returning one.
        command_timeout_s: Deadline for every command's response.
    """

    def __init__(
        self,
        dispatch: Callable[[protocol.Request], Any],
        *,
        policy_provider: Callable[[], access.Policy],
        socket_path: str | os.PathLike[str] | None = None,
        log: Any = None,
        server_version: str = __version__,
        plugins_rev: int | Callable[[], int] = 0,
        command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
    ) -> None:
        self._dispatch = dispatch
        self._policy_provider = policy_provider
        self._socket_path = Path(socket_path) if socket_path is not None else None
        self._log = log
        self._server_version = server_version
        self._plugins_rev = plugins_rev
        self._command_timeout_s = float(command_timeout_s)
        self._queue = ThreadPoolExecutor(max_workers=1, thread_name_prefix="control-dispatch")
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    @property
    def socket_path(self) -> Path:
        return self._socket_path if self._socket_path is not None else paths.control_sock()

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> Path:
        """Create the runtime directory and socket, then accept in the background."""
        paths.ensure_runtime()
        path = self.socket_path
        if path.exists():
            path.unlink()  # a socket left behind by a killed runner
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        os.chmod(path, SOCKET_MODE)
        listener.listen(_BACKLOG)
        listener.settimeout(_ACCEPT_TIMEOUT_S)
        self._listener = listener
        self._thread = threading.Thread(
            target=self.serve_forever, name="control-accept", daemon=True
        )
        self._thread.start()
        return path

    def serve_forever(self) -> None:
        """Accept connections until :meth:`stop`."""
        listener = self._listener
        if listener is None:
            return
        while not self._stopping.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def stop(self) -> None:
        """Stop accepting, close the queue and remove the socket."""
        self._stopping.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self._queue.shutdown(wait=False, cancel_futures=True)
        path = self.socket_path
        try:
            path.unlink()
        except OSError:
            pass

    # -- one connection ---------------------------------------------------- #

    def _handle(self, conn: socket.socket) -> None:
        try:
            reader = conn.makefile("rb")
            try:
                self._serve_connection(conn, reader)
            finally:
                reader.close()
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _serve_connection(self, conn: socket.socket, reader: Any) -> None:
        try:
            raw = self._read_line(reader)
        except _LineTooLong:
            self._reject_line(conn)
            return
        if raw is None:
            return
        try:
            protocol.parse_hello(protocol.decode_line(raw))
        except protocol.ProtocolError as exc:
            # The first line must be the hello; anything else is a mismatch.
            self._write(conn, exc.as_response(None))
            return

        try:
            uid, gid = peercred.get_peer(conn)
        except OSError:
            return
        groups = peercred.get_peer_groups(uid, gid)
        policy = self._policy_provider()
        if not access.is_allowed(uid, groups, policy):
            self._write(
                conn,
                protocol.Response.failure(
                    None, "not_permitted", "this account is not allowed to use the control channel"
                ),
            )
            if self._log is not None:
                self._log.warning("denied", uid=uid, message=f"denied uid={uid}")
            return

        reply = protocol.hello_reply(self._server_version, self._plugins_rev_value())
        self._write(conn, reply)

        while True:
            try:
                raw = self._read_line(reader)
            except _LineTooLong:
                self._reject_line(conn)
                return
            if raw is None:
                return
            try:
                request = protocol.parse_request(protocol.decode_line(raw))
            except protocol.ProtocolError as exc:
                self._write(conn, exc.as_response(None))
                continue
            if access.authorize(uid, groups, policy, request.cmd) is not None:
                self._write(
                    conn,
                    protocol.Response.failure(
                        request.id,
                        "not_permitted",
                        f"{request.cmd} is restricted to the runner's account",
                    ),
                )
                continue
            result = self._dispatch_request(request)
            if isinstance(result, Iterator):
                # A streaming command (``logs.tail --follow``) returns a lazy
                # iterator, which ``_dispatch_request`` has already handed back:
                # the serialized worker is free by now. The events are pulled
                # here, on the connection's own thread, so one client following
                # the log does not delay any other command.
                for event in result:
                    self._write(conn, event)
                return
            self._write(conn, result)

    def _read_line(self, reader: Any) -> bytes | None:
        raw = reader.readline(_READ_SIZE)
        if not raw:
            return None
        if protocol.is_oversize_line(raw):
            raise _LineTooLong
        return raw

    def _reject_line(self, conn: socket.socket) -> None:
        self._write(
            conn,
            protocol.Response.failure(
                None, "invalid_input", f"line exceeds the {protocol.MAX_LINE_BYTES} byte limit"
            ),
        )

    def _write(self, conn: socket.socket, payload: Any) -> None:
        if isinstance(payload, (protocol.Response, protocol.Hello)):
            payload = payload.to_obj()
        conn.sendall(protocol.encode_line(payload))

    # -- the serialized command queue -------------------------------------- #

    def _dispatch_request(self, request: protocol.Request) -> Any:
        """Run the command on the worker and bound its *response* by the deadline.

        Every command carries the deadline, not just mutating plugin verbs
        (``contracts/control-protocol.md``): a slow ordinary command would
        otherwise hold the single serialized worker until it finished and every
        other client would queue behind it. The timeout does not cancel the
        in-flight command, so it still occupies the worker in the background;
        what the bound buys is that a silent, indefinite hang becomes a fast,
        diagnosable ``timeout`` failure with a ``command_timeout`` warning.
        """
        future = self._queue.submit(self._run_dispatch, request)
        try:
            return future.result(timeout=self._command_timeout_s)
        except TimeoutError:
            if self._log is not None:
                self._log.warning(
                    "command_timeout",
                    cmd=request.cmd,
                    timeout_s=self._command_timeout_s,
                )
            return protocol.Response.failure(
                request.id,
                "timeout",
                f"{request.cmd} exceeded {self._command_timeout_s:g} s",
            )

    def _run_dispatch(self, request: protocol.Request) -> Any:
        try:
            return self._dispatch(request)
        except protocol.ProtocolError as exc:
            return exc.as_response(request.id)
        except Exception as exc:  # noqa: BLE001 - one bad command must not kill the server
            if self._log is not None:
                self._log.error("dispatch_failed", cmd=request.cmd, error=str(exc))
            return protocol.Response.failure(
                request.id, "internal", f"{request.cmd} failed"
            )

    def _plugins_rev_value(self) -> int:
        value = self._plugins_rev
        return int(value() if callable(value) else value)
