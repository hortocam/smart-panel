"""Unit regressions for the control command handlers (T052).

F3 — a validation failure must surface as ``invalid_input`` (CLI exit 2), not
``internal`` (exit 1). ``SettingError`` and ``SettingsRejected`` were already
mapped, but ``LayoutError`` and ``SecretError`` are plain ``ValueError``s and
fell through to the catch-all: an invalid layout or secret name was reported as
an internal error instead of a usage error.

F4 — ``logs.tail --follow`` must keep streaming once the log ring is full. The
ring is a fixed-size ``deque``, so its length stops growing at capacity; a
follow loop that indexes new events by the *length* of the ring silently drops
every event from then on. The stream tracks the log's monotonic sequence
number instead.
"""

from __future__ import annotations

import copy

import pytest

from smart_panel.core import commands
from smart_panel.core.commands import _logs_tail, make_dispatch
from smart_panel.core.config import validate_config
from smart_panel.core.control import protocol
from smart_panel.core.logging import RunnerLog
from smart_panel.core.secrets import SecretError


class _NullLog:
    """A logger that discards everything."""

    def debug(self, event: str, **fields: object) -> None:
        pass

    info = debug
    warning = debug
    error = debug


class _StubSecrets:
    """A secrets store whose ``set`` validates the name like the real one."""

    def set(self, name: str, value: object) -> None:
        raise SecretError(f"secret name {name!r} must match [A-Za-z0-9_.-]+")

    def list_names(self) -> list[str]:
        return []


class _StubRunner:
    """A duck-typed runner with real config validation and no threads or socket."""

    def __init__(self, log: object | None = None) -> None:
        self._config = validate_config(
            {
                "display": {"fps": 15, "brightness": 50, "sink": "null", "min_text_px": 24},
                "layout": {"regions": [], "overlays": []},
                "plugins": {},
            }
        )
        self.log = log if log is not None else _NullLog()
        self.stopping = False
        self.secrets = _StubSecrets()

    @property
    def config_data(self) -> dict:
        return copy.deepcopy(self._config)

    def set_config(self, dotted: str, value: object) -> dict:
        candidate = copy.deepcopy(self._config)
        node = candidate
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        # Real validation, so a bad layout raises the real LayoutError.
        self._config = validate_config(candidate)
        return {"path": dotted, "value": value, "applied": True}


class _FollowRunner:
    """A runner stub whose ``stopping`` flips after a bounded number of polls.

    A correct stream yields the new event on the first poll; a broken one that
    never yields would otherwise spin forever, so the budget makes it end.
    """

    def __init__(self, log: RunnerLog, poll_budget: int = 6) -> None:
        self.log = log
        self._remaining = poll_budget

    @property
    def stopping(self) -> bool:
        self._remaining -= 1
        return self._remaining < 0


# ------------------------------------------------------------------ F3: mapping


def test_an_undersized_region_is_reported_as_invalid_input() -> None:
    dispatch = make_dispatch(_StubRunner())
    with pytest.raises(protocol.ProtocolError) as excinfo:
        dispatch(
            protocol.Request(
                "layout.region.set",
                {"name": "main", "x": 0, "y": 0, "w": 10, "h": 10, "instance": "text"},
            )
        )
    assert excinfo.value.code == "invalid_input"
    assert "main" in excinfo.value.message


def test_an_overlay_on_a_missing_region_is_invalid_input() -> None:
    dispatch = make_dispatch(_StubRunner())
    with pytest.raises(protocol.ProtocolError) as excinfo:
        dispatch(protocol.Request("layout.overlay.add", {"region": "ghost", "instance": "text"}))
    assert excinfo.value.code == "invalid_input"


def test_an_invalid_secret_name_is_invalid_input() -> None:
    dispatch = make_dispatch(_StubRunner())
    with pytest.raises(protocol.ProtocolError) as excinfo:
        dispatch(protocol.Request("secret.set", {"name": "bad name", "value": "x"}))
    assert excinfo.value.code == "invalid_input"


# --------------------------------------------------------------- F4: logs.tail


def test_logs_tail_follow_streams_new_events_once_the_ring_is_full(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(commands.time, "sleep", lambda _seconds: None)

    log = RunnerLog(path=tmp_path / "runner.log", ring_size=4)
    for index in range(4):
        log.info("filler", n=index)
    runner = _FollowRunner(log)

    stream = _logs_tail(runner, {"follow": True, "lines": 1})
    assert next(stream)["n"] == 3  # the one-line backlog

    log.info("fresh", n=99)  # the ring is now full; its length will not grow
    received = False
    for _ in range(20):
        try:
            event = next(stream)
        except StopIteration:
            break
        if event.get("log_event") == "fresh":
            received = True
            break
    assert received, "a new log event was never streamed once the ring was full"
