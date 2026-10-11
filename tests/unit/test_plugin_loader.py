"""Unit tests for the plugin loader ``smart_panel.core.plugins`` (T032).

Discovery covers the ``smart_panel.plugins`` entry-point group and the drop-in
directory ``<home>/plugins/`` (``*.py`` files and package directories). Names
must match ``[a-z][a-z0-9_-]{1,31}`` and be unique, a drop-in may not shadow a
built-in, an import error is caught and reported as ``load_failed``, a plugin
targeting a newer SDK major is ``incompatible``, and ``plugins_rev`` increments
when the discovered set changes.
"""

from __future__ import annotations

from pathlib import Path

from smart_panel.core.plugins import LoadState, PluginLoader
from smart_panel.sdk.plugin import Manifest, Plugin

VALID = (
    "from smart_panel.sdk.plugin import Plugin, Manifest\n"
    "class Demo(Plugin):\n"
    '    manifest = Manifest(name="demo", version="1.0.0", sdk_version="1", summary="demo")\n'
)


class BuiltinDemo(Plugin):
    manifest = Manifest(name="demo", summary="built-in demo")


class EntryPlugin(Plugin):
    manifest = Manifest(name="epdemo", summary="entry point demo")


def _write(plugin_dir: Path, filename: str, text: str) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / filename).write_text(text)


def test_discovers_a_valid_dropin(tmp_path: Path) -> None:
    home = tmp_path / "plugins"
    _write(home, "demo.py", VALID)
    loader = PluginLoader(home=home, builtins={}, entry_points=[])
    records = loader.scan()
    record = next(r for r in records if r.name == "demo")
    assert record.state == LoadState.LOADED
    assert record.source == "dropin"
    assert "demo" in loader.loaded()


def test_discovers_a_package_dropin(tmp_path: Path) -> None:
    package = tmp_path / "plugins" / "mypkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(VALID.replace('"demo"', '"mypkg"'))
    loader = PluginLoader(home=tmp_path / "plugins", builtins={}, entry_points=[])
    assert "mypkg" in loader.loaded()


def test_broken_import_is_reported_load_failed(tmp_path: Path) -> None:
    home = tmp_path / "plugins"
    _write(home, "broken.py", "import definitely_not_a_real_module_xyz  # noqa: F401\n")
    loader = PluginLoader(home=home, builtins={}, entry_points=[])
    record = next(r for r in loader.scan() if r.source == "dropin")
    assert record.state == LoadState.LOAD_FAILED
    assert "definitely_not_a_real_module_xyz" in (record.error or "")


def test_a_dropin_may_not_shadow_a_builtin(tmp_path: Path) -> None:
    home = tmp_path / "plugins"
    _write(home, "demo.py", VALID)
    loader = PluginLoader(home=home, builtins={"demo": BuiltinDemo}, entry_points=[])
    record = next(r for r in loader.scan() if r.source == "dropin")
    assert record.state == LoadState.LOAD_FAILED
    assert "demo" in (record.error or "")


def test_a_bad_name_is_rejected(tmp_path: Path) -> None:
    home = tmp_path / "plugins"
    _write(home, "badname.py", VALID.replace('"demo"', '"Bad Name"'))
    loader = PluginLoader(home=home, builtins={}, entry_points=[])
    record = next(r for r in loader.scan() if r.source == "dropin")
    assert record.state == LoadState.LOAD_FAILED


def test_a_newer_sdk_major_is_incompatible(tmp_path: Path) -> None:
    home = tmp_path / "plugins"
    _write(home, "future.py", VALID.replace('"demo"', '"future"').replace('"1"', '"2"'))
    loader = PluginLoader(home=home, builtins={}, entry_points=[])
    record = next(r for r in loader.scan() if r.name == "future")
    assert record.state == LoadState.INCOMPATIBLE


def test_entry_points_are_discovered(tmp_path: Path) -> None:
    class FakeEntryPoint:
        name = "epdemo"

        def load(self) -> type:
            return EntryPlugin

    loader = PluginLoader(
        home=tmp_path / "plugins", builtins={}, entry_points=[FakeEntryPoint()]
    )
    records = loader.scan()
    assert any(r.name == "epdemo" and r.state == LoadState.LOADED for r in records)


def test_plugins_rev_increments_when_the_set_changes(tmp_path: Path) -> None:
    home = tmp_path / "plugins"
    loader = PluginLoader(home=home, builtins={}, entry_points=[])
    loader.scan()
    rev = loader.plugins_rev
    loader.scan()
    assert loader.plugins_rev == rev
    _write(home, "demo.py", VALID)
    loader.scan()
    assert loader.plugins_rev == rev + 1
