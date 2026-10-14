"""Integration test: crawl sources refresh against a local HTTP server (T079, US3).

Drives the real CLI and a runner with the file sink and the test-only fake clock
(quickstart Scenario D plus the sources parts of the US3 independent test):

* an RSS source (the recorded Google News fixture) and a JSON source (the
  recorded ESPN scoreboard fixture) are fetched on their own interval, from a
  local HTTP server, and each source reports ``last_fetch`` in ``status``;
* refreshing does not interrupt the scroll — frames keep being written and the
  crawl window keeps moving;
* when a source becomes unreachable the crawl keeps its last known items and the
  source's ``last_error`` appears in ``status``;
* ``crawl items set`` takes several positional items and ``--stdin`` one item
  per line, so an agent can supply headlines directly (FR-030).
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
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image as PILImage

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
CANVAS = (1920, 462)
MAIN = (192, 0, 1728, 382)
CRAWL = (192, 382, 1728, 80)
MAIN_BG = (0, 0, 85)  # #000055, the text region under the crawl
ESPN_HEADLINE = "TB 0 @ DAL 0 - 10/8 - 8:15 PM EDT"
NFL_TEMPLATE = (
    "{competitions.0.competitors.1.team.abbreviation} "
    "{competitions.0.competitors.1.score} @ "
    "{competitions.0.competitors.0.team.abbreviation} "
    "{competitions.0.competitors.0.score} - {status.type.shortDetail}"
)


class _QuietHandler(SimpleHTTPRequestHandler):
    """A static file handler that keeps the test output clean."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


