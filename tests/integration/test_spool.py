"""Integration test for the spool through the real CLI (T040).

``contracts/control-protocol.md`` defines the spool: when the runner is not
running, a *spoolable* verb waits on disk and is replayed on start, in
receive-time order, after the access policy and the file's owner are checked. A
non-owner account may not conjure the runtime directory, malformed or oversized
entries are rejected, and the directory is capped at 1,000 files.

Most cases drive ``python -m smart_panel.cli.main`` as a subprocess (the real
envelope and exit codes). The owner-uid cases need a **second account**, which a
non-root test cannot fabricate, so they are exercised in-process against the real
``ControlClient`` / ``ControlServer`` / ``SpoolIngest`` with the peer uid faked —
the mechanism under test (the ownership and peer-credential checks) is the real
one; only the kernel's report of "who is calling" is substituted.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from smart_panel.core import paths
from smart_panel.core.control import client, protocol, spool
from smart_panel.core.control.access import Policy
from smart_panel.core.control.server import ControlServer
from smart_panel.core.control.spool import SpoolIngest

REPO_ROOT = Path(__file__).resolve().parents[2]
CANVAS = (1920, 462)
OUR_UID = os.geteuid()


# -------------------------------------------------------------------- fixtures


class CliHost:
    """A CLI host pointed at a temp home, runtime and frames directory."""

    def __init__(self, base: Path, home: Path, runtime: Path) -> None:
        self.base = base
        self.home = home
        self.runtime = runtime
        self.frames = base / "frames"
        self.clock = base / "clock"
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["SMART_PANEL_HOME"] = str(home)
        env["SMART_PANEL_RUNTIME"] = str(runtime)
        env["SMART_PANEL_TESTING"] = "1"
        env.pop("SMART_PANEL_JSON", None)
        env.pop("SMART_PANEL_RUNNER_UID", None)
        self.env = env

    def cli(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "smart_panel.cli.main", *args],
            capture_output=True,
            text=True,
            env=env or self.env,
            cwd=str(REPO_ROOT),
            timeout=60,
        )

    def data(self, *args: str, env: dict[str, str] | None = None) -> dict:
        proc = self.cli(*args, env=env)
        return {"data": json.loads(proc.stdout), "proc": proc}

    def seed_config(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "display": {
                        "sink": "files",
                        "files_dir": str(self.frames),
                        "min_text_px": 24,
                    },
                    "layout": {
                        "regions": [
                            {
                                "name": "main",
                                "x": 0,
                                "y": 0,
                                "w": CANVAS[0],
                                "h": CANVAS[1],
                                "instance": "text",
                            }
                        ],
                        "overlays": [],
                    },
                    "plugins": {
                        "text": {"plugin": "text", "enabled": True, "settings": {"text": "s"}}
                    },
                }
            ),
            encoding="utf-8",
        )
        self.clock.write_text("0\n", encoding="utf-8")

    def spool_dir(self) -> Path:
        return self.runtime / "spool"

    def spool_files(self) -> list[Path]:
        return sorted(self.spool_dir().glob("*.json"))

    def start(self) -> subprocess.CompletedProcess[str]:
        return self.cli("--json", "--timeout", "15", "start")

    def stop(self) -> None:
        self.cli("--json", "stop")


@pytest.fixture
def host(tmp_path: Path) -> Iterator[CliHost]:
    base = Path(tempfile.mkdtemp(prefix="spool-", dir="/tmp"))
    cli_host = CliHost(base, tmp_path / "home", base / "rt")
    cli_host.seed_config()
    try:
        yield cli_host
    finally:
        cli_host.stop()
        shutil.rmtree(base, ignore_errors=True)


def _spoolable_push(host: CliHost, text: str) -> subprocess.CompletedProcess[str]:
    return host.cli("--json", "text", "set", "--text", text)


def _log_events(host: CliHost) -> list[dict]:
    """The runner's recent log entries (``logs.tail`` returns raw ring entries)."""
    body = host.data("--json", "logs", "--lines", "50")["data"]
    assert body["ok"] is True
    return body["data"]["events"]


def _text_sets(host: CliHost) -> list[str]:
    return [
        str(event["text"]) for event in _log_events(host) if event.get("event") == "text_set"
    ]


# ------------------------------------------------------------------- real CLI


