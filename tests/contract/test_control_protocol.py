"""Contract test for the control protocol (T038, ``contracts/control-protocol.md``).

Covers the wire contract the CLI and the runner both depend on: the mandatory
``hello`` handshake with ``"v": 1``, ``protocol_mismatch`` on a major-version
mismatch followed by a close, request ids echoed in the response, the 1 MiB
line limit, and the closed set of error codes with the exit-code mapping from
``contracts/cli.md``.

The second half drives the real ``ControlServer``/``ControlClient`` pair over a
Unix socket, so the handshake, the authorization step and the serialized command
queue are exercised as the runner and CLI will use them.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from smart_panel.core.control import access, protocol
from smart_panel.core.control.access import Policy
from smart_panel.core.control.client import ControlClient, ControlClientError
from smart_panel.core.control.server import ControlServer

DOCUMENTED_ERROR_CODES = frozenset(
    {
        "invalid_input",
        "not_found",
        "not_permitted",
        "conflict",
        "panel_unavailable",
        "plugin_error",
        "spool_full",
        "timeout",
        "protocol_mismatch",
        "internal",
    }
)

# ``contracts/cli.md`` exit codes: the table plus the extra error codes listed
# under it. ``runner_not_running`` is an exit-code name the client raises
# itself, so it is not a member of the server's error-code set.
DOCUMENTED_EXIT_CODES = {
    "invalid_input": 2,
    "not_found": 2,
    "runner_not_running": 3,
    "panel_unavailable": 4,
    "not_permitted": 5,
    "conflict": 6,
    "plugin_error": 7,
    "spool_full": 1,
    "timeout": 1,
    "protocol_mismatch": 1,
    "internal": 1,
}


# --------------------------------------------------------------------------- #
# Wire contract, exercised without a socket
# --------------------------------------------------------------------------- #


class TestProtocolMessages:
    def test_hello_first_line_is_v1(self) -> None:
        hello = protocol.hello_request("smart-panel-cli/0.1.0")
        assert hello.to_obj() == {"hello": {"v": 1, "client": "smart-panel-cli/0.1.0"}}
        reply = protocol.hello_reply("0.1.0", 17)
        assert reply.to_obj() == {"hello": {"v": 1, "server": "0.1.0", "plugins_rev": 17}}

    def test_hello_major_version_mismatch_is_protocol_mismatch(self) -> None:
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.parse_hello({"hello": {"v": 2, "client": "smart-panel-cli/0.1.0"}})
        assert excinfo.value.code == "protocol_mismatch"
        assert protocol.parse_hello({"hello": {"v": 1}}).v == 1

    def test_a_request_instead_of_a_hello_is_protocol_mismatch(self) -> None:
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.parse_hello({"id": 1, "cmd": "runner.status", "args": {}})
        assert excinfo.value.code == "protocol_mismatch"

    def test_request_ids_are_echoed(self) -> None:
        assert protocol.Response.success(7, {"a": 1}).to_obj() == {
            "id": 7,
            "ok": True,
            "data": {"a": 1},
        }
        failure = protocol.Response.failure(7, "not_found", "no such thing").to_obj()
        assert failure == {
            "id": 7,
            "ok": False,
            "error": {"code": "not_found", "message": "no such thing"},
        }

    def test_error_codes_are_only_the_documented_set(self) -> None:
        assert protocol.ERROR_CODES == DOCUMENTED_ERROR_CODES
        with pytest.raises(ValueError):
            protocol.Error("made_up", "nope")
        with pytest.raises(ValueError):
            protocol.Response.failure(1, "made_up", "nope")
        # A setting is carried only when there is one.
        with_setting = protocol.Response.failure(2, "invalid_input", "bad", "display.fps").to_obj()
        assert with_setting["error"]["setting"] == "display.fps"

    def test_exit_code_mapping_matches_the_cli_contract(self) -> None:
        assert protocol.exit_code_for("runner_not_running") == 3
        for code, expected in DOCUMENTED_EXIT_CODES.items():
            assert protocol.exit_code_for(code) == expected, code
        # An unknown code is an internal error, never a silent success.
        assert protocol.exit_code_for("who_knows") == 1

    def test_request_and_response_round_trip_as_json_lines(self) -> None:
        line = protocol.encode_line(
            {"id": 7, "cmd": "config.set", "args": {"path": "display.fps", "value": 20}}
        )
        assert line.endswith(b"\n")
        obj = protocol.decode_line(line)
        request = protocol.parse_request(obj)
        assert request.id == 7
        assert request.cmd == "config.set"
        assert request.args == {"path": "display.fps", "value": 20}

    def test_line_over_one_mib_is_rejected(self) -> None:
        oversize = b"x" * (protocol.MAX_LINE_BYTES + 1) + b"\n"
        assert protocol.is_oversize_line(oversize) is True
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.decode_line(oversize)
        assert excinfo.value.code == "invalid_input"
        # A line at exactly the limit is still fine.
        assert protocol.is_oversize_line(b"x" * protocol.MAX_LINE_BYTES) is False

    def test_malformed_json_is_rejected(self) -> None:
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.decode_line(b"{not json}\n")
        assert excinfo.value.code == "invalid_input"
        with pytest.raises(protocol.ProtocolError):
            protocol.decode_line(b"[1, 2, 3]\n")

    def test_request_validation_rejects_bad_shapes(self) -> None:
        for bad in ({}, {"cmd": ""}, {"cmd": 1}, {"cmd": "x", "args": []}, {"cmd": "x", "id": 1.5}):
            with pytest.raises(protocol.ProtocolError) as excinfo:
                protocol.parse_request(bad)
            assert excinfo.value.code == "invalid_input"


# --------------------------------------------------------------------------- #
# The real server and client over a Unix socket
# --------------------------------------------------------------------------- #


class _RawPeer:
    """A hand-driven client, so the test can send bytes the real client would not."""

    def __init__(self, path: Path, version: int = 1) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(str(path))
        self.reader = self.sock.makefile("rb")
        self.send({"hello": {"v": version, "client": "test/1"}})

    def send(self, obj: object) -> None:
        self.sock.sendall(protocol.encode_line(obj))  # type: ignore[arg-type]

    def send_raw(self, raw: bytes) -> None:
        self.sock.sendall(raw)

    def recv(self) -> dict | None:
        line = self.reader.readline()
        return json.loads(line) if line else None

    def close(self) -> None:
        self.reader.close()
        self.sock.close()


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A short runtime directory: AF_UNIX paths are capped near 108 bytes.

    The default ``/tmp/smart-panel`` is short; pytest's own ``tmp_path`` is not
    always (a long ``TMPDIR`` pushes ``<runtime>/control.sock`` past the kernel
    limit), so the socket fixture takes a short temp dir of its own.
    """
    base = _short_temp_dir()
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(base / "runtime"))
    monkeypatch.setenv("SMART_PANEL_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SMART_PANEL_RUNNER_UID", raising=False)
    try:
        yield base / "runtime"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _short_temp_dir() -> Path:
    for candidate in ("/tmp", tempfile.gettempdir()):
        if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            return Path(tempfile.mkdtemp(prefix="sp-", dir=candidate))
    raise RuntimeError("no writable temporary directory for the socket tests")


