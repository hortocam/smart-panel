"""Unit tests for ``smart_panel.core.secrets`` (T023).

Secrets live in ``<home>/secrets.json`` stored mode 0600 and are referenced from
config as ``${secret:NAME}``. Resolving a secret registers the value with the
logging redaction filter so it can never appear in a log line or ``status``
(FR-049).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from smart_panel.core import paths
from smart_panel.core.logging import RedactionFilter
from smart_panel.core.secrets import SecretError, SecretNotFound, Secrets


def test_set_writes_the_file_mode_0600(tmp_home: Path) -> None:
    secrets = Secrets()
    secrets.set("TOKEN", "abc")
    mode = stat.S_IMODE(os.stat(paths.secrets_file()).st_mode)
    assert mode == 0o600


def test_an_existing_file_is_repaired_to_mode_0600(tmp_home: Path) -> None:
    paths.secrets_file().write_text("{}")
    os.chmod(paths.secrets_file(), 0o644)
    Secrets()
    mode = stat.S_IMODE(os.stat(paths.secrets_file()).st_mode)
    assert mode == 0o600


def test_resolution_substitutes_references(tmp_home: Path) -> None:
    secrets = Secrets()
    secrets.set("TOKEN", "abc")
    assert secrets.resolve("${secret:TOKEN}") == "abc"
    assert secrets.resolve("Bearer ${secret:TOKEN}") == "Bearer abc"


def test_resolving_a_missing_name_raises(tmp_home: Path) -> None:
    secrets = Secrets()
    with pytest.raises(SecretNotFound):
        secrets.resolve("${secret:NOPE}")


def test_list_and_delete(tmp_home: Path) -> None:
    secrets = Secrets()
    secrets.set("A", "1")
    secrets.set("B", "2")
    assert secrets.list_names() == ["A", "B"]
    secrets.delete("A")
    assert secrets.list_names() == ["B"]
    with pytest.raises(SecretNotFound):
        secrets.delete("A")


def test_get_persists_across_reopen(tmp_home: Path) -> None:
    Secrets().set("A", "1")
    assert Secrets().get("A") == "1"


def test_invalid_names_are_rejected(tmp_home: Path) -> None:
    secrets = Secrets()
    with pytest.raises(SecretError):
        secrets.set("bad name", "1")


def test_set_registers_the_value_with_the_redactor(tmp_home: Path) -> None:
    redactor = RedactionFilter()
    secrets = Secrets(redactor=redactor)
    secrets.set("TOKEN", "hunter2")
    assert redactor.redact("token=hunter2") == "token=***"


def test_resolve_registers_the_value_with_the_redactor(tmp_home: Path) -> None:
    Secrets().set("TOKEN", "s3cr3t")
    redactor = RedactionFilter()
    reloaded = Secrets(redactor=redactor)
    assert reloaded.resolve("${secret:TOKEN}") == "s3cr3t"
    assert redactor.redact("header=s3cr3t") == "header=***"
