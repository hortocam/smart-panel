"""Plugin discovery: entry points and the drop-in directory (T032).

A plugin is a :class:`~smart_panel.sdk.plugin.Plugin` subclass. The loader finds
them two ways (contract ``plugin-sdk.md``, research.md decision 4):

* the ``smart_panel.plugins`` entry-point group for installed packages, plus the
  built-in registry shipped in :mod:`smart_panel.plugins`;
* every ``*.py`` file and package directory under ``<home>/plugins/``.

Plugin names must match ``[a-z][a-z0-9_-]{1,31}`` and be unique; a drop-in may
not shadow a built-in (or another installed plugin). An import error is caught
and reported as ``load_failed`` with the message rather than stopping the runner,
and a plugin targeting a newer SDK *major* is listed ``incompatible``.
``plugins_rev`` increments whenever the discovered set changes, so the CLI can
refresh its cached command tree (T054).
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from smart_panel.core import paths
from smart_panel.sdk.plugin import NAME_PATTERN, SDK_VERSION, Plugin

ENTRY_POINT_GROUP = "smart_panel.plugins"


class LoadState(StrEnum):
    """Outcome of attempting to load one plugin."""

    LOADED = "loaded"
    LOAD_FAILED = "load_failed"
    INCOMPATIBLE = "incompatible"


@dataclass
class PluginRecord:
    """One discovered plugin (loaded or with the reason it failed)."""

    name: str
    state: LoadState
    source: str  # "builtin" | "entry_point" | "dropin"
    plugin_class: type[Plugin] | None = None
    path: str | None = None
    version: str | None = None
    error: str | None = None

    def describe(self) -> dict[str, Any]:
        """A JSON-compatible summary for ``plugin list`` (never the class)."""
        return {
            "name": self.name,
            "state": str(self.state),
            "source": self.source,
            "version": self.version,
            "path": self.path,
            "error": self.error,
        }


@dataclass
class PluginLoader:
    """Discover plugins from the built-in registry, entry points and drop-ins.

    Args:
        home: Drop-in directory; defaults to ``<home>/plugins``.
        builtins: Mapping of name -> :class:`Plugin` subclass to use instead of
            the shipped registry (tests inject this).
        entry_points: Entry points to use instead of discovering them (tests
            inject a small list).
        sdk_version: SDK major the loader targets (defaults to the SDK's).
    """

    home: Path | None = None
    builtins: Mapping[str, type[Plugin]] | None = None
    entry_points: Iterable[Any] | None = None
    sdk_version: str = SDK_VERSION
    _records: list[PluginRecord] = field(default_factory=list, init=False, repr=False)
    _rev: int = field(default=0, init=False, repr=False)
    _signature: tuple[Any, ...] | None = field(default=None, init=False, repr=False)
    _counter: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.home is None:
            self.home = paths.plugins_dir()
        if self.builtins is None:
            self.builtins = _load_builtin_registry()

    # ------------------------------------------------------------------ access

    @property
    def plugins_rev(self) -> int:
        """A counter that increments whenever the discovered set changes."""
        return self._rev

    @property
    def records(self) -> list[PluginRecord]:
        """The records from the most recent :meth:`scan`."""
        self._ensure_scanned()
        return list(self._records)

    def get(self, name: str) -> PluginRecord | None:
        """Return the record for ``name`` from the last scan, or ``None``."""
        self._ensure_scanned()
        for record in self._records:
            if record.name == name:
                return record
        return None

    def loaded(self) -> dict[str, type[Plugin]]:
        """Mapping of name -> plugin class for every successfully loaded plugin."""
        self._ensure_scanned()
        return {
            record.name: record.plugin_class
            for record in self._records
            if record.state == LoadState.LOADED and record.plugin_class is not None
        }

    def _ensure_scanned(self) -> None:
        if self._signature is None:
            self.scan()

    # ------------------------------------------------------------------- scan

    def scan(self) -> list[PluginRecord]:
        """(Re)discover plugins and return the new record list."""
        installed: list[PluginRecord] = []
        for name, plugin_class in sorted(self.builtins.items()):
            installed.append(self._make_record(name, plugin_class, "builtin"))
        for entry_point in self._entry_points():
            installed.append(self._load_entry_point(entry_point))

        reserved = {
            record.name
            for record in installed
            if record.state in (LoadState.LOADED, LoadState.INCOMPATIBLE)
        }
        dropins = self._scan_dropins(reserved)
        records = self._dedupe(installed + dropins)

        self._records = records
        signature = tuple(
            sorted(
                (record.name, str(record.state), record.source, record.path or "")
                for record in records
            )
        )
        if signature != self._signature:
            self._signature = signature
            self._rev += 1
        return list(records)

    # --------------------------------------------------------------- internals

    def _make_record(
        self, name: str, plugin_class: type[Plugin], source: str, path: str | None = None
    ) -> PluginRecord:
        try:
            if not isinstance(name, str) or not NAME_PATTERN.match(name):
                raise ValueError(f"plugin name {name!r} must match [a-z][a-z0-9_-]{{1,31}}")
            manifest = plugin_class.manifest
            manifest.validate()
            if _sdk_major(manifest.sdk_version) > _sdk_major(self.sdk_version):
                return PluginRecord(
                    name=name,
                    state=LoadState.INCOMPATIBLE,
                    source=source,
                    plugin_class=plugin_class,
                    path=path,
                    version=manifest.version,
                    error=f"targets SDK {manifest.sdk_version}, newer than {self.sdk_version}",
                )
            return PluginRecord(
                name=name,
                state=LoadState.LOADED,
                source=source,
                plugin_class=plugin_class,
                path=path,
                version=manifest.version,
            )
        except Exception as exc:  # a bad manifest must not stop the loader
            return PluginRecord(
                name=name if isinstance(name, str) else "<unknown>",
                state=LoadState.LOAD_FAILED,
                source=source,
                path=path,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _entry_points(self) -> list[Any]:
        if self.entry_points is not None:
            return list(self.entry_points)
        from importlib.metadata import entry_points

        discovered = entry_points()
        select = getattr(discovered, "select", None)
        if callable(select):
            return list(select(group=ENTRY_POINT_GROUP))
        return list(discovered.get(ENTRY_POINT_GROUP, []))  # pragma: no cover - old API

    def _load_entry_point(self, entry_point: Any) -> PluginRecord:
        name = getattr(entry_point, "name", "<unknown>")
        try:
            plugin_class = entry_point.load()
        except Exception as exc:
            return PluginRecord(
                name=name,
                state=LoadState.LOAD_FAILED,
                source="entry_point",
                error=f"{type(exc).__name__}: {exc}",
            )
        manifest_name = getattr(getattr(plugin_class, "manifest", None), "name", name)
        return self._make_record(manifest_name, plugin_class, "entry_point")

    def _scan_dropins(self, reserved: set[str]) -> list[PluginRecord]:
        records: list[PluginRecord] = []
        seen: set[str] = set(reserved)
        home = self.home
        assert home is not None
        if not home.is_dir():
            return records
        for entry in sorted(home.iterdir()):
            if entry.name.startswith((".", "_")):
                continue
            if entry.suffix == ".py":
                classes, error = self._exec_module(entry, is_package=False)
                module_path = str(entry)
            elif entry.is_dir() and (entry / "__init__.py").exists():
                classes, error = self._exec_module(entry / "__init__.py", is_package=True)
                module_path = str(entry)
            else:
                continue

            if error is not None:
                records.append(
                    PluginRecord(
                        name=Path(module_path).stem,
                        state=LoadState.LOAD_FAILED,
                        source="dropin",
                        path=module_path,
                        error=error,
                    )
                )
                continue

            for plugin_class in classes:
                name = getattr(getattr(plugin_class, "manifest", None), "name", None)
                if not isinstance(name, str):
                    records.append(
                        PluginRecord(
                            name=Path(module_path).stem,
                            state=LoadState.LOAD_FAILED,
                            source="dropin",
                            path=module_path,
                            error="plugin class declares no manifest name",
                        )
                    )
                    continue
                if name in seen:
                    records.append(
                        PluginRecord(
                            name=name,
                            state=LoadState.LOAD_FAILED,
                            source="dropin",
                            path=module_path,
                            error=f"drop-in {name!r} would shadow an existing plugin",
                        )
                    )
                    continue
                seen.add(name)
                records.append(self._make_record(name, plugin_class, "dropin", module_path))
        return records

    def _exec_module(
        self, path: Path, *, is_package: bool
    ) -> tuple[list[type[Plugin]], str | None]:
        stem = path.parent.name if is_package else path.stem
        self._counter += 1
        module_name = f"smart_panel_dropin_{stem}_{self._counter}"
        try:
            if is_package:
                spec = importlib.util.spec_from_file_location(
                    module_name, path, submodule_search_locations=[str(path.parent)]
                )
            else:
                spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                return [], f"could not build an import spec for {path}"
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            return [], f"{type(exc).__name__}: {exc}"
        classes = [
            obj
            for obj in vars(module).values()
            if isinstance(obj, type)
            and issubclass(obj, Plugin)
            and obj is not Plugin
            and hasattr(obj, "manifest")
        ]
        return classes, None

    @staticmethod
    def _dedupe(records: list[PluginRecord]) -> list[PluginRecord]:
        seen: set[str] = set()
        result: list[PluginRecord] = []
        for record in records:
            if record.name in seen and record.state == LoadState.LOADED:
                result.append(
                    replace(
                        record,
                        state=LoadState.LOAD_FAILED,
                        error=f"duplicate plugin name {record.name!r}",
                    )
                )
                continue
            seen.add(record.name)
            result.append(record)
        return result


def _sdk_major(version: str) -> int:
    head = str(version).split(".", 1)[0]
    return int(head) if head.isdigit() else 0


def _load_builtin_registry() -> dict[str, type[Plugin]]:
    """Read the built-in registry from :mod:`smart_panel.plugins` if it exposes one.

    T061 populates the registry; until then the module is empty and this returns
    ``{}``, so the loader works with entry points and drop-ins alone.
    """
    try:
        package = importlib.import_module("smart_panel.plugins")
    except Exception:
        return {}
    for attribute in ("builtin_plugins", "builtin_classes", "BUILTIN_PLUGINS", "REGISTRY"):
        value = getattr(package, attribute, None)
        if value is None:
            continue
        if callable(value):
            value = value()
        if isinstance(value, Mapping):
            return {str(key): cls for key, cls in value.items()}
        if isinstance(value, (list, tuple)):
            registry: dict[str, type[Plugin]] = {}
            for item in value:
                cls = item if isinstance(item, type) else getattr(item, "plugin_class", None)
                if isinstance(cls, type) and getattr(cls, "manifest", None) is not None:
                    registry[cls.manifest.name] = cls
            return registry
    return {}
