"""Integration test for the CLI ``logs --follow`` streaming path (T059).

``logs --follow`` is the only CLI path that consumes a *stream* rather than one
response: the server answers ``logs.tail {follow: true}`` with a sequence of
``{"event": "log", ...}`` lines until it closes (``contracts/control-protocol.md``),
the client's ``ControlClient.stream()`` yields each decoded line, ``misc._follow``
writes it through ``Output.emit_line`` and returns the ``STREAMED`` sentinel so the
dispatcher adds no envelope, and the process exits when the server closes.

Three exits are pinned here (``contracts/cli.md``, "Streaming commands"): a
genuine server close exits 0; ``--timeout`` bounds only the connect and
handshake, so an *idle* follow stays parked (it does not exit while the log is
quiet); and a follow with **no runner** exits 3 ``runner_not_running``, like the
non-follow ``logs`` path. A socket that *accepts* the connect and then answers
nothing is the other "no answer" shape — a runner that is there — and exits 1
``timeout`` (the same split ``ControlClient.call`` makes).

This drives the real CLI as a subprocess against a real in-process
``ControlServer`` with a scripted dispatch (the harness style of
``tests/integration/test_spool.py``), so the assertion is on the bytes on stdout
and on the exit, not on a helper called directly.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from smart_panel.core.control import protocol
from smart_panel.core.control.access import Policy
from smart_panel.core.control.server import ControlServer

REPO_ROOT = Path(__file__).resolve().parents[2]

# Three events in the contract's ``{"event": "log", ...}`` shape, with the varied
# payload keys a real ring entry carries.
EVENTS: list[dict[str, object]] = [
    {"event": "log", "log_event": "runner_started", "ts": 1.0, "level": "info", "pid": 42},
    {"event": "log", "log_event": "text_set", "ts": 2.0, "level": "info", "text": "hello"},
    {"event": "log", "log_event": "frame_sent", "ts": 3.0, "level": "debug", "frames": 7},
]


class FollowHost:
    """A CLI host pointed at a short runtime dir and a temp home."""

    def __init__(self, base: Path, home: Path, runtime: Path) -> None:
        self.base = base
        self.home = home
        self.runtime = runtime
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["SMART_PANEL_HOME"] = str(home)
        env["SMART_PANEL_RUNTIME"] = str(runtime)
        env["SMART_PANEL_TESTING"] = "1"
        env.pop("SMART_PANEL_JSON", None)
        env.pop("SMART_PANEL_RUNNER_UID", None)
        self.env = env

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run ``smart-panel <args>`` as a subprocess against this host."""
        return subprocess.run(
            [sys.executable, "-m", "smart_panel.cli.main", *args],
            capture_output=True,
            text=True,
            env=self.env,
            cwd=str(REPO_ROOT),
            timeout=30,
        )


@pytest.fixture
def follow_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FollowHost]:
    """A host whose runtime dir is short enough for the AF_UNIX path cap.

    ``SMART_PANEL_RUNTIME`` is set in-process too, so the server's
    ``paths.ensure_runtime()`` creates the host's own runtime directory instead
    of the shared ``/tmp/smart-panel`` default.
    """
    base = Path(tempfile.mkdtemp(prefix="follow-", dir="/tmp"))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SMART_PANEL_HOME", str(home))
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(base / "rt"))
    monkeypatch.delenv("SMART_PANEL_RUNNER_UID", raising=False)
    host = FollowHost(base, home, base / "rt")
    try:
        yield host
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _serve(dispatch, host: FollowHost) -> ControlServer:
    """Start a real ``ControlServer`` on the host's control socket."""
    server = ControlServer(
        dispatch,
        policy_provider=lambda: Policy(runner_uid=os.geteuid()),
        socket_path=host.runtime / "control.sock",
    )
    server.start()
    return server


def _events(items: list[dict[str, object]]) -> Iterator[dict[str, object]]:
    """Yield ``items``, then hold the socket a moment before the server closes.

    The trailing pause proves the client is genuinely parked in a read when the
    server closes -- the exit is provoked by the close, not by the dispatch
    finishing before the client asked to follow.
    """
    yield from items
    time.sleep(0.2)


