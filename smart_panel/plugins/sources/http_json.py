"""The ``http_json`` source type: JSON endpoints mapped to topics (T083, US3).

``research.md`` decision 11: a URL, an optional ``list_path`` into the document,
a ``name -> template`` field map, ``${secret:NAME}`` header references, a
``timeout_s`` of at most 10 s and an ``interval_s`` of at least 30 s.

* a **list** source (``list_path`` set, for example ``events`` for the ESPN
  scoreboards) publishes ``<name>.items`` — one headline per element, rendered
  from the field named ``text`` (or, when that is absent, from the first
  declared field);
* a **scalar** endpoint (no ``list_path``) publishes one ``<name>.<field>``
  topic per declared field, which is what a gauge reads.

Fetches go through ``ctx.http``: this module never opens a socket and imports
only ``smart_panel.sdk``, the standard library and (lazily) nothing else.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from smart_panel.plugins.sources.templating import evaluate, field_values, resolve
from smart_panel.sdk.settings import Setting, SettingError, validate

__all__ = [
    "INTERVAL_FLOOR_S",
    "MAX_ITEMS_DEFAULT",
    "MAX_ITEMS_LIMIT",
    "TIMEOUT_DEFAULT_S",
    "TIMEOUT_MAX_S",
    "JsonSource",
    "SourceError",
    "list_at",
    "parse_json",
]

INTERVAL_FLOOR_S = 30
MAX_ITEMS_DEFAULT = 30
MAX_ITEMS_LIMIT = 200
TIMEOUT_MAX_S = 10
TIMEOUT_DEFAULT_S = 10


class SourceError(ValueError):
    """A source's settings or its payload did not conform."""


def parse_json(body: bytes | str) -> Any:
    """Decode ``body`` as JSON, reporting a bad document as a :class:`SourceError`."""
    if isinstance(body, (bytes, bytearray)):
        text = body.decode("utf-8", errors="replace")
    else:
        text = str(body)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SourceError(f"the endpoint did not return JSON: {exc}") from exc


def list_at(data: Any, list_path: str | None) -> list[Any]:
    """The list of elements at ``list_path`` (the whole document when unset).

    Raises:
        SourceError: The path is absent, or names something that is not a list.
    """
    if not list_path:
        return [data]
    found, value = resolve(data, list_path)
    if not found:
        raise SourceError(f"list_path {list_path!r} is not in the response")
    if not isinstance(value, (list, tuple)):
        raise SourceError(f"list_path {list_path!r} is not a list")
    return list(value)


class JsonSource:
    """One ``http_json`` source."""

    type = "http_json"
    SETTINGS: tuple[Setting, ...] = (
        Setting(name="url", type="str", default="", description="Endpoint URL (http/https)"),
        Setting(
            name="list_path",
            type="str",
            default="",
            description="Dotted path to the element list (empty for a scalar endpoint)",
        ),
        Setting(
            name="fields",
            type="object",
            default={},
            description="Field name to {path.to.field} template",
        ),
        Setting(
            name="headers",
            type="object",
            default={},
            description="Extra request headers; values may be ${secret:NAME}",
        ),
        Setting(
            name="max_items",
            type="int",
            default=MAX_ITEMS_DEFAULT,
            description="Most elements to map from a list response",
            min=1,
            max=MAX_ITEMS_LIMIT,
        ),
        Setting(
            name="interval_s",
            type="int",
            default=300,
            description="Seconds between refreshes (at least 30)",
            min=INTERVAL_FLOOR_S,
        ),
        Setting(
            name="timeout_s",
            type="int",
            default=TIMEOUT_DEFAULT_S,
            description="Socket timeout in seconds (at most 10)",
            min=1,
            max=TIMEOUT_MAX_S,
        ),
    )
    METRICS: tuple[str, ...] = ("items",)

    def __init__(self, name: str, settings: Mapping[str, Any] | None = None) -> None:
        self.name = str(name)
        try:
            self.settings = validate(self.SETTINGS, settings or {})
        except SettingError as exc:
            raise SourceError(str(exc)) from exc
        self.url = self._validate_url(self.settings.get("url"))

    @staticmethod
    def _validate_url(url: Any) -> str:
        from urllib.parse import urlparse

        if not isinstance(url, str) or not url.strip():
            raise SourceError("a source needs a 'url'")
        scheme = urlparse(url).scheme.lower()
        if scheme not in ("http", "https"):
            raise SourceError(f"only http and https URLs are allowed, got {url!r}")
        return url

    @property
    def interval_s(self) -> int:
        return int(self.settings["interval_s"])

    @property
    def timeout_s(self) -> int:
        return int(self.settings["timeout_s"])

    @property
    def max_items(self) -> int:
        return int(self.settings["max_items"])

    @property
    def list_path(self) -> str:
        return str(self.settings.get("list_path") or "")

    @property
    def fields(self) -> dict[str, Any]:
        return dict(self.settings.get("fields") or {})

    @property
    def headers(self) -> dict[str, Any]:
        return dict(self.settings.get("headers") or {})

    def fetch(self, http: Any) -> dict[str, Any]:
        """Fetch, map and return the topics to publish (raising on any failure)."""
        body = http.get(self.url, self.headers or None)
        data = parse_json(body)
        elements = list_at(data, self.list_path)
        if not self.list_path:
            # A scalar endpoint: one topic per declared field.
            published: dict[str, Any] = {"items": []}
            if elements:
                published.update(field_values(self.fields, elements[0]))
            return published
        headline_field = "text" if "text" in self.fields else None
        if headline_field is None and self.fields:
            headline_field = next(iter(self.fields))
        texts: list[str] = []
        for element in elements[: self.max_items]:
            if headline_field is None:
                texts.append("")
                continue
            texts.append(evaluate(str(self.fields[headline_field]), element))
        return {"items": texts}