def _serve(dispatch, runtime: Path, **kwargs) -> ControlServer:
    policy = kwargs.pop("policy", Policy(runner_uid=os.geteuid()))
    server = ControlServer(dispatch, policy_provider=lambda: policy, **kwargs)
    server.start()
    return server


class TestControlServer:
    def test_handshake_then_request_ids_are_echoed(self, runtime: Path) -> None:
        server = _serve(_echo_dispatch, runtime)
        try:
            peer = _RawPeer(server.socket_path)
            hello = peer.recv()
            assert hello is not None and hello["hello"]["v"] == 1
            peer.send({"id": 7, "cmd": "runner.status", "args": {}})
            first = peer.recv()
            peer.send({"id": 8, "cmd": "runner.status", "args": {}})
            second = peer.recv()
            assert first == {"id": 7, "ok": True, "data": {"echo": "runner.status"}}
            assert second is not None and second["id"] == 8
            peer.close()
        finally:
            server.stop()

    def test_the_first_line_must_be_a_hello(self, runtime: Path) -> None:
        server = _serve(_echo_dispatch, runtime)
        try:
            peer = _RawPeer.__new__(_RawPeer)
            peer.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            peer.sock.settimeout(5)
            peer.sock.connect(str(server.socket_path))
            peer.reader = peer.sock.makefile("rb")
            peer.send({"id": 1, "cmd": "runner.status", "args": {}})
            reply = peer.recv()
            assert reply is not None and reply["error"]["code"] == "protocol_mismatch"
            # The server closes after a bad handshake.
            assert peer.reader.readline() == b""
            peer.close()
        finally:
            server.stop()

    def test_major_version_mismatch_returns_protocol_mismatch_and_closes(
        self, runtime: Path
    ) -> None:
        server = _serve(_echo_dispatch, runtime)
        try:
            peer = _RawPeer(server.socket_path, version=2)
            reply = peer.recv()
            assert reply is not None and reply["error"]["code"] == "protocol_mismatch"
            assert peer.reader.readline() == b""
            peer.close()
        finally:
            server.stop()

    def test_oversize_line_is_rejected_and_closed(self, runtime: Path) -> None:
        server = _serve(_echo_dispatch, runtime)
        try:
            peer = _RawPeer(server.socket_path)
            assert peer.recv() is not None
            peer.send_raw(b"x" * (protocol.MAX_LINE_BYTES + 1) + b"\n")
            reply = peer.recv()
            assert reply is not None and reply["error"]["code"] == "invalid_input"
            assert peer.reader.readline() == b""
            peer.close()
        finally:
            server.stop()

    def test_a_denied_peer_gets_one_not_permitted_and_is_closed(self, runtime: Path) -> None:
        server = _serve(_echo_dispatch, runtime, policy=Policy(runner_uid=0xDEAD))
        try:
            peer = _RawPeer(server.socket_path)
            reply = peer.recv()
            assert reply is not None and reply["error"]["code"] == "not_permitted"
            assert peer.reader.readline() == b""
            peer.close()
        finally:
            server.stop()

    def test_one_worker_runs_commands_from_two_connections(self, runtime: Path) -> None:
        threads: list[int] = []
        gate = threading.Barrier(2)

        def dispatch(request: protocol.Request) -> protocol.Response:
            threads.append(threading.get_ident())
            time.sleep(0.05)
            return protocol.Response.success(request.id, {})

        server = _serve(dispatch, runtime)
        try:
            results: list[dict | None] = []

            def run() -> None:
                peer = _RawPeer(server.socket_path)
                peer.recv()
                peer.send({"id": 1, "cmd": "runner.status", "args": {}})
                gate.wait()
                results.append(peer.recv())
                peer.close()

            workers = [threading.Thread(target=run) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=5)

            assert len(results) == 2
            assert all(result is not None and result["ok"] for result in results)
            # Both commands ran on the one serialized dispatcher thread.
            assert len(set(threads)) == 1
        finally:
            server.stop()

    def test_a_mutating_verb_over_its_deadline_returns_timeout(self, runtime: Path) -> None:
        def dispatch(request: protocol.Request) -> protocol.Response:
            time.sleep(0.4)
            return protocol.Response.success(request.id, {})

        server = _serve(dispatch, runtime, command_timeout_s=0.05)
        try:
            peer = _RawPeer(server.socket_path)
            peer.recv()
            peer.send({"id": 3, "cmd": "plugin.call", "args": {}})
            reply = peer.recv()
            assert reply is not None and reply["error"]["code"] == "timeout"
            assert reply["id"] == 3
            peer.close()
        finally:
            server.stop()

    def test_an_unexpected_dispatch_failure_is_an_internal_error(self, runtime: Path) -> None:
        def dispatch(request: protocol.Request) -> protocol.Response:
            raise RuntimeError("boom")

        server = _serve(dispatch, runtime)
        try:
            peer = _RawPeer(server.socket_path)
            peer.recv()
            peer.send({"id": 4, "cmd": "runner.status", "args": {}})
            reply = peer.recv()
            assert reply is not None and reply["error"]["code"] == "internal"
            peer.close()
        finally:
            server.stop()

    def test_a_following_command_streams_until_the_client_closes(self, runtime: Path) -> None:
        def dispatch(request: protocol.Request) -> Iterator[dict]:
            for index in range(3):
                yield {"event": "log", "n": index}

        server = _serve(dispatch, runtime)
        try:
            peer = _RawPeer(server.socket_path)
            peer.recv()
            peer.send({"id": 5, "cmd": "logs.tail", "args": {"follow": True}})
            events = [peer.recv() for _ in range(3)]
            assert [event["n"] for event in events if event] == [0, 1, 2]
            peer.close()
        finally:
            server.stop()

    def test_the_client_handshakes_and_calls(self, runtime: Path) -> None:
        server = _serve(_echo_dispatch, runtime)
        try:
            with ControlClient(timeout=5) as client:
                response = client.call("runner.status")
            assert response.ok is True
            assert response.id == 1
            assert response.data == {"echo": "runner.status"}
        finally:
            server.stop()

    def test_the_client_refuses_a_socket_owned_by_a_foreign_uid(
        self, runtime: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from smart_panel.core.control import client as client_module

        server = _serve(_echo_dispatch, runtime)
        try:
            monkeypatch.setattr(client_module.os, "geteuid", lambda: 0x5150)
            with pytest.raises(ControlClientError) as excinfo:
                with ControlClient(timeout=1) as client:
                    client.call("runner.status")
            assert excinfo.value.code == "not_permitted"
        finally:
            server.stop()


def _echo_dispatch(request: protocol.Request) -> protocol.Response:
    return protocol.Response.success(request.id, {"echo": request.cmd})


def test_access_module_is_used_by_the_server_contract() -> None:
    # The authorization decision the server applies is the pure one.
    policy = Policy(runner_uid=1, allowed_uids=frozenset({2}))
    assert access.authorize(2, [2], policy, "runner.status") is None
    assert access.authorize(3, [3], policy, "runner.status") == "not_permitted"
