"""The built-in ``gauges`` plugin: one to six gauges in a region (T099, US4).

Implements FR-031 to FR-036 and the ``gauges`` section of
``contracts/builtin-plugins.md``: a grid sized to the gauge count (1x1 up to
3x2), ``dial`` / ``bar`` / ``numeric`` / ``sparkline`` styles, colour thresholds,
and an explicit stale state driven by the gauge's own ``stale_after_s``.

Where the pieces live:

* :mod:`smart_panel.plugins.gauges.model` — the Gauge entity, the 1..6 limit, the
  grid, thresholds and value formatting;
* :mod:`smart_panel.plugins.gauges.aggregation` — the bounded sample ring and the
  five aggregations over a monotonic-clock window;
* :mod:`smart_panel.plugins.gauges.render` — the grid, the styles and the
  per-gauge layer cache.

The plugin itself owns three pieces of runtime state (all in memory,
``data-model.md``: "Gauge samples ... memory only"):

* the declared gauges, from the ``gauges`` setting; ``add``/``set``/``remove``
  change them live (a plugin may not write the config file, so a settings change
  is the only way to persist them);
* one sample buffer per gauge, fed from the bus inside ``render`` — never by a
  background fetch, so a slow source cannot stall a frame;
* the arrival timestamps per gauge, from which the default staleness limit is
  derived (FR-032: the refresh interval belongs to the data source, so the gauge
  observes it and uses 3x it).

Only ``smart_panel.sdk``, ``PIL`` and this package are imported (FR-043).
"""

from __future__ import annotations

import threading
from collections import deque
from itertools import pairwise
from typing import Any

from smart_panel.plugins.gauges.aggregation import (
    AGGREGATIONS,
    WINDOW_MAX_S,
    WINDOW_MIN_S,
    SampleBuffer,
)
from smart_panel.plugins.gauges.model import (
    DEFAULT_CRIT_COLOR,
    DEFAULT_RANGE,
    DEFAULT_WARN_COLOR,
    MAX_GAUGES,
    NO_DATA_TEXT,
    STYLES,
    Gauge,
    GaugeError,
    GaugeSet,
)
from smart_panel.plugins.gauges.render import GaugeReader, GaugeRenderer
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

__all__ = ["DEFAULT_SOURCE_ID", "GaugesPlugin"]

# The CLI verb group for this plugin is ``gauge`` (``cli.main.PLUGIN_GROUP_ALIASES``).
DEFAULT_SOURCE_ID = "sys"
_FLOOR_PX = 12
_ARRIVAL_WINDOW = 8


def _gauge_fields() -> tuple[Setting, ...]:
    """The arguments shared by ``add`` and ``set`` (``set`` leaves out what it is not given)."""
    return (
        Setting(name="id", type="str", default="", description="Gauge id (unique in the instance)"),
        Setting(name="label", type="str", default=None, description="Label, at most 24 characters"),
        Setting(name="topic", type="str", default=None, description="Bus topic the gauge reads"),
        Setting(
            name="aggregation",
            type="enum",
            default=None,
            choices=list(AGGREGATIONS),
            description="Aggregation over the window",
        ),
        Setting(
            name="window",
            type="int",
            default=None,
            min=WINDOW_MIN_S,
            max=WINDOW_MAX_S,
            description=f"Aggregation window in seconds ({WINDOW_MIN_S}..{WINDOW_MAX_S})",
        ),
        Setting(
            name="style",
            type="enum",
            default=None,
            choices=list(STYLES),
            description="How the gauge is drawn",
        ),
        Setting(name="unit", type="str", default=None, description="Unit, at most 6 characters"),
        Setting(name="min", type="float", default=None, description="Bottom of the value range"),
        Setting(name="max", type="float", default=None, description="Top of the value range"),
        Setting(
            name="warn",
            type="float",
            default=None,
            description=f"Value at which the gauge turns {DEFAULT_WARN_COLOR}",
        ),
        Setting(
            name="crit",
            type="float",
            default=None,
            description=f"Value at which the gauge turns {DEFAULT_CRIT_COLOR}",
        ),
        Setting(
            name="stale_after_s",
            type="int",
            default=None,
            min=1,
            description=(
                "Seconds without a sample before the gauge shows 'no data' "
                "(default 3x the source interval)"
            ),
        ),
    )