def _events_with_idle_gap(
    items: list[dict[str, object]], gap_s: float
) -> Iterator[dict[str, object]]:
    """Yield the first two events, stay silent for ``gap_s``, then the rest.

    The silent gap is longer than the CLI's ``--timeout`` in the test that uses
    this, so a follow that stays parked through it proves the timeout bounds the
    connect/handshake only and is not a liveness limit on the open stream.
    """
    yield from items[:2]
    time.sleep(gap_s)
    yield from items[2:]
    time.sleep(0.2)


def test_follow_writes_one_json_line_per_event_and_exits_when_the_server_closes(
    follow_host: FollowHost,
) -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def dispatch(request: protocol.Request) -> Iterator[dict[str, object]]:
        requests.append((request.cmd, dict(request.args)))
        return _events(EVENTS)

    server = _serve(dispatch, follow_host)
    try:
        proc = follow_host.cli("--json", "logs", "--follow")
    finally:
        server.stop()

    # The CLI sent the streaming request, with the follow flag the path needs.
    assert requests == [("logs.tail", {"lines": 50, "follow": True})]

    # The client exited on its own when the server closed the connection; a hang
    # would instead raise ``subprocess.TimeoutExpired`` above.
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # Each streamed event is exactly one JSON line on stdout -- no envelope, no
    # extra line (the ``STREAMED`` sentinel suppresses the dispatcher's emit).
    lines = proc.stdout.splitlines()
    assert len(lines) == len(EVENTS)
    assert [json.loads(line) for line in lines] == EVENTS


def test_follow_stays_parked_through_an_idle_gap_longer_than_the_timeout(
    follow_host: FollowHost,
) -> None:
    """An idle stream must not exit on ``--timeout``; it exits 0 on the close.

    The server holds the stream silent for longer than the CLI's ``--timeout``
    (0.5 s) between the second and third event, then closes it. If the timeout
    were a liveness limit on the open stream the client would exit 1 mid-gap and
    never write the third event; a genuine close still exits 0 with every event.
    """

    def dispatch(request: protocol.Request) -> Iterator[dict[str, object]]:
        return _events_with_idle_gap(EVENTS, gap_s=1.5)

    server = _serve(dispatch, follow_host)
    try:
        proc = follow_host.cli("--json", "--timeout", "0.5", "logs", "--follow")
    finally:
        server.stop()

    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = proc.stdout.splitlines()
    assert [json.loads(line) for line in lines] == EVENTS


def test_follow_with_no_runner_exits_runner_not_running(follow_host: FollowHost) -> None:
    """A follow against no runner exits 3, matching the non-follow ``logs`` path.

    No server is started, so the connect fails with ``ENOENT`` before the
    handshake; the streaming path must map it to ``runner_not_running`` (exit 3)
    exactly as ``ControlClient.call`` already does for one-shot commands.
    """
    proc = follow_host.cli("--json", "logs", "--follow")

    assert proc.returncode == 3, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout.splitlines()[0])
    assert payload["ok"] is False
    assert payload["error"]["code"] == "runner_not_running"


def test_follow_of_a_silent_socket_is_not_reported_as_no_runner(
    follow_host: FollowHost,
) -> None:
    """A follow whose hello is never answered exits 1 ``timeout``, not 3.

    A socket that accepts the connection and then says nothing is a runner that
    *is* there — starved, or mid-teardown, still holding the single-instance
    lock. ``--timeout`` bounds the connect and hello handshake, so this is the
    follow's one bounded silence; calling it ``runner_not_running`` (exit 3)
    would claim there is no runner while one is live. It must carry the
    documented ``timeout`` code (exit 1), like ``ControlClient.call``.
    """
    follow_host.runtime.mkdir(parents=True, exist_ok=True)
    os.chmod(follow_host.runtime, 0o755)
    silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    silent.bind(str(follow_host.runtime / "control.sock"))
    silent.listen(1)  # accepts the connect into the backlog, never answers
    try:
        proc = follow_host.cli("--json", "--timeout", "1", "logs", "--follow")
    finally:
        silent.close()

    assert proc.returncode == 1, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout.splitlines()[0])
    assert payload["ok"] is False
    assert payload["error"]["code"] == "timeout"
    assert "did not answer" in payload["error"]["message"]
