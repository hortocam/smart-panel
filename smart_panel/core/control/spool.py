"""Spool entries: the client-side writer and the runner-side ingest (T049).

A spooled command is one small JSON file, so it survives a runner that is not
running and needs no shared database (``research.md`` decision 7). The format
and the 1,000-file cap are defined once, here, and used by both :mod:`client`
(which writes) and the runner (which replays on start, contract
``control-protocol.md`` section "Spool").

* :func:`write_entry` — ``<received_at_ns>-<uuid>.json``, mode 0644, written as
  a ``.tmp`` and renamed, so the runner never sees a half-written file.
* :func:`list_spool_files` — name order, which is receive-time order because the
  timestamp is zero-padded.
* :class:`SpoolIngest` — the runner's startup drain: owner checked against the
  access policy, malformed or oversized files rejected, each accepted command
  replayed with its ``received_at`` preserved, everything processed deleted.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from smart_panel.core import paths
from smart_panel.core.control import access, peercred
from smart_panel.core.control.protocol import Request

SPOOL_VERSION = 1
MAX_FILE_BYTES = 64 * 1024  # 64 KiB per entry
MAX_FILES = 1000  # per directory (contract: "capped at 1,000 files")
_NAME_WIDTH = 20  # ns since the epoch, zero-padded so names sort by receive time
_FILE_MODE = 0o644


class SpoolError(Exception):
    """A spool operation that failed."""

    code = "internal"


class SpoolFull(SpoolError):
    """The spool directory already holds :data:`MAX_FILES` entries."""

    code = "spool_full"


class SpoolMalformed(SpoolError):
    """An entry that is not a well-formed spool file."""

    code = "invalid_input"


@dataclass(frozen=True)
class SpoolEntry:
    """One accepted spool file."""

    path: Path
    received_at: float
    cmd: str
    args: dict[str, Any]

    @property
    def request(self) -> Request:
        """The control request this entry replays."""
        return Request(cmd=self.cmd, args=self.args)


@dataclass
class IngestResult:
    """What one startup drain did."""

    replayed: int = 0
    rejected: int = 0
    pending: int = 0


def entry_name(received_at: float) -> str:
    """The file name for an entry received at ``received_at``."""
    stamp = int(round(float(received_at) * 1_000_000_000))
    return f"{stamp:0{_NAME_WIDTH}d}-{uuid.uuid4().hex}.json"


def write_entry(
    directory: str | os.PathLike[str],
    cmd: str,
    args: dict[str, Any] | None = None,
    *,
    received_at: float | None = None,
) -> Path:
    """Write one spool entry and return its final path.

    The directory must already exist: the CLI never creates the runtime
    directory, so the caller decides where a push may wait.

    Raises:
        SpoolFull: The directory already holds :data:`MAX_FILES` entries.
        OSError: The directory does not exist or is not writable.
    """
    directory = Path(directory)
    if count_spool_files(directory) >= MAX_FILES:
        raise SpoolFull(f"{directory} already holds {MAX_FILES} spool entries")
    moment = time.time() if received_at is None else float(received_at)
    payload = {
        "v": SPOOL_VERSION,
        "received_at": moment,
        "cmd": cmd,
        "args": dict(args or {}),
    }
    final = directory / entry_name(moment)
    temporary = final.with_name(final.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, separators=(",", ":")))
        os.chmod(temporary, _FILE_MODE)  # umask must not narrow it away
        os.replace(temporary, final)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return final


def read_entry(path: str | os.PathLike[str]) -> SpoolEntry:
    """Parse one spool file.

    Raises:
        SpoolMalformed: The file is oversized, not JSON, or not a version-1 entry.
    """
    path = Path(path)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise SpoolMalformed(f"{path.name} is larger than {MAX_FILE_BYTES} bytes")
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpoolMalformed(f"{path.name} is not a readable spool entry") from exc
    if not isinstance(obj, dict) or obj.get("v") != SPOOL_VERSION:
        raise SpoolMalformed(f"{path.name} is not a version-{SPOOL_VERSION} spool entry")
    received_at = obj.get("received_at")
    if isinstance(received_at, bool) or not isinstance(received_at, (int, float)):
        raise SpoolMalformed(f"{path.name} has no numeric received_at")
    cmd = obj.get("cmd")
    if not isinstance(cmd, str) or not cmd:
        raise SpoolMalformed(f"{path.name} has no command")
    args = obj.get("args", {})
    if not isinstance(args, dict):
        raise SpoolMalformed(f"{path.name} has no argument object")
    return SpoolEntry(path=path, received_at=float(received_at), cmd=cmd, args=args)


def list_spool_files(*directories: str | os.PathLike[str]) -> list[Path]:
    """Every complete ``*.json`` entry across ``directories``, in name order.

    A ``.tmp`` file a client is still writing is not listed, so the runner is
    free to start while a push is in flight.
    """
    found: list[Path] = []
    for directory in directories:
        if directory is None:
            continue
        folder = Path(directory)
        if not folder.is_dir():
            continue
        found.extend(sorted(folder.glob("*.json")))
    return sorted(found, key=lambda path: path.name)


def count_spool_files(*directories: str | os.PathLike[str]) -> int:
    """How many complete entries wait in ``directories``."""
    return len(list_spool_files(*directories))


class SpoolIngest:
    """The runner's startup drain of the spool directories.

    Args:
        replay: Called ``replay(request, received_at)`` for each accepted entry,
            through the same command path a live request takes.
        policy_provider: Returns the current :class:`~.access.Policy`; called per
            entry so a config reload takes effect immediately.
        log: Optional runner log; rejections are recorded at warning level.
        spool_dir: The shared spool; defaults to ``<runtime>/spool``.
        pending_dir: The owner's fallback; defaults to ``<home>/spool-pending``.
    """

    def __init__(
        self,
        replay: Callable[[Request, float], Any],
        *,
        policy_provider: Callable[[], access.Policy],
        log: Any = None,
        spool_dir: str | os.PathLike[str] | None = None,
        pending_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self._replay = replay
        self._policy_provider = policy_provider
        self._log = log
        self._spool_dir = spool_dir
        self._pending_dir = pending_dir

    @property
    def spool_dir(self) -> Path:
        return Path(self._spool_dir) if self._spool_dir is not None else paths.spool()

    @property
    def pending_dir(self) -> Path:
        return (
            Path(self._pending_dir) if self._pending_dir is not None else paths.pending_spool()
        )

    def pending_count(self) -> int:
        """Commands waiting to be replayed, for the ``status`` spool depth."""
        return count_spool_files(self.spool_dir, self.pending_dir)

    def ingest(self) -> IngestResult:
        """Replay every acceptable entry once, in receive-time order."""
        result = IngestResult()
        for path in list_spool_files(self.spool_dir, self.pending_dir):
            try:
                stat = path.stat()
                groups = peercred.get_peer_groups(stat.st_uid, stat.st_gid)
                allowed = access.is_allowed(stat.st_uid, groups, self._policy_provider())
            except OSError:
                self._discard(path, "unreadable")
                result.rejected += 1
                continue
            if not allowed:
                self._discard(path, "owner not allowed", uid=stat.st_uid)
                result.rejected += 1
                continue
            try:
                entry = read_entry(path)
            except SpoolMalformed as exc:
                self._discard(path, str(exc))
                result.rejected += 1
                continue
            try:
                self._replay(entry.request, entry.received_at)
            except Exception as exc:  # noqa: BLE001 - one bad entry must not stall the drain
                self._discard(path, f"replay failed: {exc}", uid=stat.st_uid)
                result.rejected += 1
                continue
            path.unlink(missing_ok=True)
            result.replayed += 1
        result.pending = self.pending_count()
        return result

    def _discard(self, path: Path, reason: str, **fields: Any) -> None:
        if self._log is not None:
            self._log.warning("spool_rejected", path=path.name, reason=reason, **fields)
        path.unlink(missing_ok=True)


def iter_entries(directories: Iterable[str | os.PathLike[str]]) -> list[SpoolEntry]:
    """Parse every entry across ``directories`` in name order."""
    return [read_entry(path) for path in list_spool_files(*directories)]
