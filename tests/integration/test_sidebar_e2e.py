"""End-to-end test: the sidebar with the file sink and a fake clock (T104, US5).

Runs quickstart **Scenario F** against the real CLI and a real runner:

* three entries show icons and count chips;
* ``sidebar count set mail 1204`` shows ``999+`` and setting it to ``0`` hides the
  chip (``hide_when_zero``);
* a count that changes gets a brief highlight;
* a cross-plugin check: an alert that times out increments the sidebar's built-in
  alert entry through the ``alerts.missed`` topic in the missed alert's severity
  colour, and ``alert clear-missed`` hides it;
* a pull-source case: an ``http_json`` source (a local test server) feeds a
  sidebar entry's count on its refresh interval (FR-039).
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image as PILImage

REPO_ROOT = Path(__file__).resolve().parents[2]
CANVAS = (1920, 462)
SIDEBAR = (0, 0, 192, 462)
MAIN = (192, 0, 1728, 382)
SIDEBAR_BG = (11, 14, 17)  # #0B0E11
CHIP_BG = (214, 40, 57)  # #D62839
INFO = (46, 125, 215)  # #2E7DD7
HIGHLIGHT = (255, 209, 102)  # #FFD166


class _CountHandler(BaseHTTPRequestHandler):
    """Serve ``{"count": N}``; the count can be changed between refreshes."""

    state: dict[str, int] = {"count": 7}

    def do_GET(self) -> None:  # noqa: N802 - the BaseHTTPRequestHandler API
        body = json.dumps({"count": self.state["count"]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


class CountServer:
    """A throwaway JSON endpoint for the ``http_json`` pull-source case."""

    def __init__(self) -> None:
        handler = functools.partial(_CountHandler)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/counts.json"

    def set_count(self, value: int) -> None:
        _CountHandler.state["count"] = int(value)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class CliHost:
    """A CLI host pointed at a runner with a file sink and a fake clock."""

    def __init__(self, base: Path, home: Path, server: CountServer) -> None:
        self.base = base
        self.home = home
        self.runtime = base / "runtime"
        self.frames = base / "frames"
        self.clock = base / "clock"
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["SMART_PANEL_HOME"] = str(home)
        env["SMART_PANEL_RUNTIME"] = str(self.runtime)
        env["SMART_PANEL_TESTING"] = "1"
        env.pop("SMART_PANEL_JSON", None)
        env.pop("SMART_PANEL_RUNNER_UID", None)
        self.env = env

        self.home.mkdir(parents=True, exist_ok=True)
        (home / "config.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "display": {
                        "fps": 15,
                        "brightness": 50,
                        "sink": "files",
                        "files_dir": str(self.frames),
                        "min_text_px": 24,
                    },
                    "layout": {
                        "regions": [
                            {
                                "name": "sidebar",
                                "x": SIDEBAR[0],
                                "y": SIDEBAR[1],
                                "w": SIDEBAR[2],
                                "h": SIDEBAR[3],
                                "instance": "sidebar",
                            },
                            {
                                "name": "main",
                                "x": MAIN[0],
                                "y": MAIN[1],
                                "w": MAIN[2],
                                "h": MAIN[3],
                                "instance": "text",
                            },
                        ],
                        "overlays": [{"region": "main", "instance": "alerts"}],
                    },
                    "plugins": {
                        "text": {
                            "plugin": "text",
                            "enabled": True,
                            "settings": {"text": "MAIN", "bg": "#000055", "size": 120},
                        },
                        "sidebar": {
                            "plugin": "sidebar",
                            "enabled": True,
                            "settings": {"entries": []},
                        },
                        "alerts": {
                            "plugin": "alerts",
                            "enabled": True,
                            "settings": {
                                "durations": {"info": 1, "warning": 1, "critical": 1},
                                "max_wait_s": 1,
                                "queue_cap": 5,
                            },
                        },
                        "sources": {
                            "plugin": "sources",
                            "enabled": True,
                            "settings": {
                                "sources": {
                                    "inbox": {
                                        "type": "http_json",
                                        "interval_s": 30,
                                        "settings": {
                                            "url": server.url,
                                            "fields": {"count": "{count}"},
                                        },
                                    }
                                }
                            },
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        self.set_clock(0.0)

    # ------------------------------------------------------------- transport

    def cli(
        self, *args: str, input: str | None = None, timeout: float = 60.0
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "smart_panel.cli.main", *args],
            capture_output=True,
            text=True,
            input=input,
            env=self.env,
            cwd=str(REPO_ROOT),
            timeout=timeout,
        )

    def ok(self, *args: str, timeout: float = 60.0) -> dict:
        proc = self.cli(*args, timeout=timeout)
        payload = json.loads(proc.stdout)
        assert proc.returncode == 0, f"{args}: exit {proc.returncode}: {payload}"
        assert payload["ok"] is True, payload
        return payload["data"]

    def status(self) -> dict:
        return self.ok("--json", "status")

    def set_clock(self, value: float) -> None:
        self.clock.write_text(f"{value}\n", encoding="utf-8")

    def start(self) -> dict:
        return self.ok(
            "--json",
            "--timeout",
            "30",
            "start",
            "--sink",
            "files",
            "--files-dir",
            str(self.frames),
            "--clock-file",
            str(self.clock),
            timeout=60,
        )

    def stop(self) -> subprocess.CompletedProcess[str]:
        return self.cli("--json", "stop", timeout=30)

    def frame(self) -> PILImage.Image:
        with PILImage.open(self.frames / "latest.png") as image:
            image.load()
            return image.convert("RGB").copy()

    def wait_for_frame(
        self,
        predicate: Callable[[PILImage.Image], bool],
        *,
        timeout: float = 20.0,
        what: str = "the frame",
    ) -> PILImage.Image:
        deadline = time.monotonic() + timeout
        last: PILImage.Image | None = None
        while time.monotonic() < deadline:
            if (self.frames / "latest.png").exists():
                last = self.frame()
                if predicate(last):
                    return last
            time.sleep(0.05)
        raise AssertionError(f"{what} never appeared within {timeout}s (last={last})")

    def wait_for_status(
        self,
        predicate: Callable[[dict], bool],
        *,
        timeout: float = 20.0,
        what: str = "the status",
    ) -> dict:
        deadline = time.monotonic() + timeout
        last: dict = {}
        while time.monotonic() < deadline:
            last = self.status()
            if predicate(last):
                return last
            time.sleep(0.2)
        raise AssertionError(f"{what} never appeared within {timeout}s (last={last})")


# --------------------------------------------------------------------- helpers


def _sidebar_detail(status: dict) -> dict:
    return status["plugins"]["sidebar"]["detail"]


def _entry(status: dict, entry_id: str) -> dict:
    for item in _sidebar_detail(status)["entries"]:
        if item["id"] == entry_id:
            return item
    raise AssertionError(f"no sidebar entry {entry_id!r} in {_sidebar_detail(status)}")


def _crop(image: PILImage.Image, box: tuple[int, int, int, int]) -> PILImage.Image:
    x, y, w, h = box
    return image.crop((x, y, x + w, y + h))


def _colours(image: PILImage.Image) -> list[tuple[int, tuple[int, int, int]]]:
    return image.getcolors(maxcolors=1 << 20) or []


def _count_colour(image: PILImage.Image, colour: tuple[int, int, int]) -> int:
    return sum(count for count, rgb in _colours(image) if rgb == colour)


def _ink(image: PILImage.Image) -> int:
    colours = _colours(image)
    if not colours:
        return 0
    background = max(colours, key=lambda pair: pair[0])[1]
    return sum(count for count, colour in colours if colour != background)


@pytest.fixture
def server() -> Iterator[CountServer]:
    _CountHandler.state = {"count": 7}
    instance = CountServer()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def host(tmp_path: Path, server: CountServer) -> Iterator[CliHost]:
    base = Path(tempfile.mkdtemp(prefix="sidebar-e2e-", dir="/tmp"))
    cli_host = CliHost(base, tmp_path / "home", server)
    try:
        yield cli_host
    finally:
        cli_host.stop()
        shutil.rmtree(base, ignore_errors=True)


def _add_entries(host: CliHost) -> None:
    for entry_id, icon, label in (
        ("mail", "mail", "Mail"),
        ("chat", "chat", "Chat"),
        ("calendar", "calendar", "Cal"),
    ):
        added = host.ok(
            "--json",
            "sidebar",
            "entry",
            "add",
            "--id",
            entry_id,
            "--icon",
            icon,
            "--label",
            label,
            "--topic",
            f"count.{entry_id}",
        )
        assert added["id"] == entry_id


# ----------------------------------------------------------------- the tests


def test_scenario_f_entries_chips_and_the_alert_entry(host: CliHost) -> None:
    host.start()
    host.wait_for_status(lambda status: "sidebar" in status["plugins"], what="the sidebar")

    _add_entries(host)
    status = host.wait_for_status(
        lambda status: _sidebar_detail(status)["count"] == 3, what="three entries"
    )
    assert [entry["id"] for entry in _sidebar_detail(status)["entries"]] == [
        "mail",
        "chat",
        "calendar",
    ]
    assert _sidebar_detail(status)["max"] == 6
    assert _entry(status, "mail")["shown"] == "?", "a count is unknown until it is set"
    assert _entry(status, "mail")["icon"] == "mail"
    assert _entry(status, "mail")["topic"] == "count.mail"
    assert _sidebar_detail(status)["alert"]["topic"] == "alerts.missed"
    assert _sidebar_detail(status)["alert"]["shown"] is None, "the alert entry is idle at zero"

    # Every row carries an icon; the sidebar paints its own background.
    frame = host.wait_for_frame(
        lambda image: _ink(_crop(image, SIDEBAR)) > 0, what="the sidebar entries"
    )
    assert frame.size == CANVAS
    assert _crop(frame, SIDEBAR).convert("RGB").getpixel((1, 1)) == SIDEBAR_BG

    # Scenario F: a big count is abbreviated, a zero hides the chip.
    set_ = host.ok("--json", "sidebar", "count", "set", "mail", "1204")
    assert set_["value"] == 1204
    status = host.wait_for_status(
        lambda status: _entry(status, "mail")["shown"] == "999+", what="the 999+ chip"
    )
    assert _entry(status, "mail")["count"] == 1204

    host.ok("--json", "sidebar", "count", "set", "mail", "0")
    status = host.wait_for_status(
        lambda status: _entry(status, "mail")["shown"] is None, what="the hidden chip"
    )
    assert _entry(status, "mail")["count"] == 0
    assert host.status()["state"] == "running"

    # A count that changes gets a brief highlight; the clock moving on clears it.
    host.ok("--json", "sidebar", "count", "set", "chat", "12")
    host.wait_for_status(lambda status: _entry(status, "chat")["shown"] == "12", what="chat")
    host.wait_for_frame(
        lambda image: _count_colour(_crop(image, SIDEBAR), HIGHLIGHT) > 0,
        what="the change highlight",
    )
    host.set_clock(2.0)
    host.wait_for_frame(
        lambda image: _count_colour(_crop(image, SIDEBAR), HIGHLIGHT) == 0,
        what="the highlight to fade",
    )
    assert _entry(host.status(), "chat")["shown"] == "12"

    # Cross-plugin: an alert that times out increments the built-in alert entry
    # through ``alerts.missed``, in the missed alert's severity colour...
    pushed = host.ok(
        "--json",
        "alert",
        "push",
        "--severity",
        "info",
        "--message",
        "Disk almost full",
        "--ttl",
        "1",
    )
    assert pushed["state"] == "displayed"
    host.wait_for_frame(
        lambda image: _count_colour(_crop(image, MAIN), INFO) > 0,
        what="the alert overlay",
    )
    host.set_clock(10.0)  # the displayed alert's 1 s duration elapses
    # No ``status`` call in between: the alerts plugin's own per-frame tick is what
    # expires the alert, and that tick is what publishes ``alerts.missed``.
    host.wait_for_frame(
        lambda image: _count_colour(_crop(image, MAIN), INFO) == 0,
        what="the alert to time out",
    )
    host.wait_for_status(
        lambda status: status["plugins"]["alerts"]["detail"]["missed"] == 1,
        what="the missed alert",
    )
    status = host.wait_for_status(
        lambda status: _sidebar_detail(status)["alert"]["shown"] == "1",
        what="the sidebar alert count",
    )
    assert _sidebar_detail(status)["alert"]["max_severity"] == "info"
    host.wait_for_frame(
        lambda image: _count_colour(_crop(image, SIDEBAR), INFO) > 0,
        what="the info-coloured alert chip",
    )

    # ...and ``alert clear-missed`` hides it again.
    cleared = host.ok("--json", "alert", "clear-missed")
    assert cleared["cleared"] == 1
    host.wait_for_status(
        lambda status: _sidebar_detail(status)["alert"]["shown"] is None,
        what="the hidden alert chip",
    )
    host.wait_for_frame(
        lambda image: _count_colour(_crop(image, SIDEBAR), INFO) == 0,
        what="the alert chip to disappear",
    )
    assert host.status()["state"] == "running"


def test_a_pull_source_feeds_an_entry_count_on_its_refresh_interval(host: CliHost) -> None:
    host.start()
    host.wait_for_status(lambda status: "sources" in status["plugins"], what="the sources")

    added = host.ok(
        "--json",
        "sidebar",
        "entry",
        "add",
        "--id",
        "inbox",
        "--icon",
        "mail",
        "--label",
        "Inbox",
        "--topic",
        "inbox.count",
    )
    assert added["id"] == "inbox"

    # The ``http_json`` source publishes ``inbox.count`` on its first fetch at start.
    status = host.wait_for_status(
        lambda status: _entry(status, "inbox")["shown"] == "7",
        what="the pulled count",
    )
    assert _entry(status, "inbox")["count"] == 7

    # Changing the endpoint and letting the 30 s interval elapse refreshes it.
    server_count = _CountHandler.state["count"]
    assert server_count == 7
    _CountHandler.state["count"] = 42
    host.set_clock(31.0)
    status = host.wait_for_status(
        lambda status: _entry(status, "inbox")["shown"] == "42",
        what="the refreshed count",
    )
    assert _entry(status, "inbox")["count"] == 42
    assert host.status()["state"] == "running"
