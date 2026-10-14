"""Integration test for the security surface (T118, FR-049/FR-050/FR-051).

Four properties the spec states as requirements and the CLI has to keep under
real conditions:

1. **Secrets stay in ``secrets.json``.** The file is mode 0600, ``secret list``
   shows names only, and a value the runner has *resolved* (a source header
   ``${secret:NAME}``) never appears in ``status``, ``logs`` (the control reply
   or the raw ``runner.log``) or ``config show`` — including inside a fetch
   failure's error message (FR-049). The HTTP helper's own redactor is checked
   directly too, so the error-message clause has a positive control.
2. **Hostile text is bounded plain text.** Control characters, markup and a
   100 KB string pushed into an alert, a crawl item and a sidebar count are
   normalized before they are stored or rendered: no control characters, a hard
   length bound, and an abbreviated chip — verified through the real runner's
   ``status`` (the same values the render reads) and by rendering a preview.
3. **A spool entry from a non-allowed owner is rejected** and deleted, through
   the runner's real :class:`~smart_panel.core.control.spool.SpoolIngest`. (The
   file's owner cannot be forged by an unprivileged test, so the policy — not
   the ownership call — is the substituted part.)
4. **A non-allowed peer receives exit 5.** A real control server whose policy
   excludes this account answers the real CLI with ``not_permitted``; the same
   CLI against an allowing policy exits 0, so the exit code is the policy's, not
   a broken socket.

Everything runs on the file/null sink: no panel is involved.
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

import pytest

from smart_panel.core.control import protocol
from smart_panel.core.control.access import Policy
from smart_panel.core.control.server import ControlServer
from smart_panel.core.control.spool import SpoolIngest
from smart_panel.core.control.spool import write_entry as write_spool_entry
from smart_panel.core.http import HttpError, HttpFetcher

REPO_ROOT = Path(__file__).resolve().parents[2]
CANVAS = (1920, 462)
OUR_UID = os.geteuid()
SECRET_NAME = "DEMO_TOKEN"
SECRET_VALUE = "s3cr3t-demo-token-9000-never-on-the-panel"
HOSTILE_MARKUP = "\x1b[31m<script>alert('x')</script>\x07\x01"
CONTROL_CHARS = {chr(code) for code in range(0x20)} - {"\n", "\r", "\t"}


# -------------------------------------------------------------------- fixtures


class CliHost:
    """A real CLI host: temp home/runtime, ``--json`` envelopes, no panel."""

    def __init__(self, base: Path, home: Path, runtime: Path) -> None:
        self.base = base
        self.home = home
        self.runtime = runtime
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["SMART_PANEL_HOME"] = str(home)
        env["SMART_PANEL_RUNTIME"] = str(runtime)
        env.pop("SMART_PANEL_JSON", None)
        env.pop("SMART_PANEL_RUNNER_UID", None)
        self.env = env
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "display": {"sink": "null", "min_text_px": 24},
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
                        "text": {"plugin": "text", "enabled": True, "settings": {"text": "s"}},
                        "sources": {"plugin": "sources", "enabled": True, "settings": {}},
                        "alerts": {"plugin": "alerts", "enabled": True, "settings": {}},
                        "crawl": {"plugin": "crawl", "enabled": True, "settings": {}},
                        "sidebar": {"plugin": "sidebar", "enabled": True, "settings": {}},
                    },
                }
            ),
            encoding="utf-8",
        )

    def cli(
        self, *args: str, env: dict[str, str] | None = None, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "smart_panel.cli.main", *args],
            capture_output=True,
            text=True,
            env=env or self.env,
            cwd=str(REPO_ROOT),
            input=stdin,
            timeout=60,
        )

    def json_cli(
        self, *args: str, env: dict[str, str] | None = None, stdin: str | None = None
    ) -> tuple[int, dict]:
        proc = self.cli("--json", *args, env=env, stdin=stdin)
        return proc.returncode, json.loads(proc.stdout)

    def data(self, *args: str, env: dict[str, str] | None = None, stdin: str | None = None) -> dict:
        code, body = self.json_cli(*args, env=env, stdin=stdin)
        assert body["ok"] is True, (code, body)
        return body["data"]

    def status(self) -> dict:
        return self.data("status")

    def start(self) -> None:
        code, body = self.json_cli("--timeout", "15", "start")
        assert code == 0, body

    def stop(self) -> None:
        self.cli("--json", "stop")


@pytest.fixture
def host(tmp_path: Path) -> Iterator[CliHost]:
    base = Path(tempfile.mkdtemp(prefix="security-", dir="/tmp"))
    cli_host = CliHost(base, tmp_path / "home", base / "rt")
    try:
        yield cli_host
    finally:
        cli_host.stop()
        shutil.rmtree(base, ignore_errors=True)


def _wait_until(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _sources_detail(status: dict) -> dict:
    return status["plugins"]["sources"]["detail"]["sources"]


# ------------------------------------------------------- secrets (FR-049)


def test_secrets_json_is_mode_0600_and_lists_names_only(host: CliHost) -> None:
    host.start()
    assert host.data("secret", "set", SECRET_NAME, "--stdin", stdin=SECRET_VALUE)["stored"] is True

    secrets_file = host.home / "secrets.json"
    mode = secrets_file.stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)
    # Non-vacuous: the value really is in the file, and it is not in the listing.
    assert SECRET_VALUE in secrets_file.read_text(encoding="utf-8")
    listed = host.data("secret", "list")["names"]
    assert listed == [SECRET_NAME]
    assert SECRET_VALUE not in json.dumps(listed)


def test_a_resolved_secret_never_appears_in_status_logs_or_config_show(host: CliHost) -> None:
    host.start()
    host.data("secret", "set", SECRET_NAME, "--stdin", stdin=SECRET_VALUE)

    # A source whose header resolves the secret and whose URL cannot be reached:
    # the runner resolves the value, registers it for redaction, and reports the
    # failure in `status` (a real error message).
    settings = {
        "url": "http://127.0.0.1:1/headlines",
        "headers": {"Authorization": f"${{secret:{SECRET_NAME}}}"},
        "fields": {"text": "{title}"},
    }
    host.data(
        "data",
        "source",
        "add",
        "--id",
        "news",
        "--type",
        "http_json",
        "--interval",
        "30",
        "--settings",
        json.dumps(settings),
    )

    assert _wait_until(lambda: _source_state(host, "news") is not None), "the fetch never failed"
    status = host.status()
    state = _sources_detail(status)["news"]

    # The failure really happened and names the endpoint ...
    assert state["last_error"] and "127.0.0.1:1" in state["last_error"]
    # ... and no read surface leaks the resolved value.
    assert SECRET_VALUE not in json.dumps(status)
    assert SECRET_VALUE not in json.dumps(host.data("logs", "--lines", "200"))
    assert SECRET_VALUE not in json.dumps(host.data("config", "show"))
    assert SECRET_VALUE not in (host.home / "runner.log").read_text(encoding="utf-8")


def _source_state(host: CliHost, name: str) -> str | None:
    try:
        return _sources_detail(host.status())[name]["last_error"]
    except (KeyError, TypeError):
        return None


def test_the_http_helper_redacts_a_secret_from_an_error_message() -> None:
    # The positive control for the error-message clause: the URL itself carries
    # the value, so without redaction it would be reported verbatim.
    fetcher = HttpFetcher(timeout_s=1.0, secret_resolver=lambda name: SECRET_VALUE)
    headers = fetcher.resolve_headers({"Authorization": f"${{secret:{SECRET_NAME}}}"})
    assert headers == {"Authorization": SECRET_VALUE}

    with pytest.raises(HttpError) as excinfo:
        fetcher.get(f"http://127.0.0.1:1/{SECRET_VALUE}")
    message = excinfo.value.message
    assert SECRET_VALUE not in message
    assert "***" in message


# ---------------------------------------------- hostile text (bounded, plain)


def test_a_hostile_alert_message_is_stored_as_bounded_plain_text(host: CliHost) -> None:
    host.start()
    payload = f"{HOSTILE_MARKUP}{'A' * 100_000}"
    result = host.data(
        "alert", "push", "--severity", "critical", "--message", payload, "--id", "h1"
    )

    stored = host.data("alert", "list")["alerts"]
    alert = next(item for item in stored if item["ext_id"] == "h1")
    message = alert["message"]
    assert len(message) <= 280, len(message)
    assert not (CONTROL_CHARS & set(message))
    assert "<script>" in message  # markup is literal text, never interpreted
    # The caller is told the overflow was bounded, rather than silently dropped.
    assert "truncated" in result["warning"]

    # The hostile string does not blow up a render: a preview is still produced.
    preview = host.home / "hostile.png"
    data = host.data("preview", "--output", str(preview))
    assert preview.exists() and data["bytes"] < 1_000_000


def test_a_hostile_crawl_item_is_truncated_at_a_word_boundary(host: CliHost) -> None:
    host.start()
    # Through ``--stdin`` so the payload may carry a NUL (argv cannot).
    payload = f"\x00{HOSTILE_MARKUP}{'B' * 100_000}"
    result = host.data("crawl", "items", "set", "--stdin", stdin=payload + "\n")
    assert result["count"] == 1

    item = host.status()["plugins"]["crawl"]["detail"]["item_texts"][0]
    assert len(item) <= 280, len(item)
    assert item.endswith("…")
    assert not (CONTROL_CHARS & set(item))
    assert "<script>" in item


def test_a_hostile_sidebar_count_is_abbreviated_and_a_bad_one_rejected(host: CliHost) -> None:
    host.start()
    host.data("sidebar", "entry", "add", "--id", "mail", "--icon", "mail", "--label", "Mail",
              "--topic", "count.mail")

    host.data("sidebar", "count", "set", "mail", "1000000000000000000000000000000")
    entries = host.status()["plugins"]["sidebar"]["detail"]["entries"]
    entry = next(item for item in entries if item["id"] == "mail")
    assert entry["shown"] == "999+", entry

    code, body = host.json_cli("sidebar", "count", "set", "mail", "not-a-number")
    assert code == 2
    assert body["error"]["code"] == "invalid_input"


# ------------------------------------------------------------ spool ownership


def test_a_spool_entry_from_a_non_allowed_owner_is_rejected(host: CliHost) -> None:
    directory = host.runtime / "spool"
    directory.mkdir(parents=True, exist_ok=True)
    entry = write_spool_entry(
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


# ------------------------------------------------------------- peer credentials


def _server(host: CliHost, policy: Policy) -> ControlServer:
    def dispatch(request: protocol.Request) -> protocol.Response:
        return protocol.Response.success(request.id, {"cmd": request.cmd})

    host.runtime.mkdir(parents=True, exist_ok=True)
    server = ControlServer(
        dispatch,
        policy_provider=lambda: policy,
        socket_path=host.runtime / "control.sock",
    )
    server.start()
    return server


def test_a_non_allowed_peer_receives_exit_five(host: CliHost) -> None:
    env = dict(host.env)
    env["SMART_PANEL_RUNNER_UID"] = str(OUR_UID + 999)
    server = _server(host, Policy(runner_uid=OUR_UID + 999, allowed_uids=frozenset()))
    try:
        code, body = host.json_cli("status", env=env)
    finally:
        server.stop()

    assert code == 5
    assert body["ok"] is False
    assert body["error"]["code"] == "not_permitted"


def test_an_allowed_peer_reaches_the_same_socket(host: CliHost) -> None:
    # Positive control: identical setup, permissive policy -> exit 0, real data.
    env = dict(host.env)
    env["SMART_PANEL_RUNNER_UID"] = str(OUR_UID)
    server = _server(host, Policy(runner_uid=OUR_UID, allowed_uids=frozenset()))
    try:
        code, body = host.json_cli("status", env=env)
    finally:
        server.stop()

    assert code == 0, body
    assert body["data"] == {"cmd": "runner.status"}
