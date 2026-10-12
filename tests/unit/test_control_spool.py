"""Unit tests for the spool writer, the client fallback and the ingest (T048, T049).

The spool is the contract's answer to "the runner is down" (``control-protocol.md``
section "Spool"). Three pieces are pinned here:

* the on-disk entry (``<received_at_ns>-<uuid>.json``, mode 0644, written as
  ``.tmp`` then renamed, ``{v, received_at, cmd, args}``) and the 1,000-file cap;
* the CLI-side fallback: spoolable verbs only, ``<runtime>/spool`` when it
  exists, the owner's ``<home>/spool-pending`` when it does not, and exit 3 for
  any other account;
* the runner-side ingest: name order, owner check against the access policy,
  rejection of malformed or oversized files, deletion of everything processed.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from smart_panel.core import paths
from smart_panel.core.control import client, spool
from smart_panel.core.control.access import Policy
from smart_panel.core.control.protocol import Request
from smart_panel.core.control.spool import SpoolEntry, SpoolIngest

OUR_UID = os.geteuid()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


class _FakeLog:
    """Records the runner log events the ingest emits."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def debug(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))

    def info(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))

    def warning(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))

    def error(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    """A short temp base: AF_UNIX paths are capped near 108 bytes."""
    for candidate in ("/tmp", tempfile.gettempdir()):
        if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            base = Path(tempfile.mkdtemp(prefix="sp-", dir=candidate))
            break
    else:
        raise RuntimeError("no writable temporary directory for the socket tests")
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, short_tmp: Path) -> Path:
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(short_tmp / "runtime"))
    monkeypatch.setenv("SMART_PANEL_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SMART_PANEL_RUNNER_UID", raising=False)
    return short_tmp / "runtime"


class TestSpoolFiles:
    """The on-disk entry format and its cap."""

    def test_entry_name_encodes_the_receive_time_in_order(self, tmp_path: Path) -> None:
        first = spool.write_entry(
            tmp_path, "text.set", {"text": "a"}, received_at=1_791_412_345.0002
        )
        second = spool.write_entry(
            tmp_path, "text.set", {"text": "b"}, received_at=1_791_412_345.0001
        )
        assert first.suffix == ".json"
        assert first.name.endswith(".json")
        stem_ns, _, tail = first.stem.partition("-")
        assert stem_ns.isdigit()
        assert tail  # a uuid
        # Names sort in receive-time order, not write order.
        assert spool.list_spool_files(tmp_path) == [second, first]

    def test_entry_is_written_as_tmp_then_renamed_with_mode_0644(self, tmp_path: Path) -> None:
        path = spool.write_entry(
            tmp_path,
            "plugin.call",
            {"instance": "alerts", "verb": "push", "args": {"message": "hi"}},
            received_at=1_791_412_345.123,
        )
        assert list(tmp_path.glob("*.tmp")) == []
        assert (os.stat(path).st_mode & 0o777) == 0o644
        assert _read_json(path) == {
            "v": 1,
            "received_at": 1_791_412_345.123,
            "cmd": "plugin.call",
            "args": {"instance": "alerts", "verb": "push", "args": {"message": "hi"}},
        }

    def test_entry_reads_back_into_a_request(self, tmp_path: Path) -> None:
        path = spool.write_entry(tmp_path, "text.set", {"text": "hi"}, received_at=5.0)
        entry = spool.read_entry(path)
        assert isinstance(entry, SpoolEntry)
        assert entry.received_at == 5.0
        assert entry.cmd == "text.set"
        assert isinstance(entry.request, Request)
        assert entry.request.cmd == "text.set"
        assert entry.request.args == {"text": "hi"}

    def test_write_refuses_at_the_1000_file_cap(self, tmp_path: Path) -> None:
        for index in range(spool.MAX_FILES):
            (tmp_path / f"{index:020d}-00000000000000000000000000000000.json").write_text("{}")
        with pytest.raises(spool.SpoolFull):
            spool.write_entry(tmp_path, "text.set", {}, received_at=1.0)

    def test_read_rejects_malformed_and_oversized_files(self, tmp_path: Path) -> None:
        malformed = tmp_path / "0001-x.json"
        malformed.write_text("{not json}")
        with pytest.raises(spool.SpoolMalformed):
            spool.read_entry(malformed)

        wrong_version = tmp_path / "0002-x.json"
        wrong_version.write_text(json.dumps({"v": 2, "received_at": 1.0, "cmd": "x", "args": {}}))
        with pytest.raises(spool.SpoolMalformed):
            spool.read_entry(wrong_version)

        oversize = tmp_path / "0003-x.json"
        oversize.write_text("x" * (spool.MAX_FILE_BYTES + 1))
        with pytest.raises(spool.SpoolMalformed):
            spool.read_entry(oversize)

    def test_list_ignores_partial_tmp_files(self, tmp_path: Path) -> None:
        (tmp_path / "0001-in-flight.json.tmp").write_text("{}")
        real = spool.write_entry(tmp_path, "text.set", {}, received_at=2.0)
        assert spool.list_spool_files(tmp_path) == [real]


