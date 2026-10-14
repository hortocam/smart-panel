"""The ``rss`` source type: RSS 2.0 and Atom titles (T082, US3).

Parsing goes through ``defusedxml`` (``research.md`` decision 11) so a hostile
feed cannot expand entities or declare a DTD; the import is lazy, because
``defusedxml`` ships in the ``app`` extra and the CLI must stay importable
without it. Fetches go through ``ctx.http`` (bounded, ``http``/``https`` only), so
this module never opens a socket itself.

Only ``smart_panel.sdk``, the standard library and ``defusedxml`` are imported
(FR-043).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from smart_panel.sdk.settings import Setting, SettingError, validate

__all__ = [
    "DEFAULT_INTERVAL_S",
    "INTERVAL_FLOOR_S",
    "MAX_FEED_BYTES",
    "MAX_ITEMS_DEFAULT",
    "MAX_ITEMS_LIMIT",
    "RssSource",
    "SourceError",
    "parse_feed",
    "strip_control",
    "validate_url",
]

INTERVAL_FLOOR_S = 30
DEFAULT_INTERVAL_S = 300
MAX_ITEMS_DEFAULT = 30
MAX_ITEMS_LIMIT = 200

# The 2 MB response cap (research.md decision 11). ``ctx.http`` enforces it on
# the wire; it is repeated here so a body handed straight to ``parse_feed`` is
# bounded too. A plugin may import only ``smart_panel.sdk`` (FR-043), so this
# module does not read the constant from ``smart_panel.core.http``.
MAX_FEED_BYTES = 2 * 1024 * 1024


class SourceError(ValueError):
    """A source's settings or its payload did not conform."""


def strip_control(text: str) -> str:
    """Drop C0/C1 control characters, keeping the text otherwise intact."""
    return "".join(
        character
        for character in text
        if character in ("\t", "\n", "\r") or ord(character) >= 0x20
    ).replace("\t", " ").replace("\n", " ").replace("\r", " ")


def validate_url(url: Any) -> str:
    """Return ``url`` when it is an ``http``/``https`` URL, else raise."""
    if not isinstance(url, str) or not url.strip():
        raise SourceError("a source needs a 'url'")
    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise SourceError(f"only http and https URLs are allowed, got {url!r}")
    return url


def _defused_element_tree() -> Any:
    """Import ``defusedxml.ElementTree`` lazily (it lives in the ``app`` extra)."""
    try:
        from defusedxml import ElementTree
    except ImportError as exc:  # pragma: no cover - exercised by a base install
        raise SourceError(
            "reading RSS/Atom feeds needs the 'app' extra: pip install 'panel-driver[app]'"
        ) from exc
    return ElementTree


def _local(tag: Any) -> str:
    """The tag name without its XML namespace."""
    text = str(tag)
    return text.rpartition("}")[2]


def _element_text(element: Any) -> str:
    if element is None or element.text is None:
        return ""
    return strip_control(str(element.text)).strip()


def _find_child(element: Any, name: str) -> Any:
    """The first child whose local tag is ``name`` (namespace-insensitive).

    ``Element.find("title")`` does not match Atom's namespaced ``<title>``, so
    the children are matched on their local name instead.
    """
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def _item_elements(root: Any) -> list[Any]:
    tag = _local(root.tag)
    if tag == "rss":
        channel = root.find("channel")
        if channel is None:
            return []
        return [child for child in channel if _local(child.tag) == "item"]
    if tag == "feed":  # Atom
        return [child for child in root if _local(child.tag) == "entry"]
    return []


def parse_feed(data: bytes | str, max_items: int = MAX_ITEMS_DEFAULT) -> list[str]:
    """Return up to ``max_items`` item titles from an RSS 2.0 or Atom document.

    Raises:
        SourceError: The body is not a parseable feed, is larger than the 2 MB
            cap, is not RSS/Atom, or declares a DTD or entities.
    """
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if len(payload) > MAX_FEED_BYTES:
        raise SourceError(f"the feed is larger than {MAX_FEED_BYTES} bytes")
    limit = int(max_items)
    if limit < 1:
        raise SourceError("max_items must be at least 1")
    element_tree = _defused_element_tree()
    try:
        root = element_tree.fromstring(payload, forbid_dtd=True)
    except ValueError as exc:  # DefusedXmlException is a ValueError
        raise SourceError(
            "the feed declares a DTD or entity expansions, which are refused"
        ) from exc
    except Exception as exc:  # xml.etree.ElementTree.ParseError (a SyntaxError)
        raise SourceError(f"the feed is not valid XML: {exc}") from exc
    titles: list[str] = []
    for item in _item_elements(root):
        title = _element_text(_find_child(item, "title"))
        if title:
            titles.append(title)
        if len(titles) >= limit:
            break
    if not titles:
        raise SourceError("the document is not an RSS 2.0 or Atom feed with items")
    return titles


class RssSource:
    """One ``rss`` source: a URL, a title cap and its own refresh interval."""

    type = "rss"
    SETTINGS: tuple[Setting, ...] = (
        Setting(name="url", type="str", default="", description="Feed URL (http/https)"),
        Setting(
            name="max_items",
            type="int",
            default=MAX_ITEMS_DEFAULT,
            description="Most titles to keep from the feed",
            min=1,
            max=MAX_ITEMS_LIMIT,
        ),
        Setting(
            name="interval_s",
            type="int",
            default=DEFAULT_INTERVAL_S,
            description="Seconds between refreshes (at least 30)",
            min=INTERVAL_FLOOR_S,
        ),
        Setting(
            name="headers",
            type="object",
            default={},
            description="Extra request headers; values may be ${secret:NAME}",
        ),
    )
    METRICS: tuple[str, ...] = ("items",)

    def __init__(self, name: str, settings: Mapping[str, Any] | None = None) -> None:
        self.name = str(name)
        try:
            self.settings = validate(self.SETTINGS, settings or {})
        except SettingError as exc:
            raise SourceError(str(exc)) from exc
        self.url = validate_url(self.settings.get("url"))

    @property
    def interval_s(self) -> int:
        return int(self.settings["interval_s"])

    @property
    def max_items(self) -> int:
        return int(self.settings["max_items"])

    @property
    def headers(self) -> dict[str, Any]:
        return dict(self.settings.get("headers") or {})

    def fetch(self, http: Any) -> list[str]:
        """Fetch the feed and return its titles (raising on any failure)."""
        body = http.get(self.url, self.headers or None)
        return parse_feed(body, self.max_items)
