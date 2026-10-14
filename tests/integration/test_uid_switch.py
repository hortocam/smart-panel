"""Integration test for the cross-account (uid-switch) control path (T134, FR-051).

``quickstart.md`` Scenario H promises "the integration test that starts the
runner under one uid and connects as another (skipped when unprivileged)". T040
(``tests/integration/test_spool.py``) covers the same access-policy checks
**in-process**, with the kernel's report of "who is calling" faked, so the
cross-account path the quickstart describes had never actually run.

This module puts real accounts on both sides of the socket:

* the runner is started by **this** account (the owner) through the real CLI;
* the client is the real ``python -m smart_panel.cli.main`` executed as another
  account (``nobody``, then ``daemon``) with ``sudo -n -u``, so the socket's peer
  credentials are the kernel's, not a monkeypatched stand-in.

The second account cannot import the checkout or the pre-built venv (``$HOME`` is
0750 and the venv 0700), so the fixture stages a world-readable copy of the
interpreter, its site-packages and the two source packages under ``/tmp``. That
copy is the only way to give a distinct uid the same code; it is skipped, never
faked, when the host cannot provide it (see :func:`stage`).

The real cross-uid run was Linux-only until the macOS ``LOCAL_PEERCRED`` branch was
fixed (card ``t_e2514bdf``). The macos-latest job of PR #42 (run 38486563021) showed
the server closing the handshake with no reply for a second uid, because Darwin's
``os.getgrouplist`` takes a C ``int`` and ``nobody``'s group id (4294967294) does not
fit: the ``OverflowError`` escaped the peer-credential lookup and killed the
connection thread before the policy could answer. Both platforms now run these same
assertions against a kernel-authenticated second uid; a host that cannot provide one
still skips, and never fakes.

Every refusal is proven twice: once through the exit code of the real process,
and once against :func:`smart_panel.core.control.access.is_allowed`, the pure
policy function ``tests/unit/test_access.py`` already covers. The accepted case
is a negative control first — the same command from the same account is refused
*before* ``access allow-user`` and accepted *after*, so a green cannot come from
a permissive default.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from smart_panel.core.control import access, protocol
from smart_panel.core.control.access import Policy

REPO_ROOT = Path(__file__).resolve().parents[2]
CANVAS = (1920, 462)

OWNER_ACCOUNT = pwd.getpwuid(os.geteuid()).pw_name
OWNER_UID = os.geteuid()
ALLOWED_ACCOUNT = "nobody"  # the non-owner account the owner adds to the allow-list
REFUSED_ACCOUNT = "daemon"  # a third account that is never allowed

_UNSET = object()


# ----------------------------------------------------------------------- stage


def _account(name: str) -> pwd.struct_passwd | None:
    """The passwd entry for ``name``, or ``None`` when the host has no such account."""
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return None


class Stage:
    """A world-readable copy of the interpreter, the dependencies and the source."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.site = root / "site"
        self.src = root / "src"
        self.python = _interpreter(root / "py")

    def env(self, home: Path, runtime: Path) -> list[str]:
        """The environment another account needs to run this checkout's CLI."""
        return [
            f"PYTHONPATH={self.site}:{self.src}",
            f"SMART_PANEL_HOME={home}",
            f"HOME={home}",
            f"SMART_PANEL_RUNTIME={runtime}",
            "SMART_PANEL_TESTING=1",
        ]


def _interpreter(base: Path) -> Path:
    version = f"python{sys.version_info[0]}.{sys.version_info[1]}"
    for name in (version, f"python{sys.version_info[0]}", "python"):
        candidate = base / "bin" / name
        if candidate.exists():
            return candidate
    raise OSError(f"no interpreter under {base / 'bin'}")


def _make_world_readable(root: Path) -> None:
    """Add o+rx to every directory and o+r (plus o+x where owned x) to every file."""
    os.chmod(root, 0o755)
    for dirpath, _dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o755)
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            mode = path.stat().st_mode & 0o777
            path.chmod(mode | 0o444 | (0o111 if mode & 0o100 else 0))


