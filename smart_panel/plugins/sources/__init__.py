"""The built-in ``sources`` plugin: data sources for the display (T081-T083, T095).

A ``datasource`` plugin (``plugin-sdk.md``): it declares the source types
``pushed``, ``rss``, ``http_json`` and ``system`` and fetches each configured
source on its own interval, publishing the results on the bus as
``<name>.<metric>``.

The sources themselves are declarative configuration: the instance's ``sources``
setting carries ``{name: {type, interval_s, settings}}``, and the verbs
``source add|set|remove|list`` record runtime changes in ``ctx.kv`` so they
survive a restart without the plugin having to write the config file (a plugin
may not). A config change to ``sources`` replaces the runtime table, so the file
is always the last word.

Fetches run on the runner's scheduler through ``ctx.spawn`` with the source's
interval and timeout (T081), so a slow endpoint costs one worker slot and never
blocks a frame. A failed refresh keeps the last published value; consumers turn
it ``stale`` once its TTL passes, and ``status`` reports the source's
``last_error``. Only ``smart_panel.sdk`` is imported (FR-043).
"""

from __future__ import annotations

import json
import threading
from typing import Any

from smart_panel.plugins.sources.http_json import (
    INTERVAL_FLOOR_S as JSON_INTERVAL_FLOOR_S,
)
from smart_panel.plugins.sources.http_json import (
    TIMEOUT_MAX_S,
    JsonSource,
    SourceError,
)
from smart_panel.plugins.sources.rss import RssSource
from smart_panel.plugins.sources.system import (
    DEFAULT_INTERVAL_S as SYSTEM_DEFAULT_INTERVAL_S,
)
from smart_panel.plugins.sources.system import (
    INTERVAL_FLOOR_S as SYSTEM_INTERVAL_FLOOR_S,
)
from smart_panel.plugins.sources.system import (
    SourceError as SystemSourceError,
)
from smart_panel.plugins.sources.system import (
    SystemSource,
)
from smart_panel.sdk.context import SystemClock
from smart_panel.sdk.plugin import (
    Command,
    CommandError,
    Manifest,
    Plugin,
    SourceType,
)
from smart_panel.sdk.settings import Setting

__all__ = ["PUSH_TTL_DEFAULT", "SOURCE_TYPES", "SourcesPlugin"]

PUSH_TTL_DEFAULT = 300
_TIMEOUT_FLOOR_S = 1

SOURCE_TYPES: dict[str, type] = {
    "rss": RssSource,
    "http_json": JsonSource,
    "system": SystemSource,
}


def _source_type(name: str, plugin: type, metrics: tuple[str, ...]) -> SourceType:
    return SourceType(name=name, settings=plugin.SETTINGS, metrics=metrics)


_SETTINGS: tuple[Setting, ...] = (
    Setting(
        name="sources",
        type="object",
        default={},
        description=(
            "Source name to {type, interval_s, settings}; "
            "types are pushed, rss, http_json and system"
        ),
    ),
)

_SOURCE_TYPES: tuple[SourceType, ...] = (
    SourceType(
        name="pushed",
        settings=(
            Setting(
                name="ttl_s",
                type="int",
                default=PUSH_TTL_DEFAULT,
                description="Seconds a pushed value stays fresh",
                min=1,
            ),
        ),
        metrics=("push",),
    ),
    _source_type("rss", RssSource, ("items",)),
    _source_type("http_json", JsonSource, ("items",)),
    _source_type(
        "system", SystemSource, ("cpu", "mem", "temp", "disk", "net_rx", "net_tx")
    ),
)

