"""The secrets file: ``${secret:NAME}`` values referenced from config (T023).

``secrets.json`` is stored mode 0600 and is the only place a secret value lives
(FR-049). Resolving a reference registers the value with the logging redaction
filter so it can never appear in a log line, ``status`` or an error message.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from smart_panel.core import paths

FILE_MODE = 0o600
_REF = re.compile(r"\$\{secret:([A-Za-z0-9_.-]+)\}")
_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


class SecretError(ValueError):
    """A secret name is invalid."""


class SecretNotFound(KeyError):
    """A referenced secret name is not present in the file."""


class Secrets:
    """The account's secrets file and its ``${secret:NAME}`` resolver.

    Args:
        path: File path; defaults to ``<home>/secrets.json``.
        redactor: Optional logging redaction filter; resolved values are
            registered with it so they are masked everywhere.
    """

    def __init__(self, path: str | Path | None = None, redactor: Any = None) -> None:
        self.path = Path(path) if path is not None else paths.secrets_file()
        self._redactor = redactor
        self._data: dict[str, str] = self._load()

    # ----------------------------------------------------------------- file I/O

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        # Repair a file whose permissions drifted; secrets are owner-only.
        try:
            os.chmod(self.path, FILE_MODE)
        except OSError:
            pass
        raw = self.path.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        data = json.loads(raw)
        return {str(name): str(value) for name, value in data.items()}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        # Create the temp file already 0600 so the value is never briefly readable.
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2, sort_keys=True)
            os.chmod(temp, FILE_MODE)
            os.replace(temp, self.path)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    # --------------------------------------------------------------- operations

    def set(self, name: str, value: Any) -> None:
        """Store ``name`` -> ``value`` and register the value for redaction."""
        self._validate_name(name)
        text = str(value)
        self._data[name] = text
        self._save()
        self._register(text)

    def delete(self, name: str) -> None:
        """Remove ``name``; raise ``SecretNotFound`` when it is absent."""
        if name not in self._data:
            raise SecretNotFound(name)
        del self._data[name]
        self._save()

    def list_names(self) -> list[str]:
        """Sorted secret names (never the values)."""
        return sorted(self._data)

    def get(self, name: str) -> str:
        """Return the raw value for ``name``; raise ``SecretNotFound`` if absent."""
        try:
            value = self._data[name]
        except KeyError as exc:
            raise SecretNotFound(name) from exc
        self._register(value)
        return value

    def resolve(self, text: str) -> str:
        """Substitute every ``${secret:NAME}`` reference in ``text``.

        Raises:
            SecretNotFound: A referenced name is not present.
        """

        def replace(match: re.Match[str]) -> str:
            return self.get(match.group(1))

        return _REF.sub(replace, text)

    def _register(self, value: str) -> None:
        if self._redactor is not None and value:
            self._redactor.register(value)

    @staticmethod
    def _validate_name(name: str) -> None:
        if not isinstance(name, str) or not _NAME.match(name):
            raise SecretError(
                f"secret name {name!r} must match [A-Za-z0-9_.-]+"
            )