class FeedServer:
    """A throwaway HTTP server for ``tests/fixtures``."""

    def __init__(self) -> None:
        handler = functools.partial(_QuietHandler, directory=str(FIXTURES))
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class CliHost:
    """A CLI host pointed at a runner with a file sink and a fake clock."""

    def __init__(self, base: Path, home: Path, feed: FeedServer) -> None:
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
                                "name": "main",
                                "x": MAIN[0],
                                "y": MAIN[1],
                                "w": MAIN[2],
                                "h": MAIN[3],
                                "instance": "text",
                            },
                            {
                                "name": "crawl",
                                "x": CRAWL[0],
                                "y": CRAWL[1],
                                "w": CRAWL[2],
                                "h": CRAWL[3],
                                "instance": "crawl",
                            },
                        ],
                        "overlays": [],
                    },
                    "plugins": {
                        "text": {
                            "plugin": "text",
                            "enabled": True,
                            "settings": {"text": "MAIN", "bg": "#000055", "size": 120},
                        },
                        "crawl": {
                            "plugin": "crawl",
                            "enabled": True,
                            "settings": {
                                "fg": "#FFFFFF",
                                "bg": "#B00020",
                                "size": 44,
                                "speed_px_s": 200,
                                "sources": [
                                    {
                                        "id": "news",
                                        "kind": "rss",
                                        "url": f"{feed.url}/google_news_sample.xml",
                                        "max_items": 5,
                                        "interval_s": 30,
                                    },
                                    {
                                        "id": "nfl",
                                        "kind": "json",
                                        "url": f"{feed.url}/espn_nfl_scoreboard.json",
                                        "list_path": "events",
                                        "template": NFL_TEMPLATE,
                                        "interval_s": 30,
                                    },
                                ],
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

    def data(self) -> tuple[dict, int]:
        proc = self.cli("--json", "status")
        return json.loads(proc.stdout), proc.returncode

    def ok(self, *args: str, input: str | None = None, timeout: float = 60.0) -> dict:
        proc = self.cli(*args, input=input, timeout=timeout)
        payload = json.loads(proc.stdout)
        assert proc.returncode == 0, f"{args}: exit {proc.returncode}: {payload}"
        assert payload["ok"] is True, payload
        return payload["data"]

    def status(self) -> dict:
        return self.ok("--json", "status")

    def set_clock(self, value: float) -> None:
        self.clock.write_text(f"{value}\n", encoding="utf-8")

    def start(self) -> dict:
        # The CLI detaches a ``run`` child and waits ``max(--timeout, 10)`` for
        # its first frame, so the host's bound is the CLI's bound. 15 s was too
        # tight for a starved CI leg — macos-latest has been seen to starve a
        # render worker for over 25 s — so this uses the 30 s the sibling e2e
        # hosts settled on (``test_gauges_e2e``, ``test_sidebar_e2e``,
        # ``test_plugin_isolation``, ``test_dropin_plugin``). It stays bounded:
        # a runner that never comes up still fails.
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

    def region(self, image: PILImage.Image, box: tuple[int, int, int, int]) -> bytes:
        x, y, w, h = box
        return image.crop((x, y, x + w, y + h)).tobytes()

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

    def wait_for_new_frame(self, after_ns: int, *, timeout: float = 20.0) -> None:
        """Wait for ``latest.png`` to be rewritten past ``after_ns``.

        The runner re-sends frames continuously, so the file's mtime is the
        observable effect of "frames keep flowing". Sampling it twice and
        comparing cannot tell a stopped runner from a host that starved the
        compositor for the length of the window between the two samples (seen on
        macos-latest in ``test_refreshing_does_not_interrupt_the_scroll``), so
        the check waits for the effect instead.
        """
        path = self.frames / "latest.png"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists() and path.stat().st_mtime_ns != after_ns:
                return
            time.sleep(0.05)
        raise AssertionError(
            f"no frame was written after {after_ns} within {timeout}s "
            "(the runner stopped sending frames)"
        )

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


def _crawl_detail(status: dict) -> dict:
    return status["plugins"]["crawl"]["detail"]


def _ink(image: PILImage.Image, box: tuple[int, int, int, int]) -> int:
    x, y, w, h = box
    region = image.crop((x, y, x + w, y + h))
    colours = region.getcolors(maxcolors=1 << 20) or []
    background = max(colours, key=lambda pair: pair[0])[1]
    return sum(count for count, colour in colours if colour != background)


@pytest.fixture
def feed() -> Iterator[FeedServer]:
    server = FeedServer()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def host(tmp_path: Path, feed: FeedServer) -> Iterator[CliHost]:
    base = Path(tempfile.mkdtemp(prefix="crawl-e2e-", dir="/tmp"))
    cli_host = CliHost(base, tmp_path / "home", feed)
    try:
        yield cli_host
    finally:
        cli_host.stop()
        shutil.rmtree(base, ignore_errors=True)


def test_each_source_reports_last_fetch_and_the_ticker_scrolls(host: CliHost) -> None:
    host.start()

    def ready(status: dict) -> bool:
        detail = _crawl_detail(status)
        return all(
            detail["sources"][source]["last_fetch"] is not None for source in ("news", "nfl")
        )

    status = host.wait_for_status(ready, what="the first refresh of both sources")
    detail = _crawl_detail(status)
    assert detail["sources"]["news"]["last_error"] is None
    assert detail["sources"]["news"]["items"] == 5
    assert detail["sources"]["nfl"]["items"] == 3
    assert detail["sources"]["nfl"]["kind"] == "json"
    assert detail["items"] == 8

    # The two sources are interleaved round-robin by default, and the ticker is
    # actually moving (its position comes from the clock, which the test drives).
    first = host.wait_for_frame(
        lambda frame: _ink(frame, CRAWL) > 0, what="the crawl's first items"
    )
    assert first.getpixel((1900, 5)) == MAIN_BG
    host.set_clock(1.0)
    moved = host.wait_for_frame(
        lambda frame: host.region(frame, CRAWL) != host.region(first, CRAWL),
        what="the ticker to move",
    )
    assert moved.size == CANVAS


def test_refreshing_does_not_interrupt_the_scroll(host: CliHost) -> None:
    host.start()
    host.wait_for_status(
        lambda status: _crawl_detail(status)["sources"]["nfl"]["last_fetch"] is not None,
        what="the first refresh",
    )
    before = (host.frames / "latest.png").stat().st_mtime_ns
    host.set_clock(31.0)  # the second refresh of both sources is now due
    host.wait_for_status(
        lambda status: _crawl_detail(status)["sources"]["news"]["fetches"] >= 2,
        what="the second refresh",
    )
    # Frames must keep flowing across the refresh. Wait for the next one instead
    # of sampling ``latest.png`` twice: a starved CI host can leave the file
    # untouched for as long as a CLI round trip takes, and that is not the same
    # as "the runner stopped sending frames" (observed on macos-latest).
    host.wait_for_new_frame(before)
    assert host.status()["state"] == "running"


def test_an_unreachable_source_keeps_its_items_and_reports_last_error(
    host: CliHost, feed: FeedServer
) -> None:
    host.start()
    host.wait_for_status(
        lambda status: _crawl_detail(status)["sources"]["news"]["last_fetch"] is not None,
        what="the first refresh",
    )
    baseline = host.wait_for_frame(lambda frame: _ink(frame, CRAWL) > 0)
    items_before = _crawl_detail(host.status())["sources"]["news"]["items"]
    assert items_before == 5

    feed.close()
    host.set_clock(35.0)

    def failed(status: dict) -> bool:
        detail = _crawl_detail(status)["sources"]["news"]
        return detail["last_error"] is not None

    status = host.wait_for_status(failed, what="the source's last_error")
    detail = _crawl_detail(status)["sources"]["news"]
    assert "***" not in detail["last_error"]
    assert detail["items"] == items_before, "a failed refresh must keep the last known items"

    # The crawl keeps scrolling with the items it already had.
    still_moving = host.wait_for_frame(
        lambda frame: host.region(frame, CRAWL) != host.region(baseline, CRAWL),
        what="the ticker to keep moving after a failed refresh",
    )
    assert _ink(still_moving, CRAWL) > 0
    assert host.status()["state"] == "running"


def test_cli_items_set_takes_positional_items_and_stdin(host: CliHost) -> None:
    host.start()
    host.wait_for_frame(lambda frame: _ink(frame, CRAWL) > 0)

    set_result = host.ok(
        "--json", "crawl", "items", "set", "Alpha headline", "Beta headline", "Gamma headline"
    )
    assert set_result == {"source": "cli", "count": 3}

    added = host.ok(
        "--json", "crawl", "items", "add", "--stdin", input="Delta headline\nEpsilon headline\n"
    )
    assert added == {"source": "cli", "count": 5}

    listed = host.ok("--json", "crawl", "source", "list")
    cli_source = [source for source in listed["sources"] if source["id"] == "cli"][0]
    assert cli_source["items"] == 5

    # The ESPN preset adds a JSON source through the same verb group.
    added_source = host.ok(
        "--json", "crawl", "source", "add", "--id", "espn", "--kind", "json", "--preset", "espn-nfl"
    )
    assert added_source["list_path"] == "events"
    presets = host.ok("--json", "crawl", "preset", "list")["presets"]
    assert [preset["name"] for preset in presets][:2] == ["espn-nfl", "espn-nba"]

    host.set_clock(61.0)
    host.wait_for_status(
        lambda status: _crawl_detail(status)["sources"]["espn"]["last_fetch"] is not None,
        what="the ESPN source's first refresh",
    )
    espn_items = [
        item for item in _crawl_detail(host.status())["item_texts"] if ESPN_HEADLINE in item
    ]
    assert espn_items, "the ESPN preset headline never reached the strip"
