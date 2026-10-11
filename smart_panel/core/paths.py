"""Locations of the home, runtime and spool directories (T016).

Two directories matter:

* **home** — the account's own config dir (``$SMART_PANEL_HOME`` or
  ``~/.config/smart-panel``), created mode 0700. Holds ``config.json``,
  ``secrets.json``, ``state.db``, ``state/``, ``plugins/`` and the log.
* **runtime** — a single shared path for *every* account
  (``$SMART_PANEL_RUNTIME`` else ``/tmp/smart-panel``), so an allowed
  non-owner account can find the control socket. Only the runner and
  ``service install`` create it; the CLI never does, and the runner refuses to
  start against a directory owned by another uid (``research.md`` decision 6).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

DEFAULT_RUNTIME = Path("/tmp/smart-panel")
HOME_MODE = 0o700
RUNTIME_MODE = 0o755
SPOOL_MODE = 0o1733  # sticky, world-writable within a 0755 runtime dir


class RuntimeOwnershipError(RuntimeError):
    """The runtime directory exists but is owned by another uid."""


def home() -> Path:
    """Return the home directory, creating it mode 0700 if missing.

    Respects ``$SMART_PANEL_HOME``; otherwise ``~/.config/smart-panel``. An
    existing directory is returned untouched (its mode is not forced).
    """
    override = os.environ.get("SMART_PANEL_HOME")
    path = Path(override) if override else Path.home() / ".config" / "smart-panel"
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, HOME_MODE)
    return path


def runtime() -> Path:
    """Return the runtime directory path *without* creating it.

    The same path for every account (``$SMART_PANEL_RUNTIME`` else
    ``/tmp/smart-panel``); nothing on the CLI path may create it.
    """
    override = os.environ.get("SMART_PANEL_RUNTIME")
    return Path(override) if override else DEFAULT_RUNTIME


def ensure_runtime(uid: int | None = None) -> Path:
    """Create the runtime directory (mode 0755) or verify its owner.

    Args:
        uid: Expected owner; defaults to the caller's effective uid.

    Raises:
        RuntimeOwnershipError: The directory exists and is owned by another uid.
    """
    path = runtime()
    expected = os.geteuid() if uid is None else uid
    if path.exists():
        if not path.is_dir():
            raise RuntimeOwnershipError(f"runtime path {path} exists but is not a directory")
        owner = path.stat().st_uid
        if owner != expected:
            raise RuntimeOwnershipError(
                f"runtime directory {path} is owned by uid {owner}, expected {expected}"
            )
        return path
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, RUNTIME_MODE)
    return path


def spool(override: str | os.PathLike[str] | None = None) -> Path:
    """Return the spool directory path without creating it.

    Defaults to ``<runtime>/spool``; ``paths.spool`` in the config overrides it
    (a disk-backed spool for installs where /tmp is volatile).
    """
    if override is not None:
        return Path(override)
    return runtime() / "spool"


def ensure_spool(override: str | os.PathLike[str] | None = None) -> Path:
    """Create the spool directory (sticky mode 1733) if missing."""
    path = spool(override)
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, SPOOL_MODE)
    return path


def config_file() -> Path:
    return home() / "config.json"


def secrets_file() -> Path:
    return home() / "secrets.json"


def state_db() -> Path:
    return home() / "state.db"


def state_dir() -> Path:
    return home() / "state"


def plugins_dir() -> Path:
    return home() / "plugins"


def pending_spool() -> Path:
    """The owner's fallback spool, used when the runtime directory is absent."""
    return home() / "spool-pending"


def log_file() -> Path:
    return home() / "runner.log"


def control_sock() -> Path:
    return runtime() / "control.sock"


def runner_lock() -> Path:
    return runtime() / "runner.lock"


def runner_pid() -> Path:
    return runtime() / "runner.pid"


def mode_of(path: str | os.PathLike[str]) -> int:
    """Return the permission bits (including setuid/setgid/sticky) of ``path``."""
    return stat.S_IMODE(os.stat(path).st_mode)
