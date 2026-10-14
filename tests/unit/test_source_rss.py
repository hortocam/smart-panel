"""Unit tests for the ``sources`` plugin's RSS/Atom source (T075, US3).

Covers ``smart_panel/plugins/sources/rss.py``: RSS 2.0 and Atom item titles,
``max_items`` (default 30), rejection of entity-expansion and DTD attacks
through ``defusedxml``, the 2 MB response cap, the ``http``/``https``-only URL
rule, and the ``interval_s`` floor (30) with its 300 s default.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from smart_panel.core.http import (
    MAX_BYTES,
    HttpFetcher,
    HttpTooLargeError,
    read_bounded,
)
from smart_panel.plugins.sources.rss import (
    DEFAULT_INTERVAL_S,
    INTERVAL_FLOOR_S,
    MAX_ITEMS_DEFAULT,
    MAX_ITEMS_LIMIT,
    RssSource,
    SourceError,
    parse_feed,
    validate_url,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
RSS_SAMPLE = (FIXTURES / "google_news_sample.xml").read_bytes()
ATOM_SAMPLE = (FIXTURES / "atom_sample.xml").read_bytes()

RSS_TITLES = [
    "Rocket Lab launches its 50th Electron mission - Example Wire",
    "AT&T says network investment will rise next year - Datacenter News",
    "Chipmaker opens a second packaging plant - Silicon Desk",
    "Open source maintainers publish a joint funding pledge - Example Wire",
    "Battery startup reports a 900 Wh/kg pouch cell - Datacenter News",
]

ATOM_TITLES = [
    "Atom headline one - Example Wire",
    "Atom headline two: a colon, a dash - and a comma",
    "Atom headline three with & an entity - Datacenter News",
]


class _FakeHttp:
    """A stand-in for ``ctx.http`` that records the call it was handed."""

    def __init__(self, body: bytes = b"", error: Exception | None = None) -> None:
        self.body = body
        self.error = error
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, headers: dict[str, str] | None = None) -> bytes:
        self.calls.append({"url": url, "headers": headers})
        if self.error is not None:
            raise self.error
        return self.body


def _rss(items: int) -> bytes:
    body = "".join(f"<item><title>item {index}</title></item>" for index in range(items))
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>'.encode()


# ------------------------------------------------------------------ parsing


def test_rss_titles_are_extracted_in_order_with_entities_decoded() -> None:
    assert parse_feed(RSS_SAMPLE) == RSS_TITLES


def test_atom_entries_are_extracted_in_order() -> None:
    assert parse_feed(ATOM_SAMPLE) == ATOM_TITLES


def test_max_items_defaults_to_30_and_caps_the_returned_titles() -> None:
    assert MAX_ITEMS_DEFAULT == 30
    assert len(parse_feed(_rss(40))) == MAX_ITEMS_DEFAULT
    assert parse_feed(_rss(40), max_items=2) == ["item 0", "item 1"]


def test_entity_expansion_and_dtd_are_rejected() -> None:
    """A feed must not be able to expand entities or declare a DTD (defusedxml)."""
    dtd_entity = (
        b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b'<rss version="2.0"><channel><item><title>&xxe;</title></item></channel></rss>'
    )
    with pytest.raises(SourceError) as excinfo:
        parse_feed(dtd_entity)
    assert "DTD" in str(excinfo.value) or "entit" in str(excinfo.value).lower()

    billion_laughs = (
        b'<?xml version="1.0"?><!DOCTYPE rss ['
        b'<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">'
        b'<!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">]>'
        b'<rss version="2.0"><channel><item><title>&d;</title></item></channel></rss>'
    )
    with pytest.raises(SourceError):
        parse_feed(billion_laughs)


def test_a_non_xml_body_is_reported_as_a_source_error() -> None:
    with pytest.raises(SourceError):
        parse_feed(b"<html><body>not a feed</body></html>")


# ---------------------------------------------------------------- transport


def test_the_response_cap_is_two_megabytes() -> None:
    assert MAX_BYTES == 2 * 1024 * 1024
    assert len(read_bounded(io.BytesIO(b"a" * MAX_BYTES), MAX_BYTES)) == MAX_BYTES
    with pytest.raises(HttpTooLargeError):
        read_bounded(io.BytesIO(b"a" * (MAX_BYTES + 1)), MAX_BYTES)


def test_only_http_and_https_urls_are_accepted() -> None:
    assert validate_url("https://news.google.com/rss/search?q=x") == (
        "https://news.google.com/rss/search?q=x"
    )
    assert validate_url("http://127.0.0.1:8000/feed.xml") == "http://127.0.0.1:8000/feed.xml"
    for bad in ("file:///etc/passwd", "ftp://example.com/f.xml", "/tmp/f.xml", ""):
        with pytest.raises(SourceError):
            validate_url(bad)
    with pytest.raises(SourceError):
        RssSource("news", {"url": "file:///etc/passwd"})


def test_http_fetcher_exposes_the_documented_limits() -> None:
    fetcher = HttpFetcher()
    assert fetcher.timeout_s == 10.0
    assert fetcher.max_bytes == MAX_BYTES
    assert "smart-panel" in fetcher.user_agent


# ------------------------------------------------------------ source object


def test_interval_floor_and_defaults() -> None:
    assert INTERVAL_FLOOR_S == 30
    assert DEFAULT_INTERVAL_S == 300
    source = RssSource("news", {"url": "https://example.com/feed.xml"})
    assert source.interval_s == DEFAULT_INTERVAL_S
    assert source.max_items == MAX_ITEMS_DEFAULT
    with pytest.raises(SourceError):
        RssSource("news", {"url": "https://example.com/feed.xml", "interval_s": 10})
    with pytest.raises(SourceError):
        RssSource("news", {"url": "https://example.com/feed.xml", "max_items": MAX_ITEMS_LIMIT + 1})


def test_fetch_gets_the_url_and_parses_the_body() -> None:
    http = _FakeHttp(RSS_SAMPLE)
    source = RssSource("news", {"url": "https://example.com/feed.xml", "max_items": 3})
    assert source.fetch(http) == RSS_TITLES[:3]
    assert http.calls == [{"url": "https://example.com/feed.xml", "headers": None}]


def test_fetch_passes_headers_through() -> None:
    http = _FakeHttp(ATOM_SAMPLE)
    source = RssSource(
        "news",
        {"url": "https://example.com/atom", "headers": {"X-Feed": "1"}},
    )
    assert source.fetch(http) == ATOM_TITLES
    assert http.calls[0]["headers"] == {"X-Feed": "1"}


def test_fetch_propagates_a_transport_failure_for_the_plugin_to_keep_last_items() -> None:
    http = _FakeHttp(error=HttpTooLargeError("response is larger than 2097152 bytes"))
    source = RssSource("news", {"url": "https://example.com/feed.xml"})
    with pytest.raises(HttpTooLargeError):
        source.fetch(http)