class TestClientSpoolFallback:
    """``ControlClient.call`` when the runner is not there."""

    def test_a_spoolable_verb_is_saved_for_later(self, runtime: Path) -> None:
        (runtime / "spool").mkdir(parents=True)
        with client.ControlClient(timeout=1) as ctl:
            response = ctl.call("text.set", {"text": "hi"}, spoolable=True)
        assert response.ok is True
        assert response.data is not None and response.data["saved_for_later"] is True
        assert isinstance(response.data["spool_id"], str)
        saved = list((runtime / "spool").glob("*.json"))
        assert len(saved) == 1
        assert _read_json(saved[0])["cmd"] == "text.set"

    def test_a_non_spoolable_verb_is_runner_not_running(self, runtime: Path) -> None:
        (runtime / "spool").mkdir(parents=True)
        with pytest.raises(client.RunnerUnavailable) as excinfo:
            with client.ControlClient(timeout=1) as ctl:
                ctl.call("runner.status")
        assert excinfo.value.code == "runner_not_running"

    def test_the_owner_without_a_runtime_directory_spools_to_spool_pending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, short_tmp: Path
    ) -> None:
        home = tmp_path / "home"
        missing_runtime = short_tmp / "missing-runtime"
        monkeypatch.setenv("SMART_PANEL_HOME", str(home))
        monkeypatch.setenv("SMART_PANEL_RUNTIME", str(missing_runtime))
        monkeypatch.delenv("SMART_PANEL_RUNNER_UID", raising=False)

        with client.ControlClient(timeout=1) as ctl:
            response = ctl.call("text.set", {"text": "later"}, spoolable=True)

        assert response.data is not None and response.data["saved_for_later"] is True
        # The CLI must never create the runtime directory ...
        assert not missing_runtime.exists()
        # ... the owner's push waits in <home>/spool-pending instead.
        pending = home / "spool-pending"
        assert len(list(pending.glob("*.json"))) == 1

    def test_a_non_owner_without_a_runtime_directory_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, short_tmp: Path
    ) -> None:
        missing_runtime = short_tmp / "missing-runtime"
        monkeypatch.setenv("SMART_PANEL_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("SMART_PANEL_RUNTIME", str(missing_runtime))
        monkeypatch.setenv("SMART_PANEL_RUNNER_UID", str(OUR_UID + 1_000_000))
        with pytest.raises(client.SpoolRefused) as excinfo:
            with client.ControlClient(timeout=1) as ctl:
                ctl.call("text.set", {"text": "later"}, spoolable=True)
        assert excinfo.value.code == "runner_not_running"
        assert not missing_runtime.exists()

    def test_a_full_spool_is_spool_full(self, runtime: Path) -> None:
        directory = runtime / "spool"
        directory.mkdir(parents=True)
        for index in range(spool.MAX_FILES):
            (directory / f"{index:020d}-00000000000000000000000000000000.json").write_text("{}")
        with pytest.raises(client.SpoolFull) as excinfo:
            with client.ControlClient(timeout=1) as ctl:
                ctl.call("text.set", {"text": "hi"}, spoolable=True)
        assert excinfo.value.code == "spool_full"


class TestSpoolIngest:
    """The runner's replay of the spool on start."""

    def _ingest(self, dispatch, **kwargs) -> tuple[SpoolIngest, _FakeLog]:
        log = _FakeLog()
        policy = kwargs.pop("policy", Policy(runner_uid=OUR_UID))
        ingest = SpoolIngest(
            dispatch,
            policy_provider=lambda: policy,
            log=log,
            **kwargs,
        )
        return ingest, log

    def test_ingest_replays_in_receive_time_order_and_deletes(
        self, runtime: Path, tmp_path: Path
    ) -> None:
        directory = runtime / "spool"
        directory.mkdir(parents=True)
        spool.write_entry(directory, "text.set", {"text": "third"}, received_at=300.0)
        spool.write_entry(directory, "text.set", {"text": "first"}, received_at=100.0)
        spool.write_entry(directory, "text.set", {"text": "second"}, received_at=200.0)

        seen: list[str] = []
        ingest, _ = self._ingest(
            lambda request, received_at: seen.append(request.args["text"])
        )
        result = ingest.ingest()

        assert seen == ["first", "second", "third"]
        assert result.replayed == 3
        assert ingest.pending_count() == 0
        assert not list(directory.glob("*.json"))

    def test_ingest_replays_received_at_and_the_request(
        self, runtime: Path, tmp_path: Path
    ) -> None:
        directory = runtime / "spool"
        directory.mkdir(parents=True)
        spool.write_entry(directory, "text.set", {"text": "a"}, received_at=100.0)
        spool.write_entry(directory, "text.set", {"text": "b"}, received_at=200.0)

        seen: list[tuple[dict, float]] = []
        ingest, _ = self._ingest(
            lambda request, received_at: seen.append((request.args, received_at))
        )
        result = ingest.ingest()

        assert result.replayed == 2
        assert result.rejected == 0
        assert seen == [({"text": "a"}, 100.0), ({"text": "b"}, 200.0)]

    def test_ingest_rejects_a_disallowed_owner_and_deletes(
        self, runtime: Path, tmp_path: Path
    ) -> None:
        directory = runtime / "spool"
        directory.mkdir(parents=True)
        spool.write_entry(directory, "text.set", {}, received_at=1.0)

        replayed: list[float] = []
        ingest, log = self._ingest(
            lambda request, received_at: replayed.append(received_at),
            policy=Policy(runner_uid=0xDEAD, allowed_uids=frozenset()),
        )
        result = ingest.ingest()

        assert replayed == []
        assert result.rejected == 1
        assert not list(directory.glob("*.json"))
        assert any(event == "spool_rejected" for event, _ in log.events)

    def test_ingest_rejects_malformed_and_oversized_files(
        self, runtime: Path, tmp_path: Path
    ) -> None:
        directory = runtime / "spool"
        directory.mkdir(parents=True)
        (directory / "0001-bad.json").write_text("{not json}")
        (directory / "0002-big.json").write_text("x" * (spool.MAX_FILE_BYTES + 1))
        spool.write_entry(directory, "text.set", {"text": "ok"}, received_at=3.0)

        replayed: list[dict] = []
        ingest, log = self._ingest(
            lambda request, received_at: replayed.append(request.args)
        )
        result = ingest.ingest()

        assert replayed == [{"text": "ok"}]
        assert result.replayed == 1
        assert result.rejected == 2
        assert not list(directory.glob("*.json"))
        assert sum(1 for event, _ in log.events if event == "spool_rejected") == 2

    def test_ingest_also_drains_the_owner_pending_directory(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        pending = home / "spool-pending"
        pending.mkdir()
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        spool.write_entry(pending, "text.set", {"text": "from-home"}, received_at=5.0)

        seen: list[dict] = []
        ingest, _ = self._ingest(
            lambda request, received_at: seen.append(request.args),
            spool_dir=runtime_dir / "spool",
            pending_dir=pending,
        )
        result = ingest.ingest()

        assert seen == [{"text": "from-home"}]
        assert result.replayed == 1
        assert not list(pending.glob("*.json"))

    def test_pending_count_reports_waiting_files(self, runtime: Path) -> None:
        directory = runtime / "spool"
        directory.mkdir(parents=True)
        ingest, _ = self._ingest(lambda request, received_at: None, spool_dir=directory)
        assert ingest.pending_count() == 0
        spool.write_entry(directory, "text.set", {}, received_at=1.0)
        spool.write_entry(directory, "text.set", {}, received_at=2.0)
        assert ingest.pending_count() == 2


def test_paths_spool_default_is_the_runtime_spool(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(tmp_home / "rt"))
    assert paths.spool() == tmp_home / "rt" / "spool"
    assert paths.pending_spool() == tmp_home / "spool-pending"
