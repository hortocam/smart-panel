"""The built-in ``sidebar`` plugin: app icons with count chips (T108, US5).

Implements FR-037 to FR-041 and the ``sidebar`` section of
``contracts/builtin-plugins.md``: a region of at most six entries (including the
built-in alert entry), each an icon with an optional label and a count chip;
``hide_when_zero``, ``999+`` abbreviation and a ``?`` stale/unknown state; and a
built-in alert entry bound to the ``alerts.missed`` topic that takes the colour of
the highest missed severity and hides at zero.

Where the pieces live:

* :mod:`smart_panel.plugins.sidebar.model` — the entry, the 6-entry limit, the
  label/``max_display``/``stale_after_s`` rules and the row height;
* :mod:`smart_panel.plugins.sidebar.render` — the row, the chip and the brief
  change highlight.

The plugin owns one piece of runtime state: the declared entries, from the
``entries`` setting. ``entry add``/``set``/``remove`` change them live (a plugin may
not write the config file, so a settings change is the only way to persist them).
Counts are not owned here at all — the region plugin reads them from the bus every
frame, so a value pushed by ``count set``, by ``data push`` or published by a
configured ``http_json`` source on its own refresh interval (FR-039) all arrive the
same way and a slow source can never stall a frame.

The implicit alert entry uses only the bus: it reads ``alerts.missed``
(``{count, max_severity}``), so the sidebar does not depend on the ``alerts``
plugin, and an instance with no alerts simply shows a zero count.

Only ``smart_panel.sdk``, ``PIL`` and this package are imported (FR-043).
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from smart_panel.plugins.sidebar.model import (
    ALERT_ENTRY_ID,
    ALERT_TOPIC,
    BUNDLED_ICONS,
    DEFAULT_STALE_TTL_S,
    MAX_ENTRIES,
    SidebarEntry,
    SidebarEntrySet,
    SidebarError,
    alert_entry,
)
from smart_panel.plugins.sidebar.render import (
    SidebarReading,
    SidebarRenderer,
    chip_text,
)
from smart_panel.sdk.plugin import (
    Command,
    CommandError,
    Manifest,
    Plugin,
    SettingsRejected,
)
from smart_panel.sdk.settings import Setting

__all__ = ["SidebarPlugin"]

# ``display.min_text_px`` lower bound; the render clamps to the frame's value.
_FLOOR_PX = 12
_INTEGER = re.compile(r"^[0-9]+$")


def _entry_fields() -> tuple[Setting, ...]:
    """The arguments shared by ``entry add`` and ``entry set``."""
    return (
        Setting(
            name="id",
            type="str",
            default="",
            description="Entry id (unique in the instance)",
        ),
        Setting(
            name="icon",
            type="str",
            default=None,
            description=f"A bundled icon name ({', '.join(BUNDLED_ICONS)}) or a PNG path",
        ),
        Setting(name="label", type="str", default=None, description="Label, at most 12 characters"),
        Setting(name="topic", type="str", default=None, description="Bus topic carrying the count"),
        Setting(
            name="hide_when_zero",
            type="bool",
            default=None,
            description="Hide the chip while the count is zero (default)",
        ),
        Setting(
            name="show_zero",
            type="bool",
            default=None,
            description="Show a zero count chip (turns hide_when_zero off)",
        ),
        Setting(
            name="max_display",
            type="int",
            default=None,
            min=1,
            description="Counts above this show as '999+'",
        ),
        Setting(
            name="stale_after_s",
            type="int",
            default=None,
            min=1,
            description="Seconds before the chip reads '?' (default 300)",
        ),
    )


_SETTINGS: tuple[Setting, ...] = (
    Setting(
        name="entries",
        type="list",
        default=[],
        description=(
            f"Sidebar entries: at most {MAX_ENTRIES - 1}, plus the built-in alert "
            "entry; each {id, icon, label, topic, hide_when_zero, max_display, "
            "stale_after_s}"
        ),
        item=Setting(name="entry", type="object", default={}),
    ),
    Setting(name="bg", type="color", default="#0B0E11", description="Background colour"),
    Setting(name="chip_fg", type="color", default="#FFFFFF", description="Chip text colour"),
    Setting(name="chip_bg", type="color", default="#D62839", description="Chip background colour"),
    Setting(
        name="font",
        type="str",
        default="DejaVuSans",
        description="Bundled font name or a path to a TTF",
    ),
    Setting(
        name="label_size",
        type="int",
        default=24,
        description="Label text size in pixels (never below frame.min_text_px)",
        min=_FLOOR_PX,
        max=512,
    ),
    Setting(
        name="chip_size",
        type="int",
        default=24,
        description="Chip text size in pixels (never below frame.min_text_px)",
        min=_FLOOR_PX,
        max=512,
    ),
)

_COMMANDS: tuple[Command, ...] = (
    Command(name="entry add", summary="Add a sidebar entry", args=_entry_fields()),
    Command(name="entry set", summary="Change one field of a sidebar entry", args=_entry_fields()),
    Command(
        name="entry remove",
        summary="Remove a sidebar entry",
        args=(Setting(name="id", type="str", default="", description="Entry id"),),
    ),
    Command(name="entry list", summary="List the sidebar entries", args=()),
    Command(
        name="count set",
        summary="Set a count on an entry's topic (with a TTL)",
        args=(
            Setting(name="id", type="str", default="", description="Entry id"),
            Setting(name="value", type="str", default=None, description="Integer count"),
        ),
        spoolable=True,
    ),
)


class SidebarPlugin(Plugin):
    """Draw the configured entries and the built-in alert entry from the bus."""

    manifest = Manifest(
        name="sidebar",
        version="0.1.0",
        summary="A sidebar of app icons with count chips and a built-in alert entry",
        capabilities=frozenset({"region"}),
        settings=_SETTINGS,
        commands=_COMMANDS,
    )

    def __init__(self, ctx: Any, settings: dict[str, Any]) -> None:
        super().__init__(ctx, settings)
        self._lock = threading.RLock()
        self._renderer = SidebarRenderer()
        self._entries = self._parse_lenient(self.settings.get("entries"))
        self._subscriptions: dict[str, Any] = {}
        self._icon_warnings: list[dict[str, Any]] = []

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Subscribe to every entry's topic and to ``alerts.missed``."""
        with self._lock:
            self._subscribe()

    def stop(self) -> None:
        """Drop the subscriptions and the render cache (must return within 2 s)."""
        with self._lock:
            self._unsubscribe()
        self._renderer.forget()

    def on_settings_changed(self, settings: dict[str, Any]) -> None:
        """Adopt a validated settings change, refusing an unusable entry list."""
        try:
            entries = SidebarEntrySet.from_settings(settings.get("entries"))
        except SidebarError as exc:
            raise SettingsRejected(str(exc), setting="entries") from exc
        self.settings = dict(settings)
        with self._lock:
            self._entries = entries
            self._subscribe()
            self._renderer.forget()
        self._touch()

    # --------------------------------------------------------------- render

    def render(self, frame: Any) -> Any:
        """Draw the entries and the built-in alert entry (exactly ``frame.size``).

        The alert entry is implicit (FR-041), so it is appended to the configured
        entries here rather than stored in the entry set — it is always present,
        always last, and reads ``alerts.missed`` through the bus like any other
        entry.
        """
        with self._lock:
            entries = [*self._entries, alert_entry()]
        bus = getattr(frame, "bus", None)
        wall = float(getattr(frame, "wall", 0.0) or 0.0)
        readings = [self._read(entry, bus, wall) for entry in entries]
        result = self._renderer.render(frame, entries, readings, self.settings)
        # Rebuilt every render (not only when non-empty): an icon that resolves
        # again drops out of ``fallbacks``, and ``status`` must stop listing it.
        self._icon_warnings = [
            {"icon": name, "reason": reason}
            for name, reason in sorted(self._renderer.fallbacks.items())
        ]
        return result

    # -------------------------------------------------------------- commands

    def handle_command(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run one ``sidebar`` verb; raise ``CommandError`` for expected failures."""
        try:
            if name == "entry add":
                return self._entry_add(args)
            if name == "entry set":
                return self._entry_set(args)
            if name == "entry remove":
                return self._entry_remove(args)
            if name == "entry list":
                return self._entry_list()
            if name == "count set":
                return self._count_set(args)
        except SidebarError as exc:
            raise CommandError("invalid_input", str(exc)) from exc
        raise CommandError("invalid_input", f"unknown command {name!r}")

    def describe_state(self) -> dict[str, Any]:
        """Per-entry health for ``status`` (never a secret)."""
        bus = getattr(self.ctx, "bus", None)
        clock = getattr(self.ctx, "clock", None)
        wall = float(clock.time()) if clock is not None and hasattr(clock, "time") else 0.0
        with self._lock:
            entries = list(self._entries)
            alert = self._read(alert_entry(), bus, wall)
            rows = [
                {**entry.to_dict(), **self._reading_state(entry, self._read(entry, bus, wall))}
                for entry in entries
            ]
            warnings = list(self._icon_warnings)
        alert_entry_value = alert_entry()
        return {
            "count": len(entries),
            "max": MAX_ENTRIES,
            "entries": rows,
            "alert": {
                "id": ALERT_ENTRY_ID,
                "icon": alert_entry_value.icon,
                "topic": ALERT_TOPIC,
                **self._reading_state(alert_entry_value, alert),
            },
            "warnings": warnings,
        }

    # --------------------------------------------------------------- internals

    def _reading_state(self, entry: SidebarEntry, reading: SidebarReading) -> dict[str, Any]:
        return {
            "count": reading.count,
            "shown": chip_text(entry, reading),
            "stale": reading.stale,
            "max_severity": reading.severity,
        }

    def _read(self, entry: SidebarEntry, bus: Any, wall: float) -> SidebarReading:
        """One entry's count for this frame (the bus is read, never mutated)."""
        sample = bus.get(entry.topic) if bus is not None and hasattr(bus, "get") else None
        value = sample.value if sample is not None else None
        freshness = str(sample.freshness) if sample is not None else "no_data"
        if entry.id == ALERT_ENTRY_ID:
            return self._read_alert(value)
        count = _parse_count(value)
        stale = freshness != "fresh"
        if not stale and count is not None and entry.stale_after_s is not None:
            timestamp = sample.ts if sample is not None else None
            if timestamp is not None and wall - float(timestamp) > entry.stale_after_s:
                # The publisher may not have set a TTL; the entry's own limit then
                # decides when the count is no longer trusted (FR-040).
                stale = True
        return SidebarReading(count=count, stale=stale)

    @staticmethod
    def _read_alert(value: Any) -> SidebarReading:
        """The ``alerts.missed`` payload as a reading (unknown reads as zero)."""
        count = 0
        severity: str | None = None
        if isinstance(value, dict):
            parsed = _parse_count(value.get("count"))
            count = parsed or 0
            raw = value.get("max_severity")
            severity = raw if isinstance(raw, str) else None
        return SidebarReading(count=count, stale=False, severity=severity)

    # ---------------------------------------------------------------- verbs

    def _entry_add(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = SidebarEntry.from_dict(
            {
                "id": args.get("id"),
                "icon": args.get("icon"),
                "label": args.get("label"),
                "topic": args.get("topic"),
                "hide_when_zero": self._hide_from(args, None),
                "max_display": args.get("max_display"),
                "stale_after_s": args.get("stale_after_s"),
            }
        )
        self._check_icon(entry.icon)
        with self._lock:
            self._entries.add(entry)
            self._subscribe()
        self._touch()
        return {**entry.to_dict(), "count": len(self._entries)}

    def _entry_set(self, args: dict[str, Any]) -> dict[str, Any]:
        entry_id = str(args.get("id") or "").strip()
        with self._lock:
            current = self._entries.get(entry_id)
            if current is None:
                raise CommandError("not_found", f"no sidebar entry named {entry_id!r}")
            raw = current.to_dict()
            for key, source in (("icon", "icon"), ("label", "label"), ("topic", "topic")):
                if args.get(source) is not None:
                    raw[key] = args[source]
            raw["hide_when_zero"] = self._hide_from(args, current.hide_when_zero)
            if args.get("max_display") is not None:
                raw["max_display"] = args["max_display"]
            if args.get("stale_after_s") is not None:
                raw["stale_after_s"] = args["stale_after_s"]
            entry = SidebarEntry.from_dict(raw)
            self._check_icon(entry.icon)
            self._entries.replace(entry_id, entry)
            self._subscribe()
        self._touch()
        return {**entry.to_dict(), "count": len(self._entries)}

    def _entry_remove(self, args: dict[str, Any]) -> dict[str, Any]:
        entry_id = str(args.get("id") or "").strip()
        with self._lock:
            if self._entries.get(entry_id) is None:
                raise CommandError("not_found", f"no sidebar entry named {entry_id!r}")
            self._entries.remove(entry_id)
            self._unsubscribe(entry_id)
        self._touch()
        return {"id": entry_id, "removed": True, "count": len(self._entries)}

    def _entry_list(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entries": self._entries.to_list(),
                "count": len(self._entries),
                "max": MAX_ENTRIES,
                "alert": alert_entry().to_dict(),
            }

    def _count_set(self, args: dict[str, Any]) -> dict[str, Any]:
        entry_id = str(args.get("id") or "").strip()
        with self._lock:
            entry = self._entries.get(entry_id)
        if entry is None:
            raise CommandError("not_found", f"no sidebar entry named {entry_id!r}")
        count = _parse_count(args.get("value"))
        if count is None:
            raise CommandError(
                "invalid_input", f"count set needs an integer N, got {args.get('value')!r}"
            )
        bus = getattr(self.ctx, "bus", None)
        if bus is None:
            raise CommandError("internal", "this instance has no bus")
        ttl_s = float(entry.stale_after_s or DEFAULT_STALE_TTL_S)
        bus.publish(entry.topic, count, ttl_s=ttl_s)
        return {"id": entry.id, "topic": entry.topic, "value": count, "ttl_s": ttl_s}

    # ----------------------------------------------------------------- helpers

    def _check_icon(self, icon: str) -> None:
        """Refuse an icon that is neither bundled nor an existing file (FR-038)."""
        if icon in BUNDLED_ICONS:
            return
        if Path(icon).is_file():
            return
        raise SidebarError(
            f"unknown icon {icon!r}; expected a bundled name "
            f"({', '.join(BUNDLED_ICONS)}) or a path to an existing PNG"
        )

    @staticmethod
    def _hide_from(args: dict[str, Any], current: Any) -> bool:
        """``hide_when_zero`` after ``--hide-when-zero`` / ``--show-zero``."""
        if args.get("show_zero"):
            return False
        if args.get("hide_when_zero"):
            return True
        return True if current is None else bool(current)

    def _parse_lenient(self, raw: Any) -> SidebarEntrySet:
        """Build the entry set from settings, skipping entries that do not conform."""
        entries = SidebarEntrySet()
        for item in raw if isinstance(raw, (list, tuple)) else []:
            try:
                entries.add(SidebarEntry.from_dict(item))
            except SidebarError as exc:
                log = getattr(self.ctx, "log", None)
                if log is not None:
                    log.warning("sidebar_entry_rejected", error=str(exc))
        return entries

    def _subscribe(self) -> None:
        """(Re)subscribe to every entry's topic and to ``alerts.missed``."""
        bus = getattr(self.ctx, "bus", None)
        self._unsubscribe()
        if bus is None or not hasattr(bus, "subscribe"):
            return
        topics = [entry.topic for entry in self._entries]
        if ALERT_TOPIC not in topics:
            topics.append(ALERT_TOPIC)
        for topic in topics:
            try:
                self._subscriptions[topic] = bus.subscribe(topic, self._on_topic)
            except Exception as exc:  # a bus that refuses a subscription is not fatal
                log = getattr(self.ctx, "log", None)
                if log is not None:
                    log.warning("sidebar_subscribe_failed", topic=topic, error=str(exc))

    def _unsubscribe(self, topic: str | None = None) -> None:
        if topic is None:
            for handle in self._subscriptions.values():
                try:
                    handle.cancel()
                except Exception:  # pragma: no cover - a cancel must never raise
                    pass
            self._subscriptions = {}
            return
        handle = self._subscriptions.pop(topic, None)
        if handle is not None:
            try:
                handle.cancel()
            except Exception:  # pragma: no cover - a cancel must never raise
                pass

    def _on_topic(self, topic: str, sample: Any) -> None:
        """A subscribed topic changed; the runner redraws."""
        self._touch()

    def _touch(self) -> None:
        request_render = getattr(self.ctx, "request_render", None)
        if request_render is not None:
            request_render()


def _parse_count(value: Any) -> int | None:
    """``value`` as a non-negative count, or ``None`` when it is not an integer.

    A CLI value arrives as text; an ``http_json`` field is published as rendered
    text too, so ``"7"`` and ``7`` both mean seven (FR-039).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if not _INTEGER.match(text):
            return None
        number = int(text)
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    else:
        return None
    return number if number >= 0 else None