_COMMANDS: tuple[Command, ...] = (
    Command(
        name="push",
        summary="Publish a value on a bus topic",
        args=(
            Setting(name="topic", type="str", default="", description="Bus topic to set"),
            Setting(
                name="value",
                type="object",
                default=None,
                description="JSON value (a number, string, list or object)",
            ),
            Setting(
                name="ttl",
                type="int",
                default=None,
                description="Seconds the value stays fresh (default 300)",
                min=1,
            ),
        ),
        spoolable=True,
    ),
    Command(
        name="get",
        summary="Read the current value and freshness of a topic",
        args=(Setting(name="topic", type="str", default="", description="Bus topic"),),
    ),
    Command(
        name="list",
        summary="List the topics the sources have published",
        args=(
            Setting(
                name="prefix",
                type="str",
                default="",
                description="Only topics starting with this prefix",
            ),
        ),
    ),
    Command(
        name="source add",
        summary="Add and start a data source",
        args=(
            Setting(name="id", type="str", default="", description="Source name"),
            Setting(
                name="type",
                type="enum",
                default="pushed",
                description="Source type",
                choices=["pushed", "rss", "http_json", "system"],
            ),
            Setting(
                name="interval",
                type="int",
                default=None,
                description=(
                    "Refresh interval in seconds (floor 1 for pushed and system, "
                    "30 for network types)"
                ),
                min=1,
            ),
            Setting(
                name="settings",
                type="object",
                default={},
                description="The source type's own settings",
            ),
        ),
    ),
    Command(
        name="source set",
        summary="Change one field of an existing source",
        args=(
            Setting(name="id", type="str", default="", description="Source name"),
            Setting(name="type", type="str", default=None, description="New source type"),
            Setting(name="interval", type="int", default=None, description="New interval", min=1),
            Setting(name="settings", type="object", default=None, description="New settings"),
        ),
    ),
    Command(
        name="source remove",
        summary="Stop and forget a source",
        args=(Setting(name="id", type="str", default="", description="Source name"),),
    ),
    Command(
        name="source list",
        summary="List the configured sources and their health",
        args=(),
    ),
)


