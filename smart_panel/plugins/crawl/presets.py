"""The shipped crawl source presets (T084, US3).

``presets.json`` holds the ESPN scoreboard endpoints and the two Google News
feed builders as plain data (``contracts/builtin-plugins.md``: "presets are plain
configuration so they can be corrected without a code release"). This module
reads them and turns one into the fields of a crawl source:

* the six ``espn-<league>`` presets set ``url``, ``list_path: events`` and the
  headline template ``TB 0 @ DAL 0 - 10/8 - 8:15 PM EDT`` (``research.md``
  decision 11);
* ``google-news-search`` and ``google-news-topic`` build their feed URL from
  ``--query`` / ``--topic`` (percent-encoded), with the polite default interval
  of 300 s.

Only ``smart_panel.sdk``, the standard library and the sibling preset module are
imported (FR-043).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

__all__ = ["PRESETS_PATH", "apply_preset", "list_presets", "load_presets", "preset_source"]

PRESETS_PATH = Path(__file__).resolve().parent / "presets.json"

# URL placeholders a preset may carry, and the verb argument that fills them.
_URL_PARAMS = (("{query}", "query"), ("{topic}", "topic"))


def load_presets() -> dict[str, dict[str, Any]]:
    """Every shipped preset, in file order, keyed by name."""
    document = json.loads(PRESETS_PATH.read_text(encoding="utf-8"))
    presets = document.get("presets") if isinstance(document, dict) else None
    if not isinstance(presets, dict):  # pragma: no cover - a corrupt data file
        raise ValueError(f"{PRESETS_PATH.name} does not contain a 'presets' object")
    return {str(name): dict(preset) for name, preset in presets.items()}


def list_presets() -> list[dict[str, Any]]:
    """The preset listing shown by ``crawl preset list``."""
    return [
        {
            "name": name,
            "kind": preset.get("kind"),
            "label": preset.get("label"),
            "url": preset.get("url"),
            "interval_s": int(preset.get("interval_s", 300)),
        }
        for name, preset in load_presets().items()
    ]


def apply_preset(
    name: str,
    *,
    query: Any = None,
    topic: Any = None,
    url: Any = None,
) -> dict[str, Any]:
    """The source fields a preset contributes (``url`` overrides the preset's)."""
    preset = load_presets().get(str(name))
    if preset is None:
        raise ValueError(f"unknown preset {name!r}; run 'crawl preset list' for the names")

    resolved = str(url) if url else str(preset.get("url") or "")
    for placeholder, param in _URL_PARAMS:
        if placeholder not in resolved:
            continue
        value = query if param == "query" else topic
        if value is None or value == "":
            raise ValueError(f"preset {name!r} needs --{param}")
        resolved = resolved.replace(placeholder, quote_plus(str(value)))

    fields: dict[str, Any] = {
        "kind": str(preset.get("kind") or "rss"),
        "url": resolved,
        "interval_s": int(preset.get("interval_s", 300)),
    }
    if preset.get("label") is not None:
        fields["label"] = str(preset["label"])
    if preset.get("list_path"):
        fields["list_path"] = str(preset["list_path"])
    if preset.get("template"):
        fields["template"] = str(preset["template"])
    if preset.get("max_items") is not None:
        fields["max_items"] = int(preset["max_items"])
    return fields


# ``crawl source add`` calls the preset by name and passes through the placeholder
# arguments it was given; ``preset_source`` is the name the task uses for it.
def preset_source(
    name: str,
    *,
    query: Any = None,
    topic: Any = None,
    url: Any = None,
) -> dict[str, Any]:
    """Alias of :func:`apply_preset` (the name ``T084`` gives the helper)."""
    return apply_preset(name, query=query, topic=topic, url=url)
