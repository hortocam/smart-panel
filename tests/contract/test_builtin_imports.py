"""Contract test: built-in plugins import only ``smart_panel.sdk`` (T033, FR-043).

An AST scan of every module under ``smart_panel/plugins/`` fails if a plugin
imports ``smart_panel.core`` (or any other ``smart_panel`` submodule) or a
third-party module outside the allowed set. This is what keeps the built-ins
honest examples of the public SDK and is enforced in CI.

The scan is exercised against a synthetic offending module as a negative
control, so a scanner that silently stopped detecting violations would fail
this test rather than pass it.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_DIR = REPO_ROOT / "smart_panel" / "plugins"

ALLOWED_THIRD_PARTY = frozenset({"PIL", "psutil", "defusedxml"})


def _module_name(path: Path) -> str:
    rel = path.resolve().relative_to(REPO_ROOT)
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def _package_name(path: Path) -> str:
    module = _module_name(path)
    if path.name == "__init__.py":
        return module
    return module.rpartition(".")[0]


def _resolve_relative(level: int, module: str | None, package: str) -> str:
    parts = package.split(".") if package else []
    # level 1 means the importing package itself; each extra level walks up one.
    drop = level - 1
    if drop:
        parts = parts[: len(parts) - drop] if drop <= len(parts) else []
    if module:
        parts = [*parts, *module.split(".")]
    return ".".join(part for part in parts if part)


def _is_allowed(absolute: str, own_root: str) -> bool:
    """A plugin may import the SDK, its own package, stdlib, and the app extras."""
    root = absolute.split(".", 1)[0]
    if root == "smart_panel":
        if absolute == "smart_panel.sdk" or absolute.startswith("smart_panel.sdk."):
            return True
        # A plugin's own package (its submodules) is its private code.
        return absolute == own_root or absolute.startswith(own_root + ".")
    if root in ALLOWED_THIRD_PARTY:
        return True
    return root in sys.stdlib_module_names


def _violations(source: str, package: str, own_root: str | None = None) -> list[str]:
    root = own_root if own_root is not None else package
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names = [_resolve_relative(node.level, node.module, package)]
            else:
                names = [node.module or ""]
        for name in names:
            if not name:
                continue
            if not _is_allowed(name, root):
                offenders.append(name)
    return offenders


def test_plugin_sources_exist_to_scan() -> None:
    assert PLUGINS_DIR.is_dir()
    assert list(PLUGINS_DIR.rglob("*.py")), "expected at least one plugin module to scan"


def test_builtin_plugins_import_only_the_sdk() -> None:
    failures: list[str] = []
    for path in sorted(PLUGINS_DIR.rglob("*.py")):
        offenders = _violations(path.read_text(), _package_name(path))
        if offenders:
            failures.append(f"{path.relative_to(REPO_ROOT)}: {', '.join(sorted(set(offenders)))}")
    if failures:
        raise AssertionError(
            "built-in plugins must import only smart_panel.sdk:\n" + "\n".join(failures)
        )


def test_scanner_flags_a_core_import_negative_control() -> None:
    source = "from smart_panel.core.bus import Bus\nimport os\n"
    assert _violations(source, "smart_panel.plugins.example") == ["smart_panel.core.bus"]


def test_scanner_flags_a_relative_import_that_escapes_to_core() -> None:
    # From smart_panel.plugins.text, ``...core`` walks up to smart_panel.core.
    source = "from ...core import bus\n"
    assert _violations(source, "smart_panel.plugins.text") == ["smart_panel.core"]


def test_scanner_flags_an_import_of_a_sibling_plugin() -> None:
    source = "from smart_panel.plugins.alerts import model\n"
    assert _violations(source, "smart_panel.plugins.text") == ["smart_panel.plugins.alerts"]


def test_scanner_allows_the_sdk_and_the_permitted_third_party_modules() -> None:
    source = (
        "import os\n"
        "from smart_panel.sdk.plugin import Plugin\n"
        "from PIL import Image\n"
        "from .model import Thing\n"
    )
    assert _violations(source, "smart_panel.plugins.text") == []


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