def test_a_spoolable_push_is_saved_for_later(host: CliHost) -> None:
    host.spool_dir().mkdir(parents=True, exist_ok=True)

    proc = _spoolable_push(host, "later")
    assert proc.returncode == 0
    body = json.loads(proc.stdout)
    assert body["ok"] is True
    assert body["data"]["saved_for_later"] is True
    assert isinstance(body["data"]["spool_id"], str)

    files = host.spool_files()
    assert len(files) == 1
    entry = json.loads(files[0].read_text(encoding="utf-8"))
    assert entry["cmd"] == "plugin.call"
    assert entry["args"]["instance"] == "text"
    assert entry["args"]["verb"] == "set"
    assert entry["args"]["args"]["text"] == "later"


def test_a_command_needing_the_runner_is_exit_three(host: CliHost) -> None:
    host.spool_dir().mkdir(parents=True, exist_ok=True)
    proc = host.cli("--json", "status")
    assert proc.returncode == 3
    assert json.loads(proc.stdout)["error"]["code"] == "runner_not_running"


def test_the_spool_replays_in_receive_time_order_on_start(host: CliHost) -> None:
    host.spool_dir().mkdir(parents=True, exist_ok=True)
    for text in ("first", "second", "third"):
        proc = _spoolable_push(host, text)
        assert proc.returncode == 0
        time.sleep(0.01)
    assert len(host.spool_files()) == 3

    proc = host.start()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    try:
        assert _text_sets(host) == ["first", "second", "third"]
        assert host.spool_files() == []
        status = host.data("--json", "status")["data"]["data"]
        assert status["spool"]["pending"] == 0
    finally:
        host.stop()


def test_malformed_and_oversized_spool_files_are_rejected_and_deleted(host: CliHost) -> None:
    directory = host.spool_dir()
    directory.mkdir(parents=True, exist_ok=True)
    assert _spoolable_push(host, "ok").returncode == 0
    (directory / "0001-bad.json").write_text("{not json}", encoding="utf-8")
    (directory / "0002-big.json").write_text("x" * (spool.MAX_FILE_BYTES + 1), encoding="utf-8")
    blob = {
        "v": 1,
        "received_at": 2.0,
        "cmd": "plugin.call",
        "args": {
            "instance": "text",
            "verb": "set",
            "args": {"text": "y" * (spool.MAX_FILE_BYTES + 1)},
        },
    }
    (directory / "0003-valid-big.json").write_text(json.dumps(blob), encoding="utf-8")

    proc = host.start()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    try:
        # The good entry replayed; all three bad ones were rejected and removed.
        assert host.spool_files() == []
        assert _text_sets(host) == ["ok"]
        rejections = [
            event for event in _log_events(host) if event.get("event") == "spool_rejected"
        ]
        assert len(rejections) == 3
    finally:
        host.stop()


def test_the_spool_cap_returns_spool_full(host: CliHost) -> None:
    directory = host.spool_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(spool.MAX_FILES):
        (directory / f"{index:020d}-{'0' * 32}.json").write_text("{}", encoding="utf-8")

    proc = _spoolable_push(host, "full")
    assert proc.returncode == 1
    body = json.loads(proc.stdout)
    assert body["ok"] is False
    assert body["error"]["code"] == "spool_full"
    assert len(host.spool_files()) == spool.MAX_FILES


def test_the_owner_without_a_runtime_directory_spools_to_pending(host: CliHost) -> None:
    missing = host.base / "no-such-runtime"
    env = dict(host.env)
    env["SMART_PANEL_RUNTIME"] = str(missing)

    proc = host.cli("--json", "text", "set", "--text", "waiting", env=env)
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["data"]["saved_for_later"] is True
    # The CLI never creates the runtime directory ...
    assert not missing.exists()
    # ... the owner's push waits in <home>/spool-pending instead.
    pending = host.home / "spool-pending"
    assert len(list(pending.glob("*.json"))) == 1


def test_a_non_owner_without_a_runtime_directory_is_refused(host: CliHost) -> None:
    missing = host.base / "no-such-runtime"
    env = dict(host.env)
    env["SMART_PANEL_RUNTIME"] = str(missing)
    env["SMART_PANEL_RUNNER_UID"] = str(OUR_UID + 1_000_000)

    proc = host.cli("--json", "text", "set", "--text", "nope", env=env)
    assert proc.returncode == 3
    assert json.loads(proc.stdout)["error"]["code"] == "runner_not_running"
    assert not missing.exists()
    assert not (host.home / "spool-pending").exists()


