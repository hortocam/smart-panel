"""``{path.to.field}`` templates for JSON sources (T083, US3).

``research.md`` decision 11 describes the mapping language: a headline template
with ``{path.to.field}`` placeholders, evaluated over each element of a
configurable list path, with numeric indices for arrays (``{a.b.0.c}``) and
conditional text (an empty rendering) for a field that is absent. A missing
field renders as empty text and never raises, so one odd event in a scoreboard
cannot blank the whole crawl.

Pure functions only: no network, no state.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

__all__ = ["PLACEHOLDER", "evaluate", "field_values", "render_value", "resolve"]

PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


def resolve(data: Any, path: str) -> tuple[bool, Any]:
    """Walk ``path`` through ``data``; return ``(found, value)``.

    A numeric component indexes a list (``competitions.0``); any other component
    is a mapping key. A path that leaves the document, or walks through a
    non-container, reports ``(False, None)`` rather than raising.
    """
    current = data
    for part in str(path).split("."):
        if isinstance(current, Mapping):
            if part not in current:
                return False, None
            current = current[part]
        elif isinstance(current, (list, tuple)):
            try:
                index = int(part)
            except (TypeError, ValueError):
                return False, None
            if index < 0 or index >= len(current):
                return False, None
            current = current[index]
        else:
            return False, None
    return True, current


def render_value(value: Any) -> str:
    """Render one value as plain text (``None`` and containers become empty)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, (int, str)):
        return str(value)
    return ""


def evaluate(template: str, data: Any) -> str:
    """Substitute every ``{path}`` in ``template`` from ``data``.

    An unknown placeholder becomes empty text, which is exactly what
    ``contracts/builtin-plugins.md`` requires for a field that is missing from
    one element of a list.
    """
    if not isinstance(template, str) or not template:
        return ""

    def replace(match: re.Match[str]) -> str:
        found, value = resolve(data, match.group(1).strip())
        return render_value(value) if found else ""

    return PLACEHOLDER.sub(replace, template)


def field_values(fields: Mapping[str, Any] | None, element: Any) -> dict[str, str]:
    """Render every declared ``field -> template`` for one element."""
    return {
        str(name): evaluate(str(template), element)
        for name, template in dict(fields or {}).items()
    }