def _build_stage(root: Path) -> Stage:
    """Copy the interpreter, the venv's site-packages and the source into ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o755)
    # python.org 3.13 on macos-latest ships Tk/Tcl framework symlinks whose targets
    # are absent from the image; without ignore_dangling_symlinks the whole stage
    # aborts on them and every cross-account test skips (card t_e2514bdf).
    shutil.copytree(
        sys.base_prefix,
        root / "py",
        symlinks=False,
        dirs_exist_ok=True,
        ignore_dangling_symlinks=True,
    )
    shutil.copytree(
        Path(sysconfig.get_paths()["purelib"]), root / "site", symlinks=False, dirs_exist_ok=True
    )
    (root / "src").mkdir(exist_ok=True)
    shutil.copytree(REPO_ROOT / "smart_panel", root / "src" / "smart_panel", dirs_exist_ok=True)
    shutil.copytree(REPO_ROOT / "panel_driver", root / "src" / "panel_driver", dirs_exist_ok=True)
    _make_world_readable(root)
    return Stage(root)


def _install_dir(path: Path, account: pwd.struct_passwd) -> None:
    """Create ``path`` owned by ``account`` (mode 0700), as root through sudo."""
    subprocess.run(
        [
            "sudo",
            "-n",
            "install",
            "-d",
            "-o",
            str(account.pw_uid),
            "-g",
            str(account.pw_gid),
            "-m",
            "0700",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


@pytest.fixture(scope="session")
def stage() -> Iterator[Stage]:
    """The staged, world-readable environment, or a skip when the host cannot run one.

    ``sudo -n`` must work without a password (the owner's account is expected to
    have it; GitHub's Linux runners do), and the staged tree must actually run the
    CLI as the other account. Anything short of that is a skip with the reason
    attached, never a green that silently did nothing.

    The kernel authenticates the second uid through ``SO_PEERCRED`` on Linux and
    ``LOCAL_PEERCRED`` on macOS, and both are exercised here: the macOS branch was
    unverified until card ``t_e2514bdf`` fixed the group lookup it tripped over.
    """
    if shutil.which("sudo") is None:  # pragma: no cover - exercised on CI images without sudo
        pytest.skip("no sudo on this host: cannot run a client as a second uid")
    probe = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=15)
    if probe.returncode != 0:  # pragma: no cover - the skip path, demonstrated separately
        pytest.skip(
            f"{OWNER_ACCOUNT} has no passwordless sudo: cannot exercise a real second uid"
        )
    accounts = {}
    for name in (ALLOWED_ACCOUNT, REFUSED_ACCOUNT):
        entry = _account(name)
        if entry is None:  # pragma: no cover - defensive
            pytest.skip(f"the host has no {name!r} account to stand in for a second caller")
        accounts[name] = entry

    root = Path(tempfile.mkdtemp(prefix="uidswitch-", dir="/tmp"))
    try:
        staged = _build_stage(root)
    except OSError as exc:  # pragma: no cover - platform without a copyable interpreter
        _drop(root)
        pytest.skip(f"cannot stage a world-readable interpreter for another uid: {exc}")

    smoke_home = root / "smoke-home"
    _install_dir(smoke_home, accounts[ALLOWED_ACCOUNT])
    smoke = subprocess.run(
        [
            "sudo",
            "-n",
            "-u",
            ALLOWED_ACCOUNT,
            "env",
            *staged.env(smoke_home, root / "smoke-runtime"),
            str(staged.python),
            "-m",
            "smart_panel.cli.main",
            "version",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if smoke.returncode != 0:  # pragma: no cover - a host that cannot run the staged copy
        _drop(root)
        pytest.skip(
            f"the staged environment did not run as {ALLOWED_ACCOUNT}: "
            f"{(smoke.stderr or smoke.stdout).strip()[:200]}"
        )
    try:
        yield staged
    finally:
        _drop(root)


def _drop(root: Path) -> None:
    """Remove a staging tree, taking ownership back from the accounts that wrote in it."""
    subprocess.run(
        ["sudo", "-n", "chown", "-R", str(OWNER_UID), str(root)], capture_output=True, timeout=120
    )
    shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------------------------ host


class SwitchHost:
    """The owner's CLI host, plus a way to run the same CLI as another account."""

    def __init__(self, base: Path, staged: Stage) -> None:
        self.base = base
        self.stage = staged
        self.runtime = base / "runtime"
        self.frames = base / "frames"
        self.clock = base / "clock"
        self.home = base / "owner-home"
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["SMART_PANEL_HOME"] = str(self.home)
        env["SMART_PANEL_RUNTIME"] = str(self.runtime)
        env["SMART_PANEL_TESTING"] = "1"
        env.pop("SMART_PANEL_JSON", None)
        env.pop("SMART_PANEL_RUNNER_UID", None)
        self.env = env
        self._client_homes: dict[str, Path] = {}

    # -- the owner's account -------------------------------------------------- #

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

    def owner_json(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.cli("--json", *args)

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
                    "access": {"allowed_users": [], "allowed_group": None},
                }
            ),
            encoding="utf-8",
        )
        self.clock.write_text("0\n", encoding="utf-8")

    def start(self) -> subprocess.CompletedProcess[str]:
        return self.cli("--json", "--timeout", "15", "start")

    def stop(self) -> None:
        try:
            self.cli("--json", "--timeout", "10", "stop")
        except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - teardown must not fail
            pass

    # -- a second account ----------------------------------------------------- #

    def client_home(self, account: str) -> Path:
        if account not in self._client_homes:
            path = self.base / f"client-{account}"
            entry = _account(account)
            assert entry is not None
            _install_dir(path, entry)
            self._client_homes[account] = path
        return self._client_homes[account]

    def as_account(
        self,
        account: str,
        *args: str,
        runner_uid: Any = _UNSET,
        runtime: Path | None = None,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess[str]:
        """Run the real CLI as ``account`` against this host, with a real peer uid.

        ``runner_uid`` is what the account exports as ``$SMART_PANEL_RUNNER_UID``:
        omitted means the variable stays unset, which is the impersonation-guard
        case; pass :data:`OWNER_UID` for an allowed non-owner account.
        """
        env = self.stage.env(self.client_home(account), runtime or self.runtime)
        if runner_uid is not _UNSET:
            env.append(f"SMART_PANEL_RUNNER_UID={runner_uid}")
        return subprocess.run(
            [
                "sudo",
                "-n",
                "-u",
                account,
                "env",
                *env,
                str(self.stage.python),
                "-m",
                "smart_panel.cli.main",
                "--json",
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def client_listing(self, account: str) -> list[str]:
        """The entries of ``account``'s home, listed as that account (its mode is 0700)."""
        proc = subprocess.run(
            ["sudo", "-n", "-u", account, "ls", "-1", str(self.client_home(account))],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.stdout.split()


@pytest.fixture
def host(stage: Stage) -> Iterator[SwitchHost]:
    base = Path(tempfile.mkdtemp(prefix="uidswitch-rt-", dir="/tmp"))
    os.chmod(base, 0o755)  # the runner's runtime and socket must be reachable by the other uid
    switch_host = SwitchHost(base, stage)
    switch_host.seed_config()
    try:
        yield switch_host
    finally:
        switch_host.stop()
        switch_host.env["SMART_PANEL_HOME"] = str(switch_host.home)
        _drop(base)


# --------------------------------------------------------------------- helpers


def _envelope(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(proc.stdout)


def _policy_with(*names: str) -> Policy:
    return Policy.from_config({"allowed_users": list(names)}, runner_uid=OWNER_UID)


def _wait_for_text_set(host: SwitchHost, text: str, timeout: float = 10.0) -> None:
    """Wait until the runner's log shows the ``text set`` an account pushed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = _envelope(host.owner_json("logs", "--lines", "50"))
        events = body["data"]["events"]
        if any(event.get("event") == "text_set" and event.get("text") == text for event in events):
            return
        time.sleep(0.05)
    raise AssertionError(f"the runner never logged text_set {text!r}")


# ----------------------------------------------------------------------- tests


def test_an_allowed_non_owner_account_is_accepted(host: SwitchHost) -> None:
    """``access allow-user`` turns a refused second account into an accepted one."""
    assert host.start().returncode == 0, "the runner did not start"
    allowed = _account(ALLOWED_ACCOUNT)
    assert allowed is not None

    # Negative control: before the allow-list names the account, the *same*
    # command from the *same* process is refused. A green after this cannot be a
    # permissive default.
    before = host.as_account(ALLOWED_ACCOUNT, "status", runner_uid=OWNER_UID)
    assert before.returncode == protocol.EXIT_CODES["not_permitted"] == 5
    assert _envelope(before)["error"]["code"] == "not_permitted"

    added = host.owner_json("access", "allow-user", ALLOWED_ACCOUNT)
    assert added.returncode == 0
    assert ALLOWED_ACCOUNT in _envelope(added)["data"]["allowed_users"]

    # The pure policy the unit suite covers agrees with the server's answer.
    assert access.is_allowed(allowed.pw_uid, [allowed.pw_gid], _policy_with(ALLOWED_ACCOUNT))

    accepted = host.as_account(
        ALLOWED_ACCOUNT, "text", "set", "--text", "uid-switch", runner_uid=OWNER_UID
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert _envelope(accepted)["ok"] is True
    _wait_for_text_set(host, "uid-switch")


def test_a_third_account_is_refused(host: SwitchHost) -> None:
    """An account that is not the runner's and not on the allow-list gets exit 5."""
    assert host.start().returncode == 0, "the runner did not start"
    assert host.owner_json("access", "allow-user", ALLOWED_ACCOUNT).returncode == 0

    refused = _account(REFUSED_ACCOUNT)
    assert refused is not None
    # The allow-list exists and names a different account; daemon is not on it.
    daemon_allowed = access.is_allowed(
        refused.pw_uid, [refused.pw_gid], _policy_with(ALLOWED_ACCOUNT)
    )
    assert daemon_allowed is False

    proc = host.as_account(REFUSED_ACCOUNT, "status", runner_uid=OWNER_UID)
    assert proc.returncode == protocol.EXIT_CODES["not_permitted"] == 5
    body = _envelope(proc)
    assert body["ok"] is False
    assert body["error"]["code"] == "not_permitted"
    assert "not allowed" in body["error"]["message"]


def test_the_impersonation_guard_refuses_a_socket_owned_by_another_uid(host: SwitchHost) -> None:
    """A socket another account owns is refused unless ``$SMART_PANEL_RUNNER_UID`` names it."""
    assert host.start().returncode == 0, "the runner did not start"
    assert host.owner_json("access", "allow-user", ALLOWED_ACCOUNT).returncode == 0
    socket_path = host.runtime / "control.sock"
    assert socket_path.exists()
    # Real bytes on disk: the socket really is owned by the runner's uid, not this client's.
    assert socket_path.stat().st_uid == OWNER_UID
    allowed = _account(ALLOWED_ACCOUNT)
    assert allowed is not None
    # The account *is* allowed by policy, so a refusal below can only be the guard.
    assert access.is_allowed(allowed.pw_uid, [allowed.pw_gid], _policy_with(ALLOWED_ACCOUNT))

    unset = host.as_account(ALLOWED_ACCOUNT, "status")
    assert unset.returncode == protocol.EXIT_CODES["not_permitted"] == 5
    unset_body = _envelope(unset)
    assert unset_body["error"]["code"] == "not_permitted"
    assert "owned by uid" in unset_body["error"]["message"]

    wrong = host.as_account(ALLOWED_ACCOUNT, "status", runner_uid=_account(REFUSED_ACCOUNT).pw_uid)
    assert wrong.returncode == protocol.EXIT_CODES["not_permitted"] == 5
    assert "owned by uid" in _envelope(wrong)["error"]["message"]

    # Control: with the right uid exported, the very same command runs (exit 0).
    right = host.as_account(ALLOWED_ACCOUNT, "status", runner_uid=OWNER_UID)
    assert right.returncode == 0, right.stdout + right.stderr
    assert _envelope(right)["ok"] is True


def test_a_missing_runtime_directory_is_refused_not_created(host: SwitchHost) -> None:
    """With no runtime directory, a non-owner push exits 3 and creates nothing."""
    missing = host.base / "no-runtime-yet"
    assert not missing.exists()  # the runner never started since "boot"
    allowed = _account(ALLOWED_ACCOUNT)
    assert allowed is not None
    # The account is allowed by policy; the refusal is the client-side directory rule.
    assert access.is_allowed(allowed.pw_uid, [allowed.pw_gid], _policy_with(ALLOWED_ACCOUNT))

    proc = host.as_account(
        ALLOWED_ACCOUNT, "text", "set", "--text", "x", runner_uid=OWNER_UID, runtime=missing
    )
    assert proc.returncode == protocol.EXIT_CODES["runner_not_running"] == 3
    body = _envelope(proc)
    assert body["ok"] is False
    assert body["error"]["code"] == "runner_not_running"
    assert not missing.exists()
    assert "spool-pending" not in host.client_listing(ALLOWED_ACCOUNT)