_SETTINGS: tuple[Setting, ...] = (
    Setting(
        name="gauges",
        type="list",
        default=[],
        description="Gauges: 1..6, each {id, label, topic, aggregation, window_s, style, ...}",
        item=Setting(name="gauge", type="object", default={}),
    ),
    Setting(
        name="layout",
        type="str",
        default="auto",
        description='Grid: "auto" or a JSON {"cols": C, "rows": R}',
    ),
    Setting(name="bg", type="color", default="#101418", description="Background colour"),
    Setting(
        name="font",
        type="str",
        default="DejaVuSans-Bold",
        description="Bundled font name or a path to a TTF",
    ),
    Setting(
        name="value_size",
        type="int",
        default=72,
        description="Value text size in pixels (never below frame.min_text_px)",
        min=_FLOOR_PX,
        max=512,
    ),
    Setting(
        name="label_size",
        type="int",
        default=28,
        description="Label text size in pixels (never below frame.min_text_px)",
        min=_FLOOR_PX,
        max=512,
    ),
)

_COMMANDS: tuple[Command, ...] = (
    Command(
        name="add",
        summary="Add a gauge",
        args=_gauge_fields(),
    ),
    Command(
        name="set",
        summary="Change one field of a gauge",
        args=_gauge_fields(),
    ),
    Command(
        name="remove",
        summary="Remove a gauge",
        args=(Setting(name="id", type="str", default="", description="Gauge id"),),
    ),
    Command(
        name="list",
        summary="List the gauges and their current values",
        args=(),
    ),
    Command(
        name="push",
        summary="Publish a value on push.<id> for a gauge that reads it",
        args=(
            Setting(name="id", type="str", default="", description="Gauge id"),
            Setting(name="value", type="float", default=None, description="Numeric value"),
        ),
        spoolable=True,
    ),
)


