"""The crawl plugin's model: settings, items, sources and their parsing (T085, US3).

``contracts/builtin-plugins.md`` and ``data-model.md`` fix the shapes here: the
crawl's settings (colours, font, size, speed, direction, separator, ordering,
placeholder, source list), a :class:`CrawlSource` of kind ``cli``, ``rss`` or
``json`` (each with its own ``interval_s``, floor 30), and a
:class:`CrawlItem` whose text is plain, control characters stripped and at most
280 characters (longer text is truncated at a word boundary with an ellipsis).

The parsing lives here too, next to the shapes it produces: item titles from an
RSS 2.0 or Atom document (``defusedxml``, so a hostile feed cannot expand
entities) and headline text from a JSON document through a ``{path.to.field}``
template. Fetches go through ``ctx.http``; nothing here opens a socket.

Only ``smart_panel.sdk``, the standard library and ``defusedxml`` are imported
(FR-043) — a built-in plugin is a self-contained package.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

__all__ = [
    "DEFAULT_SEPARATOR",
    "INTERVAL_FLOOR_S",
    "MAX_ITEMS_DEFAULT",
    "MAX_ITEMS_LIMIT",
    "MAX_ITEM_CHARS",
    "ORDERINGS",
    "SOURCE_KINDS",
    "SPEED_DEFAULT",
    "SPEED_MAX",
    "SPEED_MIN",
    "TIMEOUT_MAX_S",
    "CrawlItem",
    "CrawlSource",
    "CrawlValidationError",
    "clean_text",
    "evaluate_template",
    "order_items",
    "parse_feed_titles",
    "parse_json_items",
    "strip_control",
    "truncate_item",
]

MAX_ITEM_CHARS = 280
DEFAULT_SEPARATOR = "  •  "
SEPARATOR_MAX_CHARS = 8

SPEED_MIN = 20
SPEED_MAX = 600
SPEED_DEFAULT = 140

INTERVAL_FLOOR_S = 30
DEFAULT_RSS_INTERVAL_S = 300
DEFAULT_JSON_INTERVAL_S = 60
MAX_ITEMS_DEFAULT = 30
MAX_ITEMS_LIMIT = 200
TIMEOUT_MAX_S = 10
TIMEOUT_DEFAULT_S = 10

SOURCE_KINDS = ("cli", "rss", "json")
ORDERINGS = ("round_robin", "grouped")

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


class CrawlValidationError(ValueError):
    """A crawl source or item did not conform."""


def strip_control(text: str) -> str:
    """Drop control characters, keeping tabs and newlines as spaces."""
    cleaned = "".join(
        character
        for character in str(text)
        if character in ("\t", "\n", "\r") or ord(character) >= 0x20
    )
    return cleaned.replace("\t", " ").replace("\n", " ").replace("\r", " ")


def truncate_item(text: str, limit: int = MAX_ITEM_CHARS) -> str:
    """Truncate ``text`` to ``limit`` characters at a word boundary, with an ellipsis."""
    value = str(text)
    if limit < 1 or len(value) <= limit:
        return value
    head = value[: limit - 1]
    cut = head.rfind(" ")
    if cut > 0:
        head = head[:cut]
    return head.rstrip() + "…"


def clean_text(text: Any) -> str:
    """The canonical crawl item text: control characters stripped, then truncated."""
    return truncate_item(strip_control("" if text is None else str(text))).strip()


def evaluate_template(template: str, data: Any) -> str:
    """Render ``{path.to.field}`` placeholders from ``data`` (missing -> empty)."""
    if not isinstance(template, str) or not template:
        return ""

    def replace(match: re.Match[str]) -> str:
        found, value = _resolve(data, match.group(1).strip())
        return _render(value) if found else ""

    return _PLACEHOLDER.sub(replace, template)


def _resolve(data: Any, path: str) -> tuple[bool, Any]:
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


def _render(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, (int, str)):
        return str(value)
    return ""


# --------------------------------------------------------------------- items


@dataclass(frozen=True)
class CrawlItem:
    """One headline on the ticker."""

    text: str
    source_id: str
    priority: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", truncate_item(strip_control(self.text)))

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "source_id": self.source_id, "priority": self.priority}


def order_items(by_source: Mapping[str, Sequence[CrawlItem]], order: str) -> list[CrawlItem]:
    """Interleave (``round_robin``) or group (``grouped``) the sources' items.

    ``FR-029``: several sources are either interleaved or grouped by a
    configurable order. Sources are visited in name order so the result is
    stable; an unknown order falls back to ``grouped``.
    """
    groups = [list(by_source[name]) for name in sorted(by_source)]
    if str(order) == "round_robin":
        ordered: list[CrawlItem] = []
        index = 0
        while True:
            added = False
            for group in groups:
                if index < len(group):
                    ordered.append(group[index])
                    added = True
            if not added:
                return ordered
            index += 1
    return [item for group in groups for item in group]


# ------------------------------------------------------------------ sources


@dataclass
class CrawlSource:
    """One configured crawl source: CLI items, an RSS/Atom feed or a JSON endpoint."""

    id: str
    kind: str
    url: str = ""
    list_path: str = ""
    template: str = ""
    max_items: int = MAX_ITEMS_DEFAULT
    interval_s: int = DEFAULT_RSS_INTERVAL_S
    headers: dict[str, str] = field(default_factory=dict)
    label: str | None = None
    timeout_s: int = TIMEOUT_DEFAULT_S

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CrawlSource:
        """Build and validate a source from a config or CLI mapping."""
        raw = dict(data or {})
        source_id = str(raw.get("id") or "").strip()
        if not _ID_PATTERN.match(source_id):
            raise CrawlValidationError(
                "a source needs an 'id' of 1-32 characters ([A-Za-z0-9._-], starting alphanumeric)"
            )
        kind = str(raw.get("kind") or "cli").strip().lower()
        if kind not in SOURCE_KINDS:
            raise CrawlValidationError(
                f"source {source_id!r} has kind {kind!r}; expected one of {list(SOURCE_KINDS)}"
            )
        url = str(raw.get("url") or "")
        if kind in ("rss", "json"):
            if not url:
                raise CrawlValidationError(f"a {kind} source needs a 'url'")
            scheme = urlparse(url).scheme.lower()
            if scheme not in ("http", "https"):
                raise CrawlValidationError(
                    f"only http and https URLs are allowed, got {url!r}"
                )
        template = str(raw.get("template") or "")
        if kind == "json" and not template:
            raise CrawlValidationError("a json source needs a 'template'")

        default_interval = DEFAULT_JSON_INTERVAL_S if kind == "json" else (
            DEFAULT_RSS_INTERVAL_S if kind == "rss" else INTERVAL_FLOOR_S
        )
        interval = raw.get("interval_s")
        interval_s = int(default_interval if interval is None else interval)
        if kind in ("rss", "json") and interval_s < INTERVAL_FLOOR_S:
            raise CrawlValidationError(
                f"source {source_id!r} has interval_s {interval_s}; the floor is {INTERVAL_FLOOR_S}"
            )
        max_items = int(raw.get("max_items") or MAX_ITEMS_DEFAULT)
        if max_items < 1 or max_items > MAX_ITEMS_LIMIT:
            raise CrawlValidationError(
                f"source {source_id!r} has max_items {max_items}; expected 1..{MAX_ITEMS_LIMIT}"
            )
        timeout_s = int(raw.get("timeout_s") or TIMEOUT_DEFAULT_S)
        if timeout_s < 1 or timeout_s > TIMEOUT_MAX_S:
            raise CrawlValidationError(
                f"source {source_id!r} has timeout_s {timeout_s}; expected 1..{TIMEOUT_MAX_S}"
            )
        headers = raw.get("headers") or {}
        if not isinstance(headers, Mapping):
            raise CrawlValidationError(f"source {source_id!r} has non-object 'headers'")
        label = raw.get("label")
        return cls(
            id=source_id,
            kind=kind,
            url=url,
            list_path=str(raw.get("list_path") or ""),
            template=template,
            max_items=max_items,
            interval_s=interval_s,
            headers={str(name): str(value) for name, value in headers.items()},
            label=str(label) if label else None,
            timeout_s=timeout_s,
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "kind": self.kind, "interval_s": self.interval_s}
        if self.url:
            data["url"] = self.url
        if self.list_path:
            data["list_path"] = self.list_path
        if self.template:
            data["template"] = self.template
        if self.kind in ("rss", "json"):
            data["max_items"] = self.max_items
            data["timeout_s"] = self.timeout_s
        if self.headers:
            data["headers"] = dict(self.headers)
        if self.label:
            data["label"] = self.label
        return data


# ------------------------------------------------------------------ parsing


def _defused_element_tree() -> Any:
    """Import ``defusedxml.ElementTree`` lazily (it lives in the ``app`` extra)."""
    try:
        from defusedxml import ElementTree
    except ImportError as exc:  # pragma: no cover - exercised by a base install
        raise CrawlValidationError(
            "reading RSS/Atom feeds needs the 'app' extra: pip install 'panel-driver[app]'"
        ) from exc
    return ElementTree


def _local(tag: Any) -> str:
    return str(tag).rpartition("}")[2]


def _element_text(element: Any) -> str:
    if element is None or element.text is None:
        return ""
    return clean_text(str(element.text))


def _find_child(element: Any, name: str) -> Any:
    """The first child whose local tag is ``name`` (namespace-insensitive).

    Atom puts every element in the ``http://www.w3.org/2005/Atom`` namespace, so
    ``Element.find("title")`` would not match its ``<title>``.
    """
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def parse_feed_titles(body: bytes | str, max_items: int = MAX_ITEMS_DEFAULT) -> list[str]:
    """Titles from an RSS 2.0 or Atom document (``defusedxml``).

    Raises:
        CrawlValidationError: The body is not a feed, or declares a DTD or entities.
    """
    payload = body.encode("utf-8") if isinstance(body, str) else bytes(body)
    element_tree = _defused_element_tree()
    try:
        root = element_tree.fromstring(payload)
    except ValueError as exc:  # DefusedXmlException is a ValueError
        raise CrawlValidationError(
            "the feed declares a DTD or entity expansions, which are refused"
        ) from exc
    except Exception as exc:  # ParseError (a SyntaxError)
        raise CrawlValidationError(f"the feed is not valid XML: {exc}") from exc

    if _local(root.tag) == "rss":
        channel = root.find("channel")
        candidates = [] if channel is None else list(channel)
        elements = [element for element in candidates if _local(element.tag) == "item"]
    elif _local(root.tag) == "feed":
        elements = [element for element in root if _local(element.tag) == "entry"]
    else:
        raise CrawlValidationError("the document is not an RSS 2.0 or Atom feed")

    titles: list[str] = []
    for element in elements:
        title = _element_text(_find_child(element, "title"))
        if title:
            titles.append(title)
        if len(titles) >= int(max_items):
            break
    return titles


def parse_json_items(body: bytes | str, source: CrawlSource) -> list[CrawlItem]:
    """Map a JSON endpoint's elements to headline items for ``source``.

    Raises:
        CrawlValidationError: The body is not JSON, or ``list_path`` does not
            name a list.
    """
    if isinstance(body, (bytes, bytearray)):
        text = body.decode("utf-8", errors="replace")
    else:
        text = str(body)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CrawlValidationError(f"the endpoint did not return JSON: {exc}") from exc
    elements = _list_at(data, source.list_path)
    items: list[CrawlItem] = []
    for element in elements[: source.max_items]:
        headline = clean_text(evaluate_template(source.template, element))
        if headline:
            items.append(CrawlItem(headline, source.id))
    return items


def _list_at(data: Any, list_path: str) -> list[Any]:
    if not list_path:
        return [data]
    found, value = _resolve(data, list_path)
    if not found:
        raise CrawlValidationError(f"list_path {list_path!r} is not in the response")
    if not isinstance(value, (list, tuple)):
        raise CrawlValidationError(f"list_path {list_path!r} is not a list")
    return list(value)


def clean_items(texts: Iterable[Any]) -> list[str]:
    """Clean and drop empty texts, keeping the caller's order."""
    cleaned = [clean_text(text).strip() for text in texts]
    return [text for text in cleaned if text]
