"""End-to-end pin: ``text set --text ''`` blanks a region (t_c47a1052).

Drives the real ``python -m smart_panel.cli.main`` against a runner with a file
sink, so the whole path is exercised — CLI argument parsing (including the empty
argument), the ``plugin.call`` round trip, the ``text`` plugin's ``set`` verb,
the compose loop and the ``FileSink``. Contract ``contracts/builtin-plugins.md``
(``text``) permits empty text and defines it as a background-only frame; the
composed canvas keeps its size and carries no ink (``min == max == 0``).

The assertion is two-sided on purpose, so the test goes red on either rejected
reading: option (a) "refuse" makes the CLI exit 2 instead of 0, and option (c)
"clamp/substitute" leaves ink on the canvas, so the blank-frame check never
satisfies. ``min == max == 0`` is read from ``FileSink``'s ``latest.png`` — the
same evidence the card's observation names.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PIL import Image as PILImage

REPO_ROOT = Path(__file__).resolve().parents[2]
CANVAS = (1920, 462)
BLANK = (0, 0)


# -------------------------------------------------------------------- fixtures


class CliHost:
    """A CLI host pointed at a runner with a file sink and a fake clock."""

    def __init__(self, base: Path, home: Path) -> None:
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

    def cli(self, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "smart_panel.cli.main", *args],
            capture_output=True,
            text=True,
            env=self.env,
            cwd=str(REPO_ROOT),
            timeout=timeout,
        )

    def data(
        self, *args: str, timeout: float = 60.0
    ) -> tuple[dict, subprocess.CompletedProcess[str]]:
        proc = self.cli(*args, timeout=timeout)
        return json.loads(proc.stdout), proc

    def seed_config(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.json").write_text(
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
                        "text": {
                            "plugin": "text",
                            "enabled": True,
                            "settings": {"text": "initial", "size": 120},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.clock.write_text("0\n", encoding="utf-8")

    def start(self) -> subprocess.CompletedProcess[str]:
        # The CLI detaches a ``run`` child and waits ``max(--timeout, 10)`` for
        # its first frame; 15 s was too tight for a starved CI leg, so the host
        # uses the 30 s the sibling e2e hosts settled on.
        return self.cli(
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


@pytest.fixture
def host(tmp_path: Path) -> Iterator[CliHost]:
    base = Path(tempfile.mkdtemp(prefix="empty-text-", dir="/tmp"))
    cli_host = CliHost(base, tmp_path / "home")
    cli_host.seed_config()
    try:
        yield cli_host
    finally:
        cli_host.stop()
        shutil.rmtree(base, ignore_errors=True)


# --------------------------------------------------------------------- helpers


def _latest_extrema(host: CliHost) -> tuple[int, int]:
    """The (min, max) luminance of the current ``FileSink`` frame."""
    with PILImage.open(host.frames / "latest.png") as image:
        image.load()
        luminance = image.convert("L")
        assert luminance.size == CANVAS, f"partial frame: {luminance.size}"
        return luminance.getextrema()  # type: ignore[return-value]


def _wait_for_extrema(
    host: CliHost, predicate: Callable[[tuple[int, int]], bool], *, timeout: float
) -> tuple[int, int]:
    deadline = time.monotonic() + timeout
    last: tuple[int, int] | None = None
    while time.monotonic() < deadline:
        if (host.frames / "latest.png").exists():
            last = _latest_extrema(host)
            if predicate(last):
                return last
        time.sleep(0.05)
    raise AssertionError(
        f"the FileSink canvas never matched before {timeout}s (last extrema={last})"
    )


# ----------------------------------------------------------------------- test


def test_text_set_empty_blanks_the_frame(host: CliHost) -> None:
    # The same 30 s ``start`` bound as ``CliHost.start`` (and the sibling e2e
    # hosts): 15 s was too tight for a starved CI leg.
    started, proc = host.data("--json", "--timeout", "30", "start", "--sink", "files",
                              "--files-dir", str(host.frames), "--clock-file", str(host.clock),
                              timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert started["data"]["state"] == "running"

    # The runner's first *sent* frame carries the configured text, so the canvas
    # has ink: the baseline the blank-frame assertion is measured against.
    assert _wait_for_extrema(host, lambda ext: ext[1] > 200, timeout=10.0) != BLANK

    # ``text set --text ''`` is accepted (exit 0), NOT refused with exit 2.
    result, proc = host.data("--json", "text", "set", "--text", "")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert result["ok"] is True
    assert result["data"] == {"text": ""}

    # The region now draws nothing: a full-size, fully blank canvas (min == max == 0).
    assert _wait_for_extrema(host, lambda ext: ext == BLANK, timeout=5.0) == BLANK
    # And it stays blank — not a single transient frame in the sequence.
    time.sleep(0.2)
    assert _latest_extrema(host) == BLANK
