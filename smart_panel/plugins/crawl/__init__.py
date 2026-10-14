"""The built-in ``crawl`` plugin: a news-style horizontal ticker (T087, US3).

Implements FR-025 to FR-030 and the ``crawl`` section of
``contracts/builtin-plugins.md``: a seamless horizontal loop over items from the
CLI, RSS/Atom feeds (Google News) and JSON endpoints (the ESPN presets), with
configured colours, speed, font, size, separator, direction and ordering, last
known items kept when a refresh fails, and each source's ``last_fetch`` and
``last_error`` reported in ``status``.

Where the state lives:

* **configuration** (``plugins.crawl.settings.sources``) declares sources;
  ``source add|set|remove`` record a runtime table in ``ctx.kv`` instead, because
  a plugin may not write the config file — a later config change to ``sources``
  replaces it, so the file always has the last word;
* **CLI items** are held in ``ctx.kv`` and published on ``crawl.items.<id>`` so
  they survive a restart and are visible to other plugins;
* **fetches** run on the runner's scheduler through ``ctx.spawn`` with the
  source's interval and timeout (T081), never inside ``render`` (FR-027: refresh
  without interrupting the scroll).

Only ``smart_panel.sdk``, ``PIL`` and this package are imported (FR-043).
"""

from __future__ import annotations

import threading
from typing import Any

from smart_panel.plugins.crawl.model import (
    DEFAULT_SEPARATOR,
    INTERVAL_FLOOR_S,
    MAX_ITEMS_LIMIT,
    ORDERINGS,
    SPEED_DEFAULT,
    SPEED_MAX,
    SPEED_MIN,
    TIMEOUT_MAX_S,
    CrawlItem,
    CrawlSource,
    CrawlValidationError,
    clean_items,
    order_items,
    parse_feed_titles,
    parse_json_items,
)
from smart_panel.plugins.crawl.presets import list_presets, preset_source
from smart_panel.plugins.crawl.render import CrawlRenderer
from smart_panel.sdk.plugin import (
    Command,
    CommandError,
    Manifest,
    Plugin,
    SettingsRejected,
)
from smart_panel.sdk.plugin import (
    Image as RenderImage,
)
from smart_panel.sdk.settings import Setting

__all__ = ["CrawlPlugin", "DEFAULT_SOURCE_ID"]

DEFAULT_SOURCE_ID = "cli"
_FLOOR_PX = 12
_PLACEHOLDER = "No headlines"
_ITEM_TEXT_LIMIT = 20  # how many headlines ``describe_state`` reports

_SETTINGS: tuple[Setting, ...] = (
    Setting(name="fg", type="color", default="#FFFFFF", description="Text colour"),
    Setting(name="bg", type="color", default="#B00020", description="Background colour"),
    Setting(
        name="font",
        type="str",
        default="DejaVuSans-Bold",
        description="Bundled font name or a path to a TTF",
    ),
    Setting(
        name="size",
        type="int",
        default=44,
        description="Text size in pixels (never below frame.min_text_px)",
        min=_FLOOR_PX,
        max=512,
    ),
    Setting(
        name="speed_px_s",
        type="int",
        default=SPEED_DEFAULT,
        description="Scroll speed in pixels per second",
        min=SPEED_MIN,
        max=SPEED_MAX,
    ),
    Setting(
        name="direction",
        type="enum",
        default="left",
        description="Scroll direction",
        choices=["left", "right"],
    ),
    Setting(
        name="separator",
        type="str",
        default=DEFAULT_SEPARATOR,
        description="Text between items (at most 8 characters)",
        pattern=r"^.{0,8}$",
    ),
    Setting(
        name="order",
        type="enum",
        default="round_robin",
        description="How items from several sources are arranged",
        choices=list(ORDERINGS),
    ),
    Setting(
        name="show_source_labels",
        type="bool",
        default=False,
        description="Prefix each item with its source's label",
    ),
    Setting(
        name="placeholder",
        type="str",
        default=_PLACEHOLDER,
        description="Shown while the crawl has never had an item",
    ),
    Setting(
        name="sources",
        type="list",
        default=[],
        description="Crawl sources: {id, kind: cli|rss|json, ...}",
        item=Setting(name="source", type="object", default={}),
    ),
)

