"""Typed, ranged settings used by plugin manifests and the core config (T017).

``Setting`` describes one value; :func:`validate` turns a partial mapping of
values into a fully populated, conforming dict or raises :class:`SettingError`
naming the offending dotted path. The runner validates settings *before* they
reach a plugin (contract ``plugin-sdk.md``), so plugin code only ever sees
values that already conform.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

TYPES = frozenset(
    {"int", "float", "bool", "str", "color", "enum", "list", "object", "duration"}
)

_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_SECRET_REF = re.compile(r"^\$\{secret:[A-Za-z0-9_.-]+\}$")
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|m|h|d)?\s*$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# A small CSS-style colour table. ``data-model.md`` and the setting spec require
# ``#RRGGBB``/``#RRGGBBAA``; the SDK contract additionally allows a CSS-style
# name from a small table, so both are accepted.
CSS_COLORS = frozenset(
    {
        "black",
        "white",
        "red",
        "green",
        "blue",
        "yellow",
        "orange",
        "purple",
        "grey",
        "gray",
        "cyan",
        "magenta",
        "transparent",
    }
)


class SettingError(ValueError):
    """A setting value did not conform, or an unknown setting was supplied.

    Attributes:
        path: Dotted path of the offending setting (for example
            ``plugins.text.settings.text``), so a CLI/agent error is actionable.
        message: Human-readable explanation.
    """

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


@dataclass(frozen=True)
class Setting:
    """Declaration of one settings value.

    Args:
        name: Dotted-free setting name (the part after the instance prefix).
        type: One of :data:`TYPES`.
        default: Value used when the setting is omitted.
        description: Shown by ``config schema``/``plugin describe``.
        min: Inclusive lower bound for numeric types.
        max: Inclusive upper bound for numeric types.
        choices: Allowed values for ``enum``.
        pattern: Regex a ``str`` value must match.
        secret: When true the value must be a ``${secret:NAME}`` reference.
        item: Element spec for ``list``.
    """

    name: str
    type: str
    default: Any = None
    description: str = ""
    min: float | None = None
    max: float | None = None
    choices: Sequence[str] | None = None
    pattern: str | None = None
    secret: bool = False
    item: Setting | None = None

    def __post_init__(self) -> None:
        if self.type not in TYPES:
            raise ValueError(f"unknown setting type {self.type!r} for {self.name!r}")
        if self.choices is not None and not isinstance(self.choices, tuple):
            object.__setattr__(self, "choices", tuple(self.choices))


def validate(
    settings_specs: Iterable[Setting], values: Mapping[str, Any] | None, prefix: str = ""
) -> dict[str, Any]:
    """Validate ``values`` against ``settings_specs`` and fill in defaults.

    Args:
        settings_specs: The declared settings.
        values: A (possibly partial) mapping of supplied values.
        prefix: Dotted prefix used in error paths (for example
            ``plugins.text.settings``).

    Returns:
        A new dict containing every declared setting.

    Raises:
        SettingError: An unknown key was supplied, or a value did not conform.
    """
    if values is None:
        values = {}
    if not isinstance(values, Mapping):
        raise SettingError(
            prefix or "<settings>", f"expected an object, got {type(values).__name__}"
        )
    specs = list(settings_specs)
    by_name = {spec.name: spec for spec in specs}
    for key in values:
        if key not in by_name:
            raise SettingError(_join(prefix, key), f"unknown setting {key!r}")
    result: dict[str, Any] = {}
    for spec in specs:
        value = values.get(spec.name, spec.default)
        result[spec.name] = _check(spec, value, _join(prefix, spec.name))
    return result


def _join(prefix: str, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


def _check(spec: Setting, value: Any, path: str) -> Any:
    if spec.secret:
        if not isinstance(value, str) or not _SECRET_REF.match(value):
            raise SettingError(path, "a secret setting accepts only a ${secret:NAME} reference")
        return value
    checker = _CHECKERS.get(spec.type)
    if checker is None:  # pragma: no cover - guarded by Setting.__post_init__
        raise SettingError(path, f"unsupported setting type {spec.type!r}")
    return checker(spec, value, path)


def _check_int(spec: Setting, value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingError(path, f"expected an integer, got {type(value).__name__}")
    return _range(spec, value, path)


def _check_float(spec: Setting, value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingError(path, f"expected a number, got {type(value).__name__}")
    return _range(spec, float(value), path)


def _check_bool(spec: Setting, value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise SettingError(path, f"expected a boolean, got {type(value).__name__}")
    return value


def _check_str(spec: Setting, value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise SettingError(path, f"expected a string, got {type(value).__name__}")
    if spec.pattern is not None and not re.match(spec.pattern, value):
        raise SettingError(path, f"value {value!r} does not match {spec.pattern!r}")
    return value


def _check_color(spec: Setting, value: Any, path: str) -> str:
    if not isinstance(value, str) or not (_HEX_COLOR.match(value) or value.lower() in CSS_COLORS):
        raise SettingError(path, f"expected #RRGGBB, #RRGGBBAA or a colour name, got {value!r}")
    return value


def _check_enum(spec: Setting, value: Any, path: str) -> Any:
    if spec.choices is None or value not in spec.choices:
        raise SettingError(path, f"expected one of {list(spec.choices or [])}, got {value!r}")
    return value


def _check_list(spec: Setting, value: Any, path: str) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise SettingError(path, f"expected a list, got {type(value).__name__}")
    if spec.item is None:
        return list(value)
    return [_check(spec.item, element, f"{path}[{index}]") for index, element in enumerate(value)]


def _check_object(spec: Setting, value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SettingError(path, f"expected an object, got {type(value).__name__}")
    return dict(value)


def _check_duration(spec: Setting, value: Any, path: str) -> int:
    if isinstance(value, bool):
        raise SettingError(path, f"expected a duration, got {type(value).__name__}")
    if isinstance(value, (int, float)):
        seconds = int(value)
    elif isinstance(value, str):
        match = _DURATION.match(value)
        if match is None:
            raise SettingError(path, f"expected a duration like '30s', '5m' or '1h', got {value!r}")
        seconds = int(float(match.group(1)) * _DURATION_UNITS[match.group(2) or "s"])
    else:
        raise SettingError(path, f"expected a duration, got {type(value).__name__}")
    return _range(spec, seconds, path)


def _range(spec: Setting, value: float, path: str) -> Any:
    if spec.min is not None and value < spec.min:
        raise SettingError(path, f"value {value} is below the minimum {spec.min}")
    if spec.max is not None and value > spec.max:
        raise SettingError(path, f"value {value} is above the maximum {spec.max}")
    return value


_CHECKERS = {
    "int": _check_int,
    "float": _check_float,
    "bool": _check_bool,
    "str": _check_str,
    "color": _check_color,
    "enum": _check_enum,
    "list": _check_list,
    "object": _check_object,
    "duration": _check_duration,
}
