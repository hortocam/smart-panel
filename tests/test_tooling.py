"""TDD guard for the Phase 1 tooling contract (T008 ruff config, T009 CI).

These assertions target the exact deliverables T008 and T009 name, so each one
fails on the pre-change tree for its own reason (no ``[tool.ruff]`` table, no
``.github/workflows/ci.yml``) rather than because a file is merely absent.

The CI workflow is checked as text rather than by parsing YAML: ``PyYAML`` is
not a dependency of this project (constitution III: the transport stays minimal
and the app extra carries only ``psutil`` and ``defusedxml``), so the test pins
the observable tokens the task requires without pulling in a new dependency.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"


def _pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def _ci_text() -> str:
    return CI_PATH.read_text(encoding="utf-8")


# --- T008: ruff configuration ---------------------------------------------


def test_ruff_line_length_is_100():
    ruff = _pyproject()["tool"]["ruff"]
    assert ruff["line-length"] == 100


def test_ruff_selects_the_required_rule_families():
    # pycodestyle, pyflakes, isort, pyupgrade, flake8-bugbear.
    lint = _pyproject()["tool"]["ruff"]["lint"]
    assert lint["select"] == ["E", "F", "I", "UP", "B"]


def test_ruff_excludes_the_vendored_spec_kit_tooling():
    # `.specify/extensions/*/scripts/python/*.py` is vendored spec-kit tooling,
    # not project code; without this exclude `ruff check .` reports hundreds of
    # findings in files this repository does not own.
    excluded = _pyproject()["tool"]["ruff"]["extend-exclude"]
    assert ".specify" in excluded


def test_ruff_target_version_matches_requires_python():
    # isort's forced-sort behavior is target-version sensitive; pin it so the
    # import order is stable across ruff versions and local/CI runs.
    ruff = _pyproject()["tool"]["ruff"]
    assert ruff["target-version"] == "py311"


# --- T009: CI workflow -----------------------------------------------------


def test_ci_workflow_exists():
    assert CI_PATH.is_file()


def test_ci_matrix_covers_both_os_and_both_python_versions():
    text = _ci_text()
    for os_name in ("ubuntu-latest", "macos-latest"):
        assert os_name in text, f"CI matrix misses {os_name}"
    for py in ("3.11", "3.13"):
        assert f'"{py}"' in text, f"CI matrix misses Python {py}"


def test_ci_installs_hidapi_per_platform():
    text = _ci_text()
    assert "libhidapi-hidraw0" in text, "Linux hidapi install step missing"
    assert "apt-get" in text, "Linux package manager step missing"
    assert "brew install hidapi" in text, "macOS hidapi install step missing"


def test_ci_installs_the_dev_extra():
    assert re.search(r'pip install -e "\.\[dev\]"', _ci_text()) is not None


def test_ci_runs_ruff_and_the_non_hardware_pytest_selection():
    text = _ci_text()
    assert "ruff check ." in text, "CI does not run ruff"
    assert re.search(r'pytest -m "not hardware"', text) is not None, (
        "CI does not run the non-hardware pytest selection"
    )