_COMMANDS: tuple[Command, ...] = (
    Command(
        name="items set",
        summary="Replace the items of the CLI source (ITEM... or --stdin)",
        args=(
            Setting(
                name="items",
                type="list",
                default=[],
                description="One headline per argument (or one per line with --stdin)",
            ),
            Setting(name="source", type="str", default=None, description="Crawl source id"),
            Setting(
                name="stdin",
                type="bool",
                default=False,
                description="Read one headline per line from standard input",
            ),
        ),
        spoolable=True,
    ),
    Command(
        name="items add",
        summary="Append items to the CLI source",
        args=(
            Setting(name="items", type="list", default=[], description="Headlines to append"),
            Setting(name="source", type="str", default=None, description="Crawl source id"),
            Setting(
                name="stdin",
                type="bool",
                default=False,
                description="Read one headline per line from standard input",
            ),
        ),
        spoolable=True,
    ),
    Command(
        name="items clear",
        summary="Clear the CLI source's items",
        args=(Setting(name="source", type="str", default=None, description="Crawl source id"),),
        spoolable=True,
    ),
    Command(
        name="source add",
        summary="Add an RSS/Atom or JSON source",
        args=(
            Setting(name="id", type="str", default="", description="Source id"),
            Setting(
                name="kind",
                type="enum",
                default="rss",
                description="Source kind",
                choices=["rss", "json"],
            ),
            Setting(name="url", type="str", default=None, description="Feed or endpoint URL"),
            Setting(
                name="preset",
                type="str",
                default=None,
                description="A shipped preset (see 'crawl preset list')",
            ),
            Setting(
                name="query",
                type="str",
                default=None,
                description="Search query for a preset",
            ),
            Setting(
                name="topic",
                type="str",
                default=None,
                description="Topic for a preset",
            ),
            Setting(name="template", type="str", default=None, description="Headline template"),
            Setting(name="list_path", type="str", default=None, description="JSON list path"),
            Setting(
                name="interval",
                type="int",
                default=None,
                description="Refresh interval in seconds (floor 30)",
                min=INTERVAL_FLOOR_S,
            ),
            Setting(
                name="label",
                type="str",
                default=None,
                description="Shown as the source label",
            ),
        ),
    ),
    Command(
        name="source set",
        summary="Change one field of a source",
        args=(
            Setting(name="id", type="str", default="", description="Source id"),
            Setting(name="kind", type="enum", default=None, description="Source kind",
                    choices=["rss", "json"]),
            Setting(name="url", type="str", default=None, description="Feed or endpoint URL"),
            Setting(name="template", type="str", default=None, description="Headline template"),
            Setting(name="list_path", type="str", default=None, description="JSON list path"),
            Setting(
                name="max_items",
                type="int",
                default=None,
                description="Most items to keep from the source",
                min=1,
                max=MAX_ITEMS_LIMIT,
            ),
            Setting(name="interval", type="int", default=None, description="Refresh interval",
                    min=INTERVAL_FLOOR_S),
            Setting(name="label", type="str", default=None, description="Source label"),
            Setting(name="timeout", type="int", default=None, description="Fetch timeout",
                    min=1, max=TIMEOUT_MAX_S),
        ),
    ),
    Command(
        name="source remove",
        summary="Remove a source",
        args=(Setting(name="id", type="str", default="", description="Source id"),),
    ),
    Command(
        name="source list",
        summary="List the sources and their health",
        args=(),
    ),
    Command(
        name="preset list",
        summary="List the shipped source presets",
        args=(),
    ),
)


