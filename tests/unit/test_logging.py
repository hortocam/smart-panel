"""Unit tests for ``smart_panel.core.logging`` (T022).

The log is JSON-lines, rotates, keeps the last 200 events in memory for
``status``/``logs``, and never writes a value registered as a secret.
"""

from __future__ import annotations

import json
from pathlib import Path

from smart_panel.core.logging import RunnerLog


def test_writes_json_lines(tmp_path: Path) -> None:
    log = RunnerLog(path=tmp_path / "runner.log")
    log.info("frame_sent", bytes=1024)
    lines = (tmp_path / "runner.log").read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["level"] == "info"
    assert record["event"] == "frame_sent"
    assert record["bytes"] == 1024
    assert "ts" in record


def test_rotation_creates_backups_and_bounds_the_file_count(tmp_path: Path) -> None:
    log = RunnerLog(path=tmp_path / "runner.log", max_bytes=1000, backup_count=2)
    for i in range(300):
        log.info("evt", i=i)
    files = sorted(tmp_path.glob("runner.log*"))
    # Current file plus up to two backups.
    assert len(files) <= 3
    assert (tmp_path / "runner.log.1").exists()


def test_ring_keeps_only_the_last_200_events(tmp_path: Path) -> None:
    log = RunnerLog(path=tmp_path / "runner.log", ring_size=200)
    for i in range(250):
        log.info("evt", i=i)
    recent = log.recent()
    assert len(recent) == 200
    assert recent[0]["i"] == 50
    assert recent[-1]["i"] == 249


def test_recent_limit_returns_the_newest_events(tmp_path: Path) -> None:
    log = RunnerLog(path=tmp_path / "runner.log")
    for i in range(10):
        log.info("evt", i=i)
    recent = log.recent(limit=3)
    assert [event["i"] for event in recent] == [7, 8, 9]


def test_secrets_never_appear_in_the_file_or_the_ring(tmp_path: Path) -> None:
    log = RunnerLog(path=tmp_path / "runner.log")
    log.register_secret("hunter2")
    log.info("auth_failed", token="hunter2", note="ok")
    text = (tmp_path / "runner.log").read_text()
    assert "hunter2" not in text
    assert "***" in text
    ring = json.dumps(log.recent())
    assert "hunter2" not in ring
    assert "ok" in ring


def test_secret_inside_a_formatted_argument_is_redacted(tmp_path: Path) -> None:
    log = RunnerLog(path=tmp_path / "runner.log")
    log.register_secret("s3cr3t")
    log.warning("header", value="Bearer s3cr3t")
    assert "s3cr3t" not in (tmp_path / "runner.log").read_text()


def test_register_secret_ignores_empty_values(tmp_path: Path) -> None:
    log = RunnerLog(path=tmp_path / "runner.log")
    log.register_secret("")
    log.info("plain", value="")
    assert "***" not in (tmp_path / "runner.log").read_text()