class GaugesPlugin(Plugin):
    """Draw the configured gauges from the bus, aggregating each over its window."""

    manifest = Manifest(
        name="gauges",
        version="0.1.0",
        summary="One to six gauges: aggregation windows, thresholds and four styles",
        capabilities=frozenset({"region"}),
        settings=_SETTINGS,
        commands=_COMMANDS,
    )

    def __init__(self, ctx: Any, settings: dict[str, Any]) -> None:
        super().__init__(ctx, settings)
        self._lock = threading.RLock()
        self._renderer = GaugeRenderer()
        self._gauges = self._parse_lenient(self.settings.get("gauges"))
        self._buffers: dict[str, SampleBuffer] = {}
        self._arrivals: dict[str, deque[float]] = {}
        self._seen: dict[str, float] = {}
        self._readings: dict[str, GaugeReader] = {}
        self._subscriptions: dict[str, Any] = {}

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Subscribe to every configured gauge's topic."""
        with self._lock:
            self._subscribe()

    def stop(self) -> None:
        """Drop the subscriptions and the render cache (must return within 2 s)."""
        with self._lock:
            self._unsubscribe()
        self._renderer.forget()

    def on_settings_changed(self, settings: dict[str, Any]) -> None:
        """Adopt a validated settings change, refusing an unusable gauge list."""
        try:
            gauges = GaugeSet.from_settings(settings.get("gauges"))
        except GaugeError as exc:
            raise SettingsRejected(str(exc), setting="gauges") from exc
        self.settings = dict(settings)
        with self._lock:
            self._gauges = gauges
            wanted = set(gauges.ids())
            for gauge_id in list(self._buffers):
                if gauge_id not in wanted:
                    self._buffers.pop(gauge_id, None)
                    self._arrivals.pop(gauge_id, None)
                    self._seen.pop(gauge_id, None)
                    self._readings.pop(gauge_id, None)
            self._subscribe()
            self._renderer.forget()
        self._touch()

    # --------------------------------------------------------------- render

    def render(self, frame: Any) -> Any:
        """Draw the gauge grid for the bus's current values (exactly ``frame.size``)."""
        with self._lock:
            gauges = list(self._gauges)
            readings = [self._read(gauge, frame) for gauge in gauges]
            self._readings = {
                gauge.id: reading for gauge, reading in zip(gauges, readings, strict=True)
            }
            image = self._renderer.render(frame, gauges, readings, self.settings)
        return RenderImage(image)

    # -------------------------------------------------------------- commands

    def handle_command(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run one ``gauge`` verb; raise ``CommandError`` for expected failures."""
        try:
            if name == "add":
                return self._add(args)
            if name == "set":
                return self._set(args)
            if name == "remove":
                return self._remove(args)
            if name == "list":
                return self._list()
            if name == "push":
                return self._push(args)
        except GaugeError as exc:
            raise CommandError("invalid_input", str(exc)) from exc
        raise CommandError("invalid_input", f"unknown command {name!r}")

    def describe_state(self) -> dict[str, Any]:
        """Per-gauge health for ``status`` (never a secret)."""
        with self._lock:
            clock = getattr(self.ctx, "clock", None)
            wall = float(clock.time()) if clock is not None and hasattr(clock, "time") else 0.0
            bus = getattr(self.ctx, "bus", None)
            entries: list[dict[str, Any]] = []
            for gauge in self._gauges:
                reader = self._readings.get(gauge.id)
                sample = (
                    bus.get(gauge.topic) if bus is not None and hasattr(bus, "get") else None
                )
                freshness = str(sample.freshness) if sample is not None else "no_data"
                timestamp = (
                    float(sample.ts) if sample is not None and sample.ts is not None else None
                )
                value = reader.value if reader is not None else None
                stale = (
                    freshness != "fresh"
                    or value is None
                    or (
                        timestamp is not None
                        and wall - timestamp > gauge.stale_after(self._observed_interval(gauge.id))
                    )
                )
                entries.append(
                    {
                        "id": gauge.id,
                        "label": gauge.label,
                        "topic": gauge.topic,
                        "aggregation": gauge.aggregation,
                        "window_s": gauge.window_s,
                        "style": gauge.style,
                        "unit": gauge.unit,
                        "thresholds": [threshold.to_dict() for threshold in gauge.thresholds],
                        "value": value,
                        "shown": NO_DATA_TEXT if stale else gauge.format_value(value),
                        "freshness": freshness,
                        "stale": stale,
                    }
                )
        return {"count": len(entries), "max": MAX_GAUGES, "gauges": entries}

    # --------------------------------------------------------------- internals

    def _read(self, gauge: Gauge, frame: Any) -> GaugeReader:
        """Aggregate one gauge from the bus for this frame (called on the render thread)."""
        bus = getattr(frame, "bus", None)
        sample = bus.get(gauge.topic) if bus is not None and hasattr(bus, "get") else None
        now = float(getattr(frame, "now", 0.0) or 0.0)
        wall = float(getattr(frame, "wall", 0.0) or 0.0)

        buffer = self._buffers.get(gauge.id)
        if buffer is None:
            buffer = SampleBuffer()
            self._buffers[gauge.id] = buffer
        value = sample.value if sample is not None else None
        freshness = str(sample.freshness) if sample is not None else "no_data"
        timestamp = (
            float(sample.ts) if sample is not None and sample.ts is not None else None
        )
        if (
            timestamp is not None
            and not isinstance(value, bool)
            and isinstance(value, (int, float))
            and self._seen.get(gauge.id) != timestamp
        ):
            # A new sample: buffer it and remember when it arrived (the observed
            # interval is what the default stale_after_s is 3x of).
            self._seen[gauge.id] = timestamp
            buffer.add(now, float(value))
            self._arrivals.setdefault(gauge.id, deque(maxlen=_ARRIVAL_WINDOW)).append(timestamp)

        aggregated = buffer.aggregate(gauge.aggregation, gauge.window_s, now)
        history = (
            tuple(buffer.window_values(gauge.window_s, now))
            if gauge.style == "sparkline"
            else ()
        )
        stale = (
            freshness != "fresh"
            or aggregated is None
            or (
                timestamp is not None
                and wall - timestamp > gauge.stale_after(self._observed_interval(gauge.id))
            )
        )
        return GaugeReader(value=aggregated, stale=stale, history=history)

    def _observed_interval(self, gauge_id: str) -> float | None:
        """The source's refresh interval as observed from this gauge's samples."""
        arrivals = self._arrivals.get(gauge_id)
        if not arrivals or len(arrivals) < 2:
            return None
        moments = list(arrivals)
        gaps = sorted(
            later - earlier
            for earlier, later in pairwise(moments)
            if later > earlier
        )
        if not gaps:
            return None
        return gaps[len(gaps) // 2]

    # ---------------------------------------------------------------- verbs

    def _add(self, args: dict[str, Any]) -> dict[str, Any]:
        gauge = Gauge.from_dict(
            {
                "id": args.get("id"),
                "label": args.get("label"),
                "topic": args.get("topic"),
                "aggregation": args.get("aggregation"),
                "window_s": args.get("window"),
                "unit": args.get("unit"),
                "style": args.get("style"),
                "range": self._range_from(args, None),
                "thresholds": self._thresholds_from(args, None),
                "stale_after_s": args.get("stale_after_s"),
            }
        )
        with self._lock:
            self._gauges.add(gauge)
            self._buffers.setdefault(gauge.id, SampleBuffer())
            self._subscribe()
        self._touch()
        return {**gauge.to_dict(), "count": len(self._gauges)}

    def _set(self, args: dict[str, Any]) -> dict[str, Any]:
        gauge_id = str(args.get("id") or "").strip()
        with self._lock:
            current = self._gauges.get(gauge_id)
            if current is None:
                raise CommandError("not_found", f"no gauge named {gauge_id!r}")
            raw = current.to_dict()
            for key, source in (
                ("label", "label"),
                ("topic", "topic"),
                ("aggregation", "aggregation"),
                ("window_s", "window"),
                ("unit", "unit"),
                ("style", "style"),
            ):
                if args.get(source) is not None:
                    raw[key] = args[source]
            raw["range"] = self._range_from(args, current.range)
            raw["thresholds"] = self._thresholds_from(args, current)
            if args.get("stale_after_s") is not None:
                raw["stale_after_s"] = args["stale_after_s"]
            gauge = Gauge.from_dict(raw)
            self._gauges.replace(gauge_id, gauge)
            if gauge.id != gauge_id:
                self._buffers[gauge.id] = self._buffers.pop(gauge_id, SampleBuffer())
                self._seen.pop(gauge_id, None)
                self._arrivals.pop(gauge_id, None)
                self._readings.pop(gauge_id, None)
            self._subscribe()
        self._touch()
        return {**gauge.to_dict(), "count": len(self._gauges)}

    def _remove(self, args: dict[str, Any]) -> dict[str, Any]:
        gauge_id = str(args.get("id") or "").strip()
        with self._lock:
            if self._gauges.get(gauge_id) is None:
                raise CommandError("not_found", f"no gauge named {gauge_id!r}")
            self._gauges.remove(gauge_id)
            self._buffers.pop(gauge_id, None)
            self._seen.pop(gauge_id, None)
            self._arrivals.pop(gauge_id, None)
            self._readings.pop(gauge_id, None)
            self._unsubscribe(gauge_id)
        self._touch()
        return {"id": gauge_id, "removed": True, "count": len(self._gauges)}

    def _list(self) -> dict[str, Any]:
        with self._lock:
            return {
                "gauges": self._gauges.to_list(),
                "count": len(self._gauges),
                "max": MAX_GAUGES,
            }

    def _push(self, args: dict[str, Any]) -> dict[str, Any]:
        gauge_id = str(args.get("id") or "").strip()
        gauge = self._gauges.get(gauge_id)
        if gauge is None:
            raise CommandError("not_found", f"no gauge named {gauge_id!r}")
        value = args.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CommandError("invalid_input", f"push needs a numeric VALUE, got {value!r}")
        bus = getattr(self.ctx, "bus", None)
        if bus is None:
            raise CommandError("internal", "this instance has no bus")
        topic = f"push.{gauge_id}"
        # No bus TTL: the gauge's own stale_after_s is what limits it (FR-032).
        bus.publish(topic, float(value))
        if gauge.topic != topic:
            log = getattr(self.ctx, "log", None)
            if log is not None:
                log.warning("gauge_push_topic_mismatch", gauge=gauge_id, topic=gauge.topic)
        return {"id": gauge_id, "topic": topic, "value": float(value)}

    # ----------------------------------------------------------------- helpers

    def _range_from(self, args: dict[str, Any], current: Any) -> list[float]:
        """The gauge's range, from ``--min``/``--max`` or the current one."""
        low, high = current if current is not None else DEFAULT_RANGE
        if args.get("min") is not None:
            low = args["min"]
        if args.get("max") is not None:
            high = args["max"]
        return [low, high]

    def _thresholds_from(self, args: dict[str, Any], current: Gauge | None) -> list[Any]:
        """The ``{at, color}`` list, from ``--warn``/``--crit`` or the current one."""
        warn, crit = args.get("warn"), args.get("crit")
        if warn is None and crit is None:
            return [threshold.to_dict() for threshold in current.thresholds] if current else []
        entries: list[dict[str, Any]] = []
        if warn is not None:
            entries.append({"at": warn, "color": DEFAULT_WARN_COLOR})
        if crit is not None:
            entries.append({"at": crit, "color": DEFAULT_CRIT_COLOR})
        return entries

    def _parse_lenient(self, raw: Any) -> GaugeSet:
        """Build the gauge set from settings, skipping entries that do not conform."""
        gauges = GaugeSet()
        for entry in raw if isinstance(raw, (list, tuple)) else []:
            try:
                gauges.add(Gauge.from_dict(entry))
            except GaugeError as exc:
                log = getattr(self.ctx, "log", None)
                if log is not None:
                    log.warning("gauge_rejected", error=str(exc))
        return gauges

    def _subscribe(self) -> None:
        """(Re)subscribe to every gauge's topic (calls ``ctx.request_render`` on change)."""
        bus = getattr(self.ctx, "bus", None)
        self._unsubscribe()
        if bus is None or not hasattr(bus, "subscribe"):
            return
        for gauge in self._gauges:
            try:
                self._subscriptions[gauge.id] = bus.subscribe(gauge.topic, self._on_topic)
            except Exception as exc:  # a bus that refuses a subscription is not fatal
                log = getattr(self.ctx, "log", None)
                if log is not None:
                    log.warning("gauge_subscribe_failed", gauge=gauge.id, error=str(exc))

    def _unsubscribe(self, gauge_id: str | None = None) -> None:
        if gauge_id is None:
            subscription = None
            for handle in self._subscriptions.values():
                try:
                    handle.cancel()
                except Exception:  # pragma: no cover - a cancel must never raise
                    pass
            self._subscriptions = {}
            return
        subscription = self._subscriptions.pop(gauge_id, None)
        if subscription is not None:
            try:
                subscription.cancel()
            except Exception:  # pragma: no cover - a cancel must never raise
                pass

    def _on_topic(self, topic: str, sample: Any) -> None:
        """A subscribed topic changed; the runner redraws (T099)."""
        self._touch()

    def _touch(self) -> None:
        request_render = getattr(self.ctx, "request_render", None)
        if request_render is not None:
            request_render()