class CrawlPlugin(Plugin):
    """Scroll the configured items across the crawl region."""

    manifest = Manifest(
        name="crawl",
        version="0.1.0",
        summary="News-style horizontal ticker of CLI, RSS and JSON headlines",
        capabilities=frozenset({"region"}),
        settings=_SETTINGS,
        commands=_COMMANDS,
        animated=True,
    )

    def __init__(self, ctx: Any, settings: dict[str, Any]) -> None:
        super().__init__(ctx, settings)
        self._lock = threading.RLock()
        self._renderer = CrawlRenderer()
        self._configured: dict[str, CrawlSource] = {
            source.id: source for source in self._parse_sources(self.settings.get("sources"))
        }
        self._runtime: dict[str, CrawlSource] = {}
        self._items: dict[str, list[str]] = {}
        self._health: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, Any] = {}
        self._ever_had_items = False

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Load the persisted state and schedule every source's refresh."""
        with self._lock:
            stored_sources = self._kv_get("sources")
            if isinstance(stored_sources, dict) and stored_sources:
                parsed = self._parse_sources(list(stored_sources.values()))
                self._runtime = {source.id: source for source in parsed}
            stored_items = self._kv_get("items")
            if isinstance(stored_items, dict):
                self._items = {
                    str(name): clean_items(value if isinstance(value, list) else [])
                    for name, value in stored_items.items()
                }
                self._ever_had_items = any(self._items.values())
            for name, items in self._items.items():
                if items:
                    self._publish_items(name, items)
            self._plan()

    def stop(self) -> None:
        """Cancel the scheduled fetches; must return within 2 s."""
        with self._lock:
            for task in self._tasks.values():
                try:
                    task.cancel()
                except Exception:  # pragma: no cover - a cancel must never raise
                    pass
            self._tasks.clear()
        self._renderer.invalidate()

    def on_settings_changed(self, settings: dict[str, Any]) -> None:
        """Adopt a validated settings change, refusing an unusable source list."""
        try:
            configured = self._parse_sources(settings.get("sources"))
        except CrawlValidationError as exc:
            raise SettingsRejected(str(exc), setting="sources") from exc
        previous = {source.id: source.to_dict() for source in self._configured.values()}
        self.settings = dict(settings)
        with self._lock:
            self._configured = {source.id: source for source in configured}
            if {source.id: source.to_dict() for source in configured} != previous:
                self._kv_delete("sources")
                self._runtime = {}
            self._plan()

    # --------------------------------------------------------------- render

    def render(self, frame: Any) -> Any:
        """Draw the ticker window for the current items (exactly ``frame.size``)."""
        with self._lock:
            ordered = self._ordered_items()
            ever = self._ever_had_items
        if not ordered:
            if ever:
                return RenderImage(self._renderer.render(frame, [], self.settings))
            return RenderImage(self._renderer.placeholder_image(frame, self.settings))
        return RenderImage(
            self._renderer.render(frame, [item.text for item in ordered], self.settings)
        )

    def _ordered_items(self) -> list[CrawlItem]:
        by_source: dict[str, list[CrawlItem]] = {}
        sources = dict(self._configured)
        sources.update(self._runtime)
        for name, texts in self._items.items():
            source = sources.get(name)
            label = source.label if source is not None else None
            show_label = bool(self.settings.get("show_source_labels")) and bool(label)
            by_source[name] = [
                CrawlItem(f"{label}: {text}" if show_label else text, name) for text in texts
            ]
        return order_items(by_source, str(self.settings.get("order", "round_robin")))

    # -------------------------------------------------------------- commands

    def handle_command(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run one ``crawl`` verb; raise ``CommandError`` for expected failures."""
        try:
            if name == "items set":
                return self._items_set(args)
            if name == "items add":
                return self._items_add(args)
            if name == "items clear":
                return self._items_clear(args)
            if name == "source add":
                return self._source_add(args)
            if name == "source set":
                return self._source_set(args)
            if name == "source remove":
                return self._source_remove(args)
            if name == "source list":
                return {"sources": [self._describe(name) for name in sorted(self._sources())]}
            if name == "preset list":
                return {"presets": list_presets()}
        except CrawlValidationError as exc:
            raise CommandError("invalid_input", str(exc)) from exc
        raise CommandError("invalid_input", f"unknown command {name!r}")

    def describe_state(self) -> dict[str, Any]:
        """Item count, each source's health and the current ordering (never a secret)."""
        with self._lock:
            ordered = self._ordered_items()
            return {
                "items": len(ordered),
                "item_texts": [item.text for item in ordered[:_ITEM_TEXT_LIMIT]],
                "order": str(self.settings.get("order", "round_robin")),
                "sources": {name: self._describe(name) for name in sorted(self._sources())},
            }

    # --------------------------------------------------------------- items

    def _items_set(self, args: dict[str, Any]) -> dict[str, Any]:
        name = self._cli_source_name(args.get("source"))
        self._store_items(name, clean_items(self._coerce_items(args.get("items"))))
        return {"source": name, "count": len(self._items.get(name, []))}

    def _items_add(self, args: dict[str, Any]) -> dict[str, Any]:
        name = self._cli_source_name(args.get("source"))
        current = list(self._items.get(name, []))
        self._store_items(name, current + clean_items(self._coerce_items(args.get("items"))))
        return {"source": name, "count": len(self._items.get(name, []))}

    def _items_clear(self, args: dict[str, Any]) -> dict[str, Any]:
        name = self._cli_source_name(args.get("source"))
        self._store_items(name, [])
        return {"source": name, "count": 0}

    def _coerce_items(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        raise CommandError("invalid_input", "items needs one or more strings")

    def _cli_source_name(self, name: Any) -> str:
        """The source an ``items`` verb writes to, creating a ``cli`` source if needed."""
        source_id = str(name).strip() if name else DEFAULT_SOURCE_ID
        with self._lock:
            if source_id in self._sources():
                source = self._sources()[source_id]
                if source.kind != "cli":
                    raise CommandError(
                        "invalid_input", f"source {source_id!r} is a {source.kind} source"
                    )
                return source_id
            source = CrawlSource.from_dict({"id": source_id, "kind": "cli"})
            self._runtime[source_id] = source
            self._persist_sources()
        return source_id

    def _store_items(self, name: str, items: list[str]) -> None:
        with self._lock:
            if items:
                self._items[name] = items
                self._ever_had_items = True
            else:
                self._items.pop(name, None)
            self._persist_items()
            self._publish_items(name, items)
            self._health.setdefault(name, {})
            self._health[name]["items"] = len(items)
        self._touch()

    def _publish_items(self, name: str, items: list[str]) -> None:
        bus = getattr(self.ctx, "bus", None)
        if bus is not None:
            bus.publish(f"crawl.items.{name}", list(items))

    # -------------------------------------------------------------- sources

    def _source_add(self, args: dict[str, Any]) -> dict[str, Any]:
        fields: dict[str, Any] = {"id": args.get("id")}
        preset = args.get("preset")
        if preset:
            try:
                fields.update(
                    preset_source(
                        str(preset),
                        query=args.get("query"),
                        topic=args.get("topic"),
                        url=args.get("url"),
                    )
                )
            except ValueError as exc:
                raise CommandError("invalid_input", str(exc)) from exc
        for key in ("kind", "url", "template", "list_path", "label"):
            if args.get(key) is not None:
                fields[key] = args[key]
        if args.get("interval") is not None:
            fields["interval_s"] = args["interval"]
        if "kind" not in fields:
            fields["kind"] = "rss"
        source = CrawlSource.from_dict(fields)
        with self._lock:
            self._runtime[source.id] = source
            self._persist_sources()
            self._schedule(source.id)
        return self._describe(source.id)

    def _source_set(self, args: dict[str, Any]) -> dict[str, Any]:
        source_id = str(args.get("id") or "").strip()
        with self._lock:
            current = self._sources().get(source_id)
            if current is None:
                raise CommandError("not_found", f"no crawl source named {source_id!r}")
            fields = current.to_dict()
            for key, _arg in (
                ("kind", "kind"),
                ("url", "url"),
                ("template", "template"),
                ("list_path", "list_path"),
                ("label", "label"),
            ):
                if args.get(key) is not None:
                    fields[key] = args[key]
            if args.get("interval") is not None:
                fields["interval_s"] = args["interval"]
            if args.get("max_items") is not None:
                fields["max_items"] = args["max_items"]
            if args.get("timeout") is not None:
                fields["timeout_s"] = args["timeout"]
            source = CrawlSource.from_dict(fields)
            self._runtime[source.id] = source
            self._persist_sources()
            self._schedule(source.id)
        return self._describe(source.id)

    def _source_remove(self, args: dict[str, Any]) -> dict[str, Any]:
        source_id = str(args.get("id") or "").strip()
        with self._lock:
            if source_id not in self._sources():
                raise CommandError("not_found", f"no crawl source named {source_id!r}")
            self._runtime.pop(source_id, None)
            self._configured.pop(source_id, None)
            self._items.pop(source_id, None)
            self._health.pop(source_id, None)
            task = self._tasks.pop(source_id, None)
            if task is not None:
                task.cancel()
            self._persist_sources()
            self._persist_items()
        self._touch()
        return {"id": source_id, "removed": True}

    def _refresh(self, name: str) -> None:
        """One scheduled refresh of one source (never called from ``render``)."""
        with self._lock:
            source = self._sources().get(name)
            health = self._health.setdefault(name, {})
        if source is None or source.kind == "cli":
            return
        http = getattr(self.ctx, "http", None)
        if http is None:
            health["last_error"] = "this instance has no http helper"
            return
        try:
            body = http.get(source.url, source.headers or None)
            if source.kind == "rss":
                texts = parse_feed_titles(body, source.max_items)
            else:
                texts = [item.text for item in parse_json_items(body, source)]
        except Exception as exc:  # one unreachable source must not stop the ticker
            message = self._redact(exc)
            with self._lock:
                self._health[name]["last_error"] = message
            log = getattr(self.ctx, "log", None)
            if log is not None:
                log.warning("source_unreachable", source=name, url=source.url, error=message)
            notify = getattr(self.ctx, "notify", None)
            if notify is not None:
                notify({"event": "source_unreachable", "source": name, "error": message})
            return
        with self._lock:
            self._items[name] = clean_items(texts)
            if self._items[name]:
                self._ever_had_items = True
            entry = self._health.setdefault(name, {})
            entry["last_error"] = None
            entry["last_fetch"] = self._wall()
            entry["fetches"] = int(entry.get("fetches", 0)) + 1
            entry["items"] = len(self._items[name])
            self._persist_items()
            self._publish_items(name, self._items[name])
        self._touch()

    def _schedule(self, name: str) -> None:
        task = self._tasks.pop(name, None)
        if task is not None:
            try:
                task.cancel()
            except Exception:  # pragma: no cover - a cancel must never raise
                pass
        source = self._sources().get(name)
        if source is None or source.kind == "cli":
            return
        if getattr(self.ctx, "spawn_handler", None) is None:
            # The SDK harness has no scheduler; a test drives ``_refresh`` directly.
            return
        try:
            self._tasks[name] = self.ctx.spawn(
                lambda name=name: self._refresh(name),
                interval_s=float(source.interval_s),
                timeout_s=float(source.timeout_s),
                name=f"crawl-{name}",
            )
        except Exception as exc:  # a scheduler that is already stopped
            self._health.setdefault(name, {})["last_error"] = self._redact(exc)

    def _plan(self) -> None:
        for name in list(self._sources()):
            self._schedule(name)

    # -------------------------------------------------------------- internals

    def _sources(self) -> dict[str, CrawlSource]:
        merged = dict(self._configured)
        merged.update(self._runtime)
        return merged

    def _parse_sources(self, raw: Any) -> list[CrawlSource]:
        """Validate a ``sources`` setting; unusable entries are skipped with a log."""
        parsed: list[CrawlSource] = []
        for entry in raw or []:
            try:
                parsed.append(CrawlSource.from_dict(entry))
            except CrawlValidationError as exc:
                log = getattr(self.ctx, "log", None)
                if log is not None:
                    log.warning("crawl_source_rejected", error=str(exc))
        return parsed

    def _describe(self, name: str) -> dict[str, Any]:
        source = self._sources().get(name)
        health = dict(self._health.get(name) or {})
        return {
            "id": name,
            "kind": source.kind if source is not None else None,
            "url": source.url if source is not None else None,
            "label": source.label if source is not None else None,
            "list_path": source.list_path if source is not None else None,
            "template": source.template if source is not None else None,
            "interval_s": source.interval_s if source is not None else None,
            "last_fetch": health.get("last_fetch"),
            "last_error": health.get("last_error"),
            "fetches": int(health.get("fetches", 0)),
            "items": len(self._items.get(name, [])),
        }

    def _redact(self, exc: BaseException) -> str:
        message = str(exc) or type(exc).__name__
        http = getattr(self.ctx, "http", None)
        if http is not None and hasattr(http, "redact"):
            return str(http.redact(message))
        return message

    def _wall(self) -> float:
        clock = getattr(self.ctx, "clock", None)
        return float(clock.time()) if clock is not None else 0.0

    def _touch(self) -> None:
        request_render = getattr(self.ctx, "request_render", None)
        if request_render is not None:
            request_render()

    def _persist_items(self) -> None:
        self._kv_set("items", {name: list(items) for name, items in self._items.items()})

    def _persist_sources(self) -> None:
        self._kv_set("sources", {name: source.to_dict() for name, source in self._runtime.items()})

    def _kv_get(self, key: str) -> Any:
        kv = getattr(self.ctx, "kv", None)
        return kv.get(key) if kv is not None else None

    def _kv_set(self, key: str, value: Any) -> None:
        kv = getattr(self.ctx, "kv", None)
        if kv is not None:
            kv.set(key, value)

    def _kv_delete(self, key: str) -> None:
        kv = getattr(self.ctx, "kv", None)
        if kv is not None:
            kv.delete(key)
