"""The JSON config document: schema, validation, atomic write, dotted edits (T024).

``config.json`` is a single JSON document (``data-model.md``) validated as a
whole before it is applied (FR-006). Unknown keys are rejected with the dotted
path of the offender so an agent editing ``config set`` gets an actionable
message, the write is atomic (temp file plus rename), and dotted-path
``set``/``unset`` edit a candidate copy so the live config is left untouched when
a value does not conform.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable, Collection, Mapping, MutableMapping
from pathlib import Path
from typing import Any

from smart_panel.core import paths
from smart_panel.core.layout import Layout, Overlay, Region
from smart_panel.core.layout import validate as validate_layout
from smart_panel.sdk.settings import Setting, SettingError, validate

CURRENT_VERSION = 1

TOP_LEVEL_KEYS = frozenset(
    {"version", "display", "access", "paths", "layout", "plugins", "sources"}
)
_DISPLAY_KEYS = frozenset(
    {"fps", "brightness", "jpeg_quality", "min_text_px", "sink", "files_dir"}
)
_DISPLAY_SPECS = (
    Setting(name="fps", type="int", default=15, description="", min=5, max=30),
    Setting(name="brightness", type="int", default=50, description="", min=0, max=100),
    Setting(name="min_text_px", type="int", default=24, description="", min=12, max=64),
    Setting(
        name="sink",
        type="enum",
        default="hid",
        description="",
        choices=["hid", "files", "null"],
    ),
)
_DEFAULT_JPEG_QUALITY = {"start": 70, "min": 30}

# Version -> callable that upgrades a document to the next version. A migration
# hook so a future schema change can be applied on load (research.md decision 10).
MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def register_migration(version: int, upgrade: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    """Register a migration applied when a document is at ``version``."""
    MIGRATIONS[version] = upgrade


def default_config() -> dict[str, Any]:
    """Return a fresh default config document."""
    return {
        "version": CURRENT_VERSION,
        "display": {
            "fps": 15,
            "brightness": 50,
            "jpeg_quality": dict(_DEFAULT_JPEG_QUALITY),
            "min_text_px": 24,
            "sink": "hid",
            "files_dir": None,
        },
        "access": {"allowed_users": [], "allowed_group": None},
        "paths": {"spool": None},
        "layout": {"regions": [], "overlays": []},
        "plugins": {},
        "sources": {},
    }


def migrate(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Apply registered migrations up to :data:`CURRENT_VERSION`."""
    if not isinstance(raw, Mapping):
        return dict(raw)  # type: ignore[arg-type]
    data = dict(raw)
    version = data.get("version", CURRENT_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        return data
    while version < CURRENT_VERSION:
        upgrade = MIGRATIONS.get(version)
        if upgrade is None:
            break
        data = upgrade(data)
        version = int(data.get("version", version + 1))
    return data


def validate_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate ``raw`` and return a complete, normalized config copy.

    Raises:
        SettingError: An unknown key or a value that does not conform, naming
            the dotted path of the offender.
        LayoutError: The layout violates a layout rule.
    """
    if not isinstance(raw, Mapping):
        raise SettingError("<config>", f"expected an object, got {type(raw).__name__}")
    _reject_unknown(raw, TOP_LEVEL_KEYS, "")

    cfg = default_config()
    if "version" in raw:
        cfg["version"] = raw["version"]
    if cfg["version"] != CURRENT_VERSION or isinstance(cfg["version"], bool):
        raise SettingError("version", f"unsupported config version {cfg['version']!r}")
    if "display" in raw:
        cfg["display"] = _validate_display(raw["display"])
    if "access" in raw:
        cfg["access"] = _validate_access(raw["access"])
    if "paths" in raw:
        cfg["paths"] = _validate_paths(raw["paths"])
    if "layout" in raw:
        cfg["layout"] = _validate_layout(raw["layout"])
    if "plugins" in raw:
        cfg["plugins"] = _validate_instances(
            raw["plugins"], "plugins", {"plugin", "enabled", "settings"}
        )
    if "sources" in raw:
        cfg["sources"] = _validate_instances(
            raw["sources"], "sources", {"type", "interval_s", "settings"}
        )
    return cfg


class Config:
    """A config document on disk plus its in-memory (live) copy."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else paths.config_file()
        self._data: dict[str, Any] = default_config()

    @property
    def data(self) -> dict[str, Any]:
        """A defensive copy of the live config."""
        return copy.deepcopy(self._data)

    def load(self) -> dict[str, Any]:
        """Read, migrate and validate the file (or return defaults when absent)."""
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        else:
            raw = {}
        self._data = validate_config(migrate(raw))
        return copy.deepcopy(self._data)

    def save(self, data: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Validate and atomically write ``data`` (default: the live config)."""
        candidate = self._data if data is None else data
        validated = validate_config(candidate)
        self._write(validated)
        self._data = validated
        return copy.deepcopy(validated)

    def get(self, dotted: str, default: Any = None) -> Any:
        """Return the value at a dotted path or ``default`` when absent."""
        current: Any = self._data
        for part in dotted.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return default
            current = current[part]
        return copy.deepcopy(current)

    def set(self, dotted: str, value: Any) -> dict[str, Any]:
        """Set a dotted path after validating the whole candidate copy.

        Raises:
            SettingError / LayoutError: When the change does not conform; the
                live config is left untouched.
        """
        candidate = copy.deepcopy(self._data)
        _assign(candidate, dotted, value)
        self._data = validate_config(candidate)
        return copy.deepcopy(self._data)

    def unset(self, dotted: str) -> dict[str, Any]:
        """Remove a dotted path; the schema default then applies."""
        candidate = copy.deepcopy(self._data)
        _remove(candidate, dotted)
        self._data = validate_config(candidate)
        return copy.deepcopy(self._data)

    def _write(self, data: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            os.replace(temp, self.path)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise


# -------------------------------------------------------------------- helpers


def _reject_unknown(mapping: Mapping[str, Any], allowed: Collection[str], prefix: str) -> None:
    for key in mapping:
        if key not in allowed:
            path = f"{prefix}.{key}" if prefix else str(key)
            raise SettingError(path, f"unknown setting {key!r}")


def _as_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingError(path, f"expected an integer, got {type(value).__name__}")
    return value


def _bounded_int(value: Any, path: str, low: int, high: int) -> int:
    number = _as_int(value, path)
    if not low <= number <= high:
        raise SettingError(path, f"value {number} is outside {low}..{high}")
    return number


def _path_or_none(value: Any, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingError(path, f"expected a path string or null, got {type(value).__name__}")
    return value


def _validate_display(display: Any) -> dict[str, Any]:
    if not isinstance(display, Mapping):
        raise SettingError("display", f"expected an object, got {type(display).__name__}")
    _reject_unknown(display, _DISPLAY_KEYS, "display")
    scalar = {
        name: display[name]
        for name in ("fps", "brightness", "min_text_px", "sink")
        if name in display
    }
    scalar_values = validate(_DISPLAY_SPECS, scalar, prefix="display")
    return {
        **scalar_values,
        "jpeg_quality": _validate_jpeg_quality(display.get("jpeg_quality", _DEFAULT_JPEG_QUALITY)),
        "files_dir": _path_or_none(display.get("files_dir"), "display.files_dir"),
    }


def _validate_jpeg_quality(quality: Any) -> dict[str, int]:
    if not isinstance(quality, Mapping):
        raise SettingError("display.jpeg_quality", "expected an object with 'start' and 'min'")
    _reject_unknown(quality, frozenset({"start", "min"}), "display.jpeg_quality")
    start = _bounded_int(quality.get("start", 70), "display.jpeg_quality.start", 30, 95)
    minimum = _bounded_int(quality.get("min", 30), "display.jpeg_quality.min", 20, start)
    return {"start": start, "min": minimum}


def _validate_access(access: Any) -> dict[str, Any]:
    if not isinstance(access, Mapping):
        raise SettingError("access", f"expected an object, got {type(access).__name__}")
    _reject_unknown(access, frozenset({"allowed_users", "allowed_group"}), "access")
    users = access.get("allowed_users", [])
    if not isinstance(users, list) or not all(isinstance(user, str) for user in users):
        raise SettingError("access.allowed_users", "expected a list of user names")
    group = access.get("allowed_group")
    if group is not None and not isinstance(group, str):
        raise SettingError("access.allowed_group", "expected a group name or null")
    return {"allowed_users": list(users), "allowed_group": group}


def _validate_paths(section: Any) -> dict[str, Any]:
    if not isinstance(section, Mapping):
        raise SettingError("paths", f"expected an object, got {type(section).__name__}")
    _reject_unknown(section, frozenset({"spool"}), "paths")
    return {"spool": _path_or_none(section.get("spool"), "paths.spool")}


def _validate_layout(layout: Any) -> dict[str, Any]:
    if not isinstance(layout, Mapping):
        raise SettingError("layout", f"expected an object, got {type(layout).__name__}")
    _reject_unknown(layout, frozenset({"regions", "overlays"}), "layout")
    raw_regions = layout.get("regions", [])
    raw_overlays = layout.get("overlays", [])
    if not isinstance(raw_regions, list) or not isinstance(raw_overlays, list):
        raise SettingError("layout", "'regions' and 'overlays' must be lists")

    regions: list[Region] = []
    normalized_regions: list[dict[str, Any]] = []
    for index, region in enumerate(raw_regions):
        path = f"layout.regions[{index}]"
        if not isinstance(region, Mapping):
            raise SettingError(path, "expected an object")
        _reject_unknown(region, frozenset({"name", "x", "y", "w", "h", "instance"}), path)
        name = region.get("name")
        instance = region.get("instance")
        if not isinstance(name, str) or not isinstance(instance, str):
            raise SettingError(path, "'name' and 'instance' must be strings")
        fields = {key: _as_int(region.get(key), f"{path}.{key}") for key in ("x", "y", "w", "h")}
        regions.append(Region(name=name, instance=instance, **fields))
        normalized_regions.append({"name": name, "instance": instance, **fields})

    overlays: list[Overlay] = []
    normalized_overlays: list[dict[str, Any]] = []
    for index, overlay in enumerate(raw_overlays):
        path = f"layout.overlays[{index}]"
        if not isinstance(overlay, Mapping):
            raise SettingError(path, "expected an object")
        _reject_unknown(overlay, frozenset({"region", "instance", "z"}), path)
        target = overlay.get("region")
        instance = overlay.get("instance")
        z = _as_int(overlay.get("z", 0), f"{path}.z")
        if not isinstance(target, str) or not isinstance(instance, str):
            raise SettingError(path, "'region' and 'instance' must be strings")
        overlays.append(Overlay(region=target, instance=instance, z=z))
        normalized_overlays.append({"region": target, "instance": instance, "z": z})

    validate_layout(Layout(regions=regions, overlays=overlays))
    return {"regions": normalized_regions, "overlays": normalized_overlays}


def _validate_instances(section: Any, prefix: str, allowed: Collection[str]) -> dict[str, Any]:
    if not isinstance(section, Mapping):
        raise SettingError(prefix, f"expected an object, got {type(section).__name__}")
    result: dict[str, Any] = {}
    for name, instance in section.items():
        path = f"{prefix}.{name}"
        if not isinstance(instance, Mapping):
            raise SettingError(path, "expected an object")
        _reject_unknown(instance, allowed, path)
        settings = instance.get("settings", {})
        if not isinstance(settings, Mapping):
            raise SettingError(f"{path}.settings", "expected an object")
        normalized: dict[str, Any] = {"settings": dict(settings)}
        if prefix == "plugins":
            plugin = instance.get("plugin")
            if not isinstance(plugin, str):
                raise SettingError(f"{path}.plugin", "expected a plugin name")
            enabled = instance.get("enabled", True)
            if not isinstance(enabled, bool):
                raise SettingError(f"{path}.enabled", "expected a boolean")
            normalized.update({"plugin": plugin, "enabled": enabled})
        else:
            source_type = instance.get("type")
            if not isinstance(source_type, str):
                raise SettingError(f"{path}.type", "expected a source type")
            normalized["type"] = source_type
            if "interval_s" in instance:
                normalized["interval_s"] = _as_int(instance["interval_s"], f"{path}.interval_s")
        result[name] = normalized
    return result


def _assign(target: MutableMapping[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = target
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, MutableMapping):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _remove(target: MutableMapping[str, Any], dotted: str) -> None:
    parts = dotted.split(".")
    node: Any = target
    for part in parts[:-1]:
        if not isinstance(node, MutableMapping) or part not in node:
            return
        node = node[part]
    if isinstance(node, MutableMapping):
        node.pop(parts[-1], None)