def test_the_runner_refuses_a_runtime_directory_owned_by_another_uid(host: CliHost) -> None:
    foreign = _foreign_owned_directory()
    if foreign is None:  # pragma: no cover - only when the suite runs as root
        pytest.skip("no directory owned by another uid to stand in for the runtime")

    env = dict(host.env)
    env["SMART_PANEL_RUNTIME"] = str(foreign)
    proc = host.cli("--json", "run", env=env)
    assert proc.returncode == 5
    body = json.loads(proc.stdout)
    assert body["ok"] is False
    assert body["error"]["code"] == "not_permitted"
    assert str(foreign) in body["error"]["message"]


# ---------------------------------------------- in-process (second-uid) cases


def test_spool_files_owned_by_a_non_allowed_uid_are_rejected_and_deleted(host: CliHost) -> None:
    directory = host.spool_dir()
    directory.mkdir(parents=True, exist_ok=True)
    entry = spool.write_entry(
        directory,
        "plugin.call",
        {"instance": "text", "verb": "set", "args": {"text": "foreign"}},
        received_at=1.0,
    )

    replayed: list[str] = []
    ingest = SpoolIngest(
        lambda request, received_at: replayed.append(request.cmd),
        policy_provider=lambda: Policy(runner_uid=OUR_UID + 999, allowed_uids=frozenset()),
        spool_dir=directory,
        pending_dir=host.home / "no-such-pending",
    )
    result = ingest.ingest()

    assert replayed == []
    assert result.rejected == 1
    assert not entry.exists()


def test_the_client_refuses_a_socket_owned_by_a_foreign_account(
    host: CliHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.runtime.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(host.runtime))
    monkeypatch.delenv("SMART_PANEL_RUNNER_UID", raising=False)
    socket_path = host.runtime / "control.sock"
    socket_path.write_text("", encoding="utf-8")

    real_stat = Path.stat
    foreign = OUR_UID + 5150

    def fake_stat(self: Path, *args: object, **kwargs: object) -> object:
        if self == socket_path:
            return SimpleNamespace(st_uid=foreign, st_gid=0)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)

    with pytest.raises(client.ImpersonationRefused) as excinfo:
        client.ControlClient(timeout=1).connect()
    assert excinfo.value.code == "not_permitted"


def test_a_second_allowed_uid_reaches_the_same_socket_and_spool_path(
    host: CliHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    from smart_panel.core.control import peercred

    host.runtime.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(host.runtime))
    monkeypatch.setenv("SMART_PANEL_RUNNER_UID", str(OUR_UID))

    second = OUR_UID + 4242
    policy = Policy(runner_uid=OUR_UID, allowed_uids=frozenset({second}))
    seen: list[str] = []

    def dispatch(request: protocol.Request) -> protocol.Response:
        seen.append(request.cmd)
        return protocol.Response.success(request.id, {"cmd": request.cmd})

    server = ControlServer(
        dispatch,
        policy_provider=lambda: policy,
        socket_path=host.runtime / "control.sock",
    )
    server.start()
    try:
        # The kernel reports the *second* account as the peer; the policy allows it.
        monkeypatch.setattr(peercred, "get_peer", lambda conn: (second, second))
        monkeypatch.setattr(peercred, "get_peer_groups", lambda uid, gid: [second])

        with client.ControlClient(timeout=2) as control:
            response = control.call("runner.status")
        assert response.ok is True
        assert response.data == {"cmd": "runner.status"}
        assert seen == ["runner.status"]

        # Both accounts resolve the shared spool path: it is a path, not per-uid.
        assert paths.spool() == host.runtime / "spool"
    finally:
        server.stop()


# --------------------------------------------------------------------- helpers


def _foreign_owned_directory() -> Path | None:
    """A real directory owned by a uid other than ours (e.g. root's ``/tmp``)."""
    for candidate in (Path("/tmp"), Path("/"), Path("/usr"), Path("/etc")):
        try:
            info = candidate.stat()
        except OSError:  # pragma: no cover - defensive
            continue
        if candidate.is_dir() and info.st_uid != OUR_UID:
            return candidate
    return None
