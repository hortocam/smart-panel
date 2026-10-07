"""TDD guard for the Phase 1 packaging fix (T001) and package skeleton (T002).

These assertions target the exact defects T001 names, so each one fails on the
pre-fix ``pyproject.toml`` for its own reason rather than because a module is
merely absent. T005-T007 add the deeper protocol/device suites under
``tests/unit/``; this file only pins the packaging contract that the rest of
Phase 1 installs on top of.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def test_build_backend_is_the_importable_setuptools_backend():
    # The pre-fix value is "setuptools.backends._legacy:_Backend", which pip
    # cannot import; the only backend this project can build with is this one.
    backend = _pyproject()["build-system"]["build-backend"]
    assert backend == "setuptools.build_meta"


def test_requires_python_matches_the_supported_versions():
    assert _pyproject()["project"]["requires-python"] == ">=3.11"


def test_package_list_includes_both_distribution_packages():
    include = _pyproject()["tool"]["setuptools"]["packages"]["find"]["include"]
    assert "panel_driver*" in include
    assert "smart_panel*" in include


def test_smart_panel_assets_are_declared_as_package_data():
    data = _pyproject()["tool"]["setuptools"]["package-data"]
    assert data["smart_panel"] == ["assets/**/*"]


def test_base_dependencies_stay_minimal_for_the_transport():
    # Constitution III: panel_driver depends only on hid and Pillow.
    base = _pyproject()["project"]["dependencies"]
    assert base == ["hid>=1.0", "Pillow>=10"]


def test_app_extra_confines_the_new_runtime_dependencies():
    app = _pyproject()["project"]["optional-dependencies"]["app"]
    assert set(app) == {"psutil", "defusedxml"}


def test_dev_extra_pulls_in_everything_the_suite_needs():
    dev = _pyproject()["project"]["optional-dependencies"]["dev"]
    for dep in ("pytest", "ruff", "psutil", "defusedxml"):
        assert any(d.lower().startswith(dep) for d in dev), f"dev extra misses {dep}"


def test_script_entry_point_targets_the_smart_panel_cli():
    scripts = _pyproject()["project"]["scripts"]
    assert scripts == {"smart-panel": "smart_panel.cli.main:main"}


def test_pytest_hardware_marker_registered_and_skipped_by_default():
    opts = _pyproject()["tool"]["pytest"]["ini_options"]
    assert any(m.startswith("hardware:") for m in opts["markers"])
    assert opts["addopts"] == "-m 'not hardware'"


@pytest.mark.parametrize(
    "dotted",
    [
        "smart_panel",
        "smart_panel.core",
        "smart_panel.sdk",
        "smart_panel.plugins",
        "smart_panel.cli",
        "smart_panel.cli.commands",
    ],
)
def test_skeleton_packages_are_importable(dotted):
    importlib.import_module(dotted)


def test_smart_panel_reports_a_version():
    import smart_panel

    assert smart_panel.__version__ == "0.1.0"


def test_asset_directories_exist():
    assert (ROOT / "smart_panel" / "assets" / "fonts").is_dir()
    assert (ROOT / "smart_panel" / "assets" / "icons").is_dir()
