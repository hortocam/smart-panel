"""Unit tests for the ``sources`` plugin's JSON source (T076, US3).

Covers ``smart_panel/plugins/sources/http_json.py`` and ``templating.py``: the
``list_path`` mapping, ``{a.b.0.c}`` templates (numeric array indices included),
the ESPN preset headline, empty text for missing fields, the ``interval_s``
floor of 30 and the ``timeout_s`` ceiling of 10, and secret substitution in
headers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from smart_panel.core.http import HttpFetcher
from smart_panel.plugins.crawl.presets import preset_source
from smart_panel.plugins.sources.http_json import (
    INTERVAL_FLOOR_S,
    MAX_ITEMS_DEFAULT,
    TIMEOUT_MAX_S,
    JsonSource,
    SourceError,
    list_at,
    parse_json,
)
from smart_panel.plugins.sources.templating import evaluate, field_values, resolve

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ESPN = (FIXTURES / "espn_nfl_scoreboard.json").read_bytes()
ESPN_HEADLINE = "TB 0 @ DAL 0 - 10/8 - 8:15 PM EDT"


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

    def get_json(self, url: str, headers: dict[str, str] | None = None) -> object:
        return json.loads(self.get(url, headers))


# ---------------------------------------------------------------- templates


def test_templates_support_dotted_paths_and_numeric_indices() -> None:
    data = {"a": {"b": [{"c": "first"}, {"c": "second"}]}}
    assert evaluate("{a.b.0.c}", data) == "first"
    assert evaluate("{a.b.1.c}", data) == "second"
    assert evaluate("x {a.b.0.c} y", data) == "x first y"
    assert evaluate("no placeholders", data) == "no placeholders"


def test_templates_render_scalars_without_trailing_floats() -> None:
    assert evaluate("{score}", {"score": 0}) == "0"
    assert evaluate("{score}", {"score": 17.0}) == "17"
    assert evaluate("{ratio}", {"ratio": 1.5}) == "1.5"
    assert evaluate("{ok}", {"ok": True}) == "true"


def test_a_missing_field_renders_as_empty_text_without_raising() -> None:
    assert evaluate("{nope.at.all}", {"a": 1}) == ""
    assert evaluate("{a.b.9.c}", {"a": {"b": [{"c": "x"}]}}) == ""
    assert evaluate("{a.b.c}", {"a": {"b": None}}) == ""
    found, value = resolve({"a": {"b": [7]}}, "a.b.0")
    assert found is True and value == 7


def test_field_values_renders_every_declared_field() -> None:
    element = {"team": {"abbreviation": "DAL"}, "score": "0"}
    assert field_values(
        {"text": "{team.abbreviation} {score}", "score": "{score}"}, element
    ) == {"text": "DAL 0", "score": "0"}


# ------------------------------------------------------------------ mapping


def test_the_espn_preset_template_yields_the_documented_headline() -> None:
    preset = preset_source("espn-nfl")
    assert preset["kind"] == "json"
    assert preset["list_path"] == "events"
    assert preset["url"] == (
        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
    )
    http = _FakeHttp(ESPN)
    source = JsonSource(
        "nfl",
        {
            "url": preset["url"],
            "list_path": preset["list_path"],
            "fields": {"text": preset["template"]},
        },
    )
    published = source.fetch(http)
    assert published["items"][0] == ESPN_HEADLINE
    assert len(published["items"]) == 3
    # The third event carries no scores: the template renders them as empty text.
    assert "PHI" in published["items"][2] and published["items"][2].endswith("Final")
    assert "  " in published["items"][2].replace(" - ", " ")


def test_a_scalar_endpoint_publishes_one_topic_per_field() -> None:
    http = _FakeHttp(json.dumps({"cpu": {"percent": 12.5, "label": "CPU"}}).encode())
    source = JsonSource(
        "sys",
        {
            "url": "https://example.com/status.json",
            "fields": {"cpu": "{cpu.percent}", "label": "{cpu.label}"},
        },
    )
    published = source.fetch(http)
    assert published["cpu"] == "12.5"
    assert published["label"] == "CPU"
    assert published["items"] == []


def test_list_at_reports_a_missing_or_wrong_path() -> None:
    data = parse_json(ESPN)
    assert len(list_at(data, "events")) == 3
    assert list_at(data, "") == [data]
    with pytest.raises(SourceError):
        list_at(data, "nope")
    with pytest.raises(SourceError):
        list_at(data, "leagues.0.abbreviation")


def test_a_non_json_body_is_reported_as_a_source_error() -> None:
    with pytest.raises(SourceError):
        parse_json(b"<html>not json</html>")


# ------------------------------------------------------------------- bounds


def test_interval_floor_and_timeout_ceiling() -> None:
    assert INTERVAL_FLOOR_S == 30
    assert TIMEOUT_MAX_S == 10
    source = JsonSource("nfl", {"url": "https://example.com/scores.json"})
    assert source.interval_s == 300
    assert source.max_items == MAX_ITEMS_DEFAULT
    assert source.timeout_s == 10
    with pytest.raises(SourceError):
        JsonSource("nfl", {"url": "https://example.com/scores.json", "interval_s": 10})
    with pytest.raises(SourceError):
        JsonSource("nfl", {"url": "https://example.com/scores.json", "timeout_s": 30})
    with pytest.raises(SourceError):
        JsonSource("nfl", {"url": "file:///etc/passwd"})


def test_fetch_propagates_a_transport_failure() -> None:
    http = _FakeHttp(error=RuntimeError("connection refused"))
    source = JsonSource("nfl", {"url": "https://example.com/scores.json"})
    with pytest.raises(RuntimeError):
        source.fetch(http)


# ------------------------------------------------------------------ secrets


def test_headers_substitute_secret_references_and_are_redacted() -> None:
    fetcher = HttpFetcher(secret_resolver=lambda name: f"Bearer {name}-token")
    headers = fetcher.resolve_headers(
        {"Authorization": "${secret:espn}", "X-Static": "v1"}
    )
    assert headers == {"Authorization": "Bearer espn-token", "X-Static": "v1"}
    scrubbed = fetcher.redact("failed with Bearer espn-token")
    assert "espn-token" not in scrubbed
    assert "***" in scrubbed
