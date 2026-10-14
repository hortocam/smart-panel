"""TEMPORARY diagnostic probe for the macOS LOCAL_PEERCRED handshake close.

Card ``t_e2514bdf`` (follow-up to PR #42, run 38486563021): on macos-latest the
runner closes the control handshake with no reply for a real second uid, so the
client reports ``runner_not_running`` (exit 3) instead of the policy's answer.

``runner start`` spawns the runner with ``stderr=DEVNULL`` (``cli/commands/
runner.py::_start_detached``), so an unhandled exception in a control-connection
thread is invisible in CI and only shows up as a silent close. This module runs
the real :class:`ControlServer` in the pytest process, patches ``threading.
excepthook`` to capture whatever that thread throws, and drives it from a real
second uid (``sudo -n -u nobody``) with nothing but the stdlib, so the server-side
traceback is printed by the failing assertion.

This module is deleted before the fix is submitted; it is a probe, not a test of
the product.
"""

from __future__ import annotations

import os
import pwd
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import pytest

from smart_panel.core.control import peercred, protocol
from smart_panel.core.control.access import Policy
from smart_panel.core.control.server import ControlServer

_NOBODY_UID = 4294967294  # Darwin's nobody, unsigned
_XUCRED_SIZE = 12 + 16 * 4

_CLIENT = textwrap.dedent(
    """
    import socket
    import sys

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(sys.argv[1])
    s.sendall(b'{"hello": {"v": 1, "client": "probe/0.1.0"}}\\n')
    f = s.makefile("rb")
    print(repr(f.readline(1 << 20)))
    """
)


def _facts(report: list[str]) -> None:
    report.append(f"platform={sys.platform} python={sys.version.split()[0]}")
    report.append(f"os.getuid()={os.getuid()} os.geteuid()={os.geteuid()}")
    for name in ("nobody", "daemon"):
        try:
            entry = pwd.getpwnam(name)
            report.append(
                f"pwd.getpwnam({name!r}) -> uid={entry.pw_uid} gid={entry.pw_gid}"
            )
        except Exception as exc:  # noqa: BLE001
            report.append(f"pwd.getpwnam({name!r}) -> {type(exc).__name__}: {exc}")
    for uid in (_NOBODY_UID, -2):
        try:
            entry = pwd.getpwuid(uid)
            report.append(
                f"pwd.getpwuid({uid}) -> name={entry.pw_name!r} uid={entry.pw_uid} "
                f"gid={entry.pw_gid}"
            )
        except Exception as exc:  # noqa: BLE001
            report.append(f"pwd.getpwuid({uid}) -> {type(exc).__name__}: {exc}")
    for gid in (_NOBODY_UID, -2, 0):
        try:
            groups = os.getgrouplist("nobody", gid)
            report.append(f"os.getgrouplist('nobody', {gid}) -> {groups}")
        except Exception as exc:  # noqa: BLE001
            report.append(
                f"os.getgrouplist('nobody', {gid}) -> {type(exc).__name__}: {exc}"
            )
    report.append(
        f"socket.SOL_LOCAL={getattr(socket, 'SOL_LOCAL', None)} "
        f"socket.LOCAL_PEERCRED={getattr(socket, 'LOCAL_PEERCRED', None)} "
        f"peercred module constants=({peercred._SOL_LOCAL}, {peercred._LOCAL_PEERCRED}, "
        f"{peercred._XUCRED_SIZE})"
    )


def _run_clients(report: list[str], sock_path: Path) -> None:
    interpreter = "/usr/bin/python3"
    if not Path(interpreter).exists():  # pragma: no cover - macOS runner fallback
        interpreter = shutil.which("python3") or sys.executable
    report.append(f"probe client interpreter={interpreter}")
    for account in ("nobody", "daemon"):
        proc = subprocess.run(
            ["sudo", "-n", "-u", account, interpreter, "-c", _CLIENT, str(sock_path)],
            capture_output=True,
            text=True,
            timeout=90,
        )
        report.append(
            f"client {account}: rc={proc.returncode} stdout={proc.stdout.strip()!r} "
            f"stderr={proc.stderr.strip()[:300]!r}"
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only diagnostic probe")
def test_probe_peercred_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    report: list[str] = []
    _facts(report)

    root = Path(tempfile.mkdtemp(prefix="peercred-probe-"))
    os.chmod(root, 0o755)
    runtime = root / "runtime"
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(runtime))
    sock_path = runtime / "control.sock"

    caught: list[str] = []
    previous_hook = threading.excepthook

    def hook(args: Any) -> None:
        caught.append(
            "".join(
                traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
            )
        )

    original_get_peer = peercred.get_peer
    original_get_groups = peercred.get_peer_groups

    def traced_get_peer(conn: socket.socket) -> tuple[int, int]:
        try:
            raw = conn.getsockopt(
                getattr(socket, "SOL_LOCAL", 0), getattr(socket, "LOCAL_PEERCRED", 1),
                _XUCRED_SIZE,
            )
            report.append(f"raw xucred len={len(raw)} hex={raw.hex()}")
            report.append(
                f"parsed version/uid/ngroups={struct.unpack_from('IIH', raw, 0)} "
                f"gid0={struct.unpack_from('I', raw, 12)[0]}"
            )
        except Exception as exc:  # noqa: BLE001
            report.append(f"raw getsockopt -> {type(exc).__name__}: {exc}")
        try:
            uid, gid = original_get_peer(conn)
        except Exception as exc:  # noqa: BLE001
            report.append(f"peercred.get_peer -> {type(exc).__name__}: {exc}")
            raise
        report.append(f"peercred.get_peer -> uid={uid} gid={gid}")
        return uid, gid

    def traced_get_groups(uid: int, gid: int) -> list[int]:
        try:
            groups = original_get_groups(uid, gid)
        except Exception as exc:  # noqa: BLE001
            report.append(
                f"peercred.get_peer_groups({uid}, {gid}) -> {type(exc).__name__}: {exc}"
            )
            raise
        report.append(f"peercred.get_peer_groups({uid}, {gid}) -> {groups}")
        return groups

    monkeypatch.setattr(peercred, "get_peer", traced_get_peer)
    monkeypatch.setattr(peercred, "get_peer_groups", traced_get_groups)
    threading.excepthook = hook

    policy = Policy(runner_uid=os.geteuid(), allowed_uids=frozenset({_NOBODY_UID}))

    def dispatch(request: protocol.Request) -> protocol.Response:
        return protocol.Response.success(request.id, {"cmd": request.cmd})

    server = ControlServer(
        dispatch,
        policy_provider=lambda: policy,
        socket_path=sock_path,
    )
    try:
        server.start()
        time.sleep(0.2)
        _run_clients(report, sock_path)
        time.sleep(0.5)
    finally:
        threading.excepthook = previous_hook
        server.stop()
        shutil.rmtree(root, ignore_errors=True)

    report.append(
        "server-thread exceptions: none caught" if not caught else "server-thread exceptions:"
    )
    report.extend(caught)
    print("\n".join(report))
    raise AssertionError("peercred probe report printed above (temporary diagnostic)")