class SourcesPlugin(Plugin):
    """Fetch configured sources on their own intervals and publish their metrics."""

    manifest = Manifest(
        name="sources",
        version="0.1.0",
        summary="Data sources: pushed values, RSS/Atom feeds and JSON endpoints",
        capabilities=frozenset({"datasource"}),
        settings=_SETTINGS,
        commands=_COMMANDS,
        source_types=_SOURCE_TYPES,
    )

    def __init__(self, ctx: Any, settings: dict[str, Any]) -> None:
        super().__init__(ctx, settings)
        self._lock = threading.RLock()
        self._sources: dict[str, dict[str, Any]] = {}
        self._health: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, Any] = {}
        self._configured_sources: dict[str, Any] = {}
        # A ``system`` source keeps the previous network counters between
        # refreshes, so one instance per (name, settings) is reused across fetches.
        self._system_lock = threading.Lock()
        self._system_sources: dict[tuple[str, str], Any] = {}

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Adopt the configured source table and schedule every fetch."""
        with self._lock:
            self._configured_sources = dict(self.settings.get("sources") or {})
            override = self._kv_get("sources")
            table = dict(self._configured_sources)
            if isinstance(override, dict):
                table = dict(override)
            self._sources = {str(name): self._normalise(entry) for name, entry in table.items()}
            for name in self._sources:
                self._schedule(name)

    def stop(self) -> None:
        """Cancel every scheduled fetch (must return promptly, contract)."""
        with self._lock:
            for task in self._tasks.values():
                try:
                    task.cancel()
                except Exception:  # pragma: no cover - a cancel must never raise
                    pass
            self._tasks.clear()

    def on_settings_changed(self, settings: dict[str, Any]) -> None:
        """Adopt a validated settings change and re-plan the fetches."""
        previous = self._configured_sources
        self.settings = dict(settings)
        configured = dict(self.settings.get("sources") or {})
        with self._lock:
            self._configured_sources = configured
            if configured != previous:
                # The config file is the last word on the source table.
                self._kv_delete("sources")
                self._sources = {
                    str(name): self._normalise(entry) for name, entry in configured.items()
                }
            self._plan()

    # --------------------------------------------------------------- fetching

    def fetch(self, source_name: str, settings: Any) -> dict[str, Any]:
        """Fetch one source of a declared type; return ``{metric: value}``.

        This is the ``datasource`` entry point named by ``contracts/plugin-sdk.md``;
        the plugin's own scheduled loop calls it, and a runner-side source loop can
        call it too.
        """
        entry = dict(settings or {})
        kind = str(entry.get("type") or "")
        payload = dict(entry.get("settings") or {})
        http = self._http()
        if kind == "pushed":
            return {}
        if kind == "rss":
            return {"items": RssSource(str(source_name), payload).fetch(http)}
        if kind == "http_json":
            return JsonSource(str(source_name), payload).fetch(http)
        if kind == "system":
            return dict(self._system_source(str(source_name), payload).fetch())
        raise SourceError(f"unknown source type {kind!r}")

    def _system_source(self, name: str, payload: dict[str, Any]) -> Any:
        """The ``SystemSource`` for one name and settings, reused so its net rate works."""
        key = (name, json.dumps(payload, sort_keys=True, default=str))
        with self._system_lock:
            source = self._system_sources.get(key)
            if source is None:
                source = SystemSource(name, payload, clock=self._clock())
                self._system_sources[key] = source
            return source

    def _refresh(self, name: str) -> None:
        with self._lock:
            entry = self._sources.get(name)
            health = self._health.setdefault(name, {})
        if entry is None:
            return
        try:
            metrics = self.fetch(name, entry)
        except Exception as exc:  # one bad source must not stop the others
            message = self._redact(exc)
            health["last_error"] = message
            log = getattr(self.ctx, "log", None)
            if log is not None:
                log.warning("source_unreachable", source=name, error=message)
            notify = getattr(self.ctx, "notify", None)
            if notify is not None:
                notify({"event": "source_unreachable", "source": name, "error": message})
            return
        bus = getattr(self.ctx, "bus", None)
        for metric, value in metrics.items():
            if bus is not None:
                bus.publish(f"{name}.{metric}", value)
        health["last_error"] = None
        health["last_fetch"] = self._wall()
        health["fetches"] = int(health.get("fetches", 0)) + 1
        health["metrics"] = sorted(metrics)

    # -------------------------------------------------------------- commands

    def handle_command(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run one ``data``/``source`` verb; raise ``CommandError`` on bad input."""
        try:
            if name == "push":
                return self._push(args)
            if name == "get":
                return self._get(args)
            if name == "list":
                return self._list(args)
            if name == "source add":
                return self._source_add(args)
            if name == "source set":
                return self._source_set(args)
            if name == "source remove":
                return self._source_remove(args)
            if name == "source list":
                return self._source_list()
        except (SourceError, SystemSourceError) as exc:
            raise CommandError("invalid_input", str(exc)) from exc
        raise CommandError("invalid_input", f"unknown command {name!r}")

    def _push(self, args: dict[str, Any]) -> dict[str, Any]:
        topic = args.get("topic")
        if not isinstance(topic, str) or not topic.strip():
            raise CommandError("invalid_input", "push needs a 'topic'")
        if "value" not in args or args.get("value") is None:
            raise CommandError("invalid_input", "push needs a 'value'")
        ttl = args.get("ttl")
        ttl_s = int(ttl) if ttl is not None else self._default_ttl(topic)
        bus = getattr(self.ctx, "bus", None)
        if bus is not None:
            bus.publish(topic, args["value"], ttl_s=ttl_s)
        pushed = self._kv_get("pushed")
        pushed = dict(pushed) if isinstance(pushed, dict) else {}
        pushed[topic] = {"value": args["value"], "ttl_s": ttl_s}
        self._kv_set("pushed", pushed)
        return {"topic": topic, "value": args["value"], "ttl_s": ttl_s}

    def _get(self, args: dict[str, Any]) -> dict[str, Any]:
        topic = args.get("topic")
        if not isinstance(topic, str) or not topic.strip():
            raise CommandError("invalid_input", "get needs a 'topic'")
        bus = getattr(self.ctx, "bus", None)
        if bus is None:
            raise CommandError("internal", "this instance has no bus")
        sample = bus.get(topic)
        return {
            "topic": topic,
            "value": sample.value,
            "ts": sample.ts,
            "freshness": str(sample.freshness),
        }

    def _list(self, args: dict[str, Any]) -> dict[str, Any]:
        bus = getattr(self.ctx, "bus", None)
        prefix = str(args.get("prefix") or "")
        topics = bus.topics(prefix) if bus is not None else []
        return {"topics": topics}

    def _source_add(self, args: dict[str, Any]) -> dict[str, Any]:
        name = args.get("id")
        if not isinstance(name, str) or not name.strip():
            raise CommandError("invalid_input", "source add needs an 'id'")
        kind = str(args.get("type") or "pushed")
        entry = self._normalise(
            {
                "type": kind,
                "interval_s": args.get("interval"),
                "settings": dict(args.get("settings") or {}),
            }
        )
        self._validate_entry(name, entry)
        with self._lock:
            self._sources[name] = entry
            self._persist()
            self._schedule(name)
        return self._describe_source(name)

    def _source_set(self, args: dict[str, Any]) -> dict[str, Any]:
        name = args.get("id")
        if not isinstance(name, str) or not name.strip():
            raise CommandError("invalid_input", "source set needs an 'id'")
        with self._lock:
            current = self._sources.get(name)
            if current is None:
                raise CommandError("not_found", f"no source named {name!r}")
            merged = dict(current)
            if args.get("type") is not None:
                merged["type"] = str(args["type"])
            if args.get("interval") is not None:
                merged["interval_s"] = int(args["interval"])
            if args.get("settings") is not None:
                merged["settings"] = dict(args["settings"])
            entry = self._normalise(merged)
            self._validate_entry(name, entry)
            self._sources[name] = entry
            self._persist()
            self._schedule(name)
        return self._describe_source(name)

    def _source_remove(self, args: dict[str, Any]) -> dict[str, Any]:
        name = args.get("id")
        if not isinstance(name, str) or not name.strip():
            raise CommandError("invalid_input", "source remove needs an 'id'")
        with self._lock:
            if name not in self._sources:
                raise CommandError("not_found", f"no source named {name!r}")
            self._sources.pop(name, None)
            self._health.pop(name, None)
            task = self._tasks.pop(name, None)
            if task is not None:
                task.cancel()
            self._persist()
        return {"id": name, "removed": True}

    def _source_list(self) -> dict[str, Any]:
        with self._lock:
            return {"sources": [self._describe_source(name) for name in sorted(self._sources)]}

    def describe_state(self) -> dict[str, Any]:
        """Per-source health for ``status`` (never a secret)."""
        with self._lock:
            sources = {name: self._describe_source(name) for name in sorted(self._sources)}
            pushed = sorted((self._kv_get("pushed") or {}).keys())
        return {"sources": sources, "pushed": pushed}

    # -------------------------------------------------------------- internals

    def _http(self) -> Any:
        http = getattr(self.ctx, "http", None)
        if http is None:
            raise SourceError("this instance has no http helper")
        return http

    def _redact(self, exc: BaseException) -> str:
        http = getattr(self.ctx, "http", None)
        message = str(exc) or type(exc).__name__
        if http is not None and hasattr(http, "redact"):
            return str(http.redact(message))
        return message

    def _wall(self) -> float:
        clock = getattr(self.ctx, "clock", None)
        if clock is not None:
            return float(clock.time())
        return 0.0

    def _clock(self) -> Any:
        """The runner's clock, so a source's rate math uses the same time as the frames."""
        clock = getattr(self.ctx, "clock", None)
        return clock if clock is not None else SystemClock()

    def _normalise(self, entry: Any) -> dict[str, Any]:
        """A source entry with defaults filled in for its type."""
        raw = dict(entry) if isinstance(entry, dict) else {}
        kind = str(raw.get("type") or "pushed")
        if kind not in ("pushed", "rss", "http_json", "system"):
            raise SourceError(f"unknown source type {kind!r}")
        plugin = SOURCE_TYPES.get(kind)
        settings = dict(raw.get("settings") or {})
        if kind == "pushed":
            default_interval, floor = PUSH_TTL_DEFAULT, _TIMEOUT_FLOOR_S
        elif kind == "system":
            default_interval = SYSTEM_DEFAULT_INTERVAL_S
            floor = SYSTEM_INTERVAL_FLOOR_S
        else:
            default_interval, floor = JSON_INTERVAL_FLOOR_S, JSON_INTERVAL_FLOOR_S
        explicit = raw.get("interval_s")
        if explicit is None and kind == "system" and settings.get("interval_s") is not None:
            # ``interval_s`` may be given either way round for a system source.
            explicit = settings.get("interval_s")
        interval = default_interval if explicit is None else int(explicit)
        if interval < floor:
            raise SourceError(
                f"source 'interval_s' is {interval}; the floor for a {kind} source is {floor}"
            )
        if kind == "pushed" and "ttl_s" not in settings:
            settings["ttl_s"] = PUSH_TTL_DEFAULT
        if plugin is not None:
            settings.setdefault("interval_s", interval)
        return {"type": kind, "interval_s": interval, "settings": settings}

    def _validate_entry(self, name: str, entry: dict[str, Any]) -> None:
        plugin = SOURCE_TYPES.get(str(entry["type"]))
        if plugin is None:
            return
        plugin(name, entry["settings"])  # raises SourceError on bad settings

    def _schedule(self, name: str) -> None:
        """(Re)register one source's fetch on the runner's scheduler."""
        task = self._tasks.pop(name, None)
        if task is not None:
            try:
                task.cancel()
            except Exception:  # pragma: no cover - a cancel must never raise
                pass
        entry = self._sources.get(name)
        if entry is None or entry["type"] == "pushed":
            return
        if getattr(self.ctx, "spawn_handler", None) is None:
            # The SDK harness has no scheduler; the source is fetched by the
            # runner's loop instead (or by a test calling ``fetch`` directly).
            return
        timeout_s = entry["settings"].get("timeout_s") or TIMEOUT_MAX_S
        try:
            self._tasks[name] = self.ctx.spawn(
                lambda name=name: self._refresh(name),
                interval_s=float(entry["interval_s"]),
                timeout_s=float(timeout_s),
                name=f"sources-{name}",
            )
        except Exception as exc:  # a scheduler that is already stopped
            self._redact(exc)

    def _plan(self) -> None:
        for name in list(self._sources):
            self._schedule(name)

    def _persist(self) -> None:
        self._kv_set("sources", dict(self._sources))

    def _describe_source(self, name: str) -> dict[str, Any]:
        entry = dict(self._sources.get(name) or {})
        health = dict(self._health.get(name) or {})
        return {
            "id": name,
            "type": entry.get("type"),
            "interval_s": entry.get("interval_s"),
            "last_fetch": health.get("last_fetch"),
            "last_error": health.get("last_error"),
            "fetches": int(health.get("fetches", 0)),
            "metrics": list(health.get("metrics") or []),
        }

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

    def _default_ttl(self, topic: str) -> int:
        """The ``ttl_s`` of the ``pushed`` source whose name matches the topic."""
        with self._lock:
            for name, entry in self._sources.items():
                if entry.get("type") == "pushed" and topic in (name, f"push.{name}"):
                    return int((entry.get("settings") or {}).get("ttl_s") or PUSH_TTL_DEFAULT)
        return PUSH_TTL_DEFAULT
