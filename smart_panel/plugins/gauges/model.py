"""The gauge model: one gauge, the instance's gauge set and the grid (T097, US4).

Shapes come from ``data-model.md`` (Gauge) and ``contracts/builtin-plugins.md``
(``gauges``): ``id`` (unique in the instance), ``label`` (<= 24 chars), ``topic``,
``aggregation`` (``last|avg|min|max|sum``), ``window_s`` (10..86400, ignored for
``last``), ``unit`` (<= 6 chars), ``range`` (``[min, max]`` with ``min < max``),
``style`` (``dial|bar|numeric|sparkline``), ``thresholds`` (a list of
``{at, color}`` in ascending ``at`` and inside the range) and ``stale_after_s``.

``stale_after_s`` defaults to **3x the source's refresh interval** (FR-032: the
refresh interval belongs to the data source, so the gauge cannot be told it —
``Gauge.stale_after`` takes the interval the plugin observed from the samples
themselves, falling back to the ``system`` source's 2 s default until two
samples have arrived).

An instance holds **between 1 and 6 gauges** (FR-031): :class:`GaugeSet` refuses
a seventh with a message naming the maximum, and refuses a duplicate id. An
empty set is allowed at runtime — a region plugin must still paint its own
background (``contracts/builtin-plugins.md``, "a region plugin has no Inactive
empty state"), and the SDK conformance suite renders the plugin with no gauges.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from smart_panel.plugins.gauges.aggregation import (
    AGGREGATIONS,
    WINDOW_MAX_S,
    WINDOW_MIN_S,
)

__all__ = [
    "DEFAULT_CRIT_COLOR",
    "DEFAULT_RANGE",
    "DEFAULT_SOURCE_INTERVAL_S",
    "DEFAULT_WARN_COLOR",
    "DEFAULT_WINDOW_S",
    "GRID_FOR_COUNT",
    "LABEL_MAX_CHARS",
    "MAX_GAUGES",
    "MAX_GRID",
    "MIN_GAUGES",
    "NO_DATA_TEXT",
    "STALE_FACTOR",
    "STYLES",
    "UNIT_MAX_CHARS",
    "Gauge",
    "GaugeError",
    "GaugeSet",
    "Threshold",
    "grid_for",
]

MAX_GAUGES = 6
MIN_GAUGES = 1
LABEL_MAX_CHARS = 24
UNIT_MAX_CHARS = 6
MAX_GRID = 6

STYLES = ("dial", "bar", "numeric", "sparkline")
STALE_FACTOR = 3
DEFAULT_SOURCE_INTERVAL_S = 2.0
DEFAULT_WINDOW_S = 60
DEFAULT_RANGE = (0.0, 100.0)
NO_DATA_TEXT = "no data"

# The threshold colours ``gauge add --warn N --crit N`` uses; they match the
# alert palette in ``data-model.md`` so a warning looks the same everywhere.
DEFAULT_WARN_COLOR = "#E6A700"
DEFAULT_CRIT_COLOR = "#D62839"

# The grid sized to the gauge count (``contracts/builtin-plugins.md``: "1x1 to
# 3x2"). The gauge region is wide, so the columns fill first.
GRID_FOR_COUNT: dict[int, tuple[int, int]] = {
    1: (1, 1),
    2: (2, 1),
    3: (3, 1),
    4: (2, 2),
    5: (3, 2),
    6: (3, 2),
}

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_TOPIC_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class GaugeError(ValueError):
    """A gauge or the gauge set did not conform."""


@dataclass(frozen=True)
class Threshold:
    """One ``{at, color}`` colour threshold."""

    at: float
    color: str

    @classmethod
    def from_value(cls, raw: Any, label: str = "threshold") -> Threshold:
        """Build a threshold from a mapping or a bare number (with the default colour)."""
        if isinstance(raw, Mapping):
            at = _number(raw.get("at"), f"{label}.at")
            color = str(raw.get("color") or DEFAULT_WARN_COLOR)
        else:
            at = _number(raw, f"{label}.at")
            color = DEFAULT_WARN_COLOR
        return cls(at=at, color=color)

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape of the threshold."""
        return {"at": self.at, "color": self.color}


def _number(value: Any, what: str) -> float:
    """Return ``value`` as a finite float, refusing booleans and strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GaugeError(f"{what} must be a number, got {value!r}")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise GaugeError(f"{what} must be a finite number, got {value!r}")
    return number


def _text(value: Any, what: str, limit: int) -> str:
    """Return ``value`` as a string of at most ``limit`` characters."""
    if not isinstance(value, str):
        raise GaugeError(f"{what} must be a string, got {value!r}")
    if len(value) > limit:
        raise GaugeError(f"{what} must be at most {limit} characters, got {len(value)}")
    return value


@dataclass(frozen=True)
class Gauge:
    """One configured gauge."""

    id: str
    label: str
    topic: str
    aggregation: str = "last"
    window_s: int = DEFAULT_WINDOW_S
    unit: str = ""
    range: tuple[float, float] = DEFAULT_RANGE
    style: str = "numeric"
    thresholds: tuple[Threshold, ...] = ()
    stale_after_s: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.thresholds, tuple):
            object.__setattr__(self, "thresholds", tuple(self.thresholds))

    # ---------------------------------------------------------------- parsing

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Gauge:
        """Build and validate a gauge from a config, CLI or ``to_dict`` mapping.

        Raises:
            GaugeError: A field is missing or out of its documented range.
        """
        data = dict(raw or {})
        identifier = str(data.get("id") or "").strip()
        if not _ID_PATTERN.match(identifier):
            raise GaugeError(
                "a gauge needs an 'id' of 1-32 characters "
                "([A-Za-z0-9._-], starting alphanumeric)"
            )
        topic = str(data.get("topic") or "").strip()
        if not _TOPIC_PATTERN.match(topic):
            raise GaugeError(f"gauge {identifier!r} needs a 'topic' to read")
        label = _text(
            data.get("label") or identifier, f"gauge {identifier!r} label", LABEL_MAX_CHARS
        )
        unit = _text(data.get("unit") or "", f"gauge {identifier!r} unit", UNIT_MAX_CHARS)

        aggregation = str(data.get("aggregation") or "last").strip().lower()
        if aggregation not in AGGREGATIONS:
            raise GaugeError(
                f"gauge {identifier!r} has aggregation {aggregation!r}; "
                f"expected one of {list(AGGREGATIONS)}"
            )
        style = str(data.get("style") or "numeric").strip().lower()
        if style not in STYLES:
            raise GaugeError(
                f"gauge {identifier!r} has style {style!r}; expected one of {list(STYLES)}"
            )

        window = data.get("window_s")
        if window is None:
            window = DEFAULT_WINDOW_S
        if isinstance(window, bool) or not isinstance(window, (int, float)):
            raise GaugeError(f"gauge {identifier!r} window_s must be a number, got {window!r}")
        window_s = int(window)
        if window_s < WINDOW_MIN_S or window_s > WINDOW_MAX_S:
            raise GaugeError(
                f"gauge {identifier!r} window_s must be between {WINDOW_MIN_S} and "
                f"{WINDOW_MAX_S} seconds, got {window_s}"
            )

        low, high = _parse_range(data.get("range"), identifier)
        thresholds = _parse_thresholds(data.get("thresholds"), identifier, low, high)

        stale = data.get("stale_after_s")
        if stale is None:
            stale_after: int | None = None
        else:
            stale_after = int(_number(stale, f"gauge {identifier!r} stale_after_s"))
            if stale_after < 1:
                raise GaugeError(
                    f"gauge {identifier!r} stale_after_s must be at least 1, got {stale_after}"
                )

        return cls(
            id=identifier,
            label=label,
            topic=topic,
            aggregation=aggregation,
            window_s=window_s,
            unit=unit,
            range=(low, high),
            style=style,
            thresholds=thresholds,
            stale_after_s=stale_after,
        )

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape stored in the config and reported by ``gauge list``."""
        return {
            "id": self.id,
            "label": self.label,
            "topic": self.topic,
            "aggregation": self.aggregation,
            "window_s": self.window_s,
            "unit": self.unit,
            "range": [self.range[0], self.range[1]],
            "style": self.style,
            "thresholds": [threshold.to_dict() for threshold in self.thresholds],
            "stale_after_s": self.stale_after_s,
        }

    # ------------------------------------------------------------- behaviour

    def stale_after(self, source_interval_s: float | None = None) -> float:
        """The staleness limit in seconds (FR-036).

        An explicit ``stale_after_s`` always wins; otherwise it is 3x the source's
        refresh interval, and until the plugin has observed two samples that
        interval falls back to the ``system`` source's 2 s default.
        """
        if self.stale_after_s is not None:
            return float(self.stale_after_s)
        interval = float(source_interval_s) if source_interval_s else DEFAULT_SOURCE_INTERVAL_S
        if interval <= 0:
            interval = DEFAULT_SOURCE_INTERVAL_S
        return STALE_FACTOR * interval

    def threshold_color(self, value: float | None) -> str | None:
        """The colour of the highest threshold reached, or ``None`` for the default."""
        if value is None:
            return None
        reached = [threshold for threshold in self.thresholds if value >= threshold.at]
        if not reached:
            return None
        return reached[-1].color

    def fraction(self, value: float | None) -> float:
        """``value`` as a 0..1 position inside the gauge's range."""
        if value is None:
            return 0.0
        low, high = self.range
        if high <= low:
            return 0.0
        return min(1.0, max(0.0, (float(value) - low) / (high - low)))

    def format_value(self, value: float | None) -> str:
        """The displayed text: one decimal at most, with the unit, or ``no data``."""
        if value is None:
            return NO_DATA_TEXT
        text = f"{float(value):.1f}".rstrip("0").rstrip(".")
        if text in ("", "-0", "-"):
            text = "0"
        return f"{text}{self.unit}"


def _parse_range(raw: Any, identifier: str) -> tuple[float, float]:
    if raw is None:
        return DEFAULT_RANGE
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        values = list(raw)
        if len(values) != 2:
            raise GaugeError(
                f"gauge {identifier!r} range must be [min, max], got {list(raw)!r}"
            )
        low, high = _number(values[0], f"gauge {identifier!r} range min"), _number(
            values[1], f"gauge {identifier!r} range max"
        )
    elif isinstance(raw, Mapping):
        low = _number(raw.get("min"), f"gauge {identifier!r} range min")
        high = _number(raw.get("max"), f"gauge {identifier!r} range max")
    else:
        raise GaugeError(f"gauge {identifier!r} range must be [min, max], got {raw!r}")
    if low >= high:
        raise GaugeError(
            f"gauge {identifier!r} range must have min below max, got [{low}, {high}]"
        )
    return low, high


def _parse_thresholds(
    raw: Any, identifier: str, low: float, high: float
) -> tuple[Threshold, ...]:
    if raw in (None, []):
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise GaugeError(
            f"gauge {identifier!r} thresholds must be a list of {{at, color}} entries"
        )
    thresholds: list[Threshold] = []
    for index, entry in enumerate(raw):
        threshold = Threshold.from_value(entry, f"gauge {identifier!r} threshold[{index}]")
        if threshold.at < low or threshold.at > high:
            raise GaugeError(
                f"gauge {identifier!r} threshold {threshold.at} is outside its range "
                f"[{low}, {high}]"
            )
        if thresholds and threshold.at <= thresholds[-1].at:
            raise GaugeError(
                f"gauge {identifier!r} thresholds must ascend, got {threshold.at} after "
                f"{thresholds[-1].at}"
            )
        thresholds.append(threshold)
    return tuple(thresholds)


def _parse_layout(layout: Any) -> tuple[int, int] | None:
    """``None`` for ``auto``, else the explicit ``(cols, rows)``."""
    if layout is None:
        return None
    if isinstance(layout, Mapping):
        data: Any = layout
    elif isinstance(layout, str):
        text = layout.strip()
        if not text or text.lower() == "auto":
            return None
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise GaugeError(
                f"layout must be 'auto' or a {{cols, rows}} object, got {layout!r}"
            ) from exc
    else:
        raise GaugeError(f"layout must be 'auto' or a {{cols, rows}} object, got {layout!r}")
    if not isinstance(data, Mapping):
        raise GaugeError(f"layout must be 'auto' or a {{cols, rows}} object, got {layout!r}")
    columns = data.get("cols")
    rows = data.get("rows")
    if (
        isinstance(columns, bool)
        or isinstance(rows, bool)
        or not isinstance(columns, int)
        or not isinstance(rows, int)
    ):
        raise GaugeError(f"layout needs integer 'cols' and 'rows', got {dict(data)!r}")
    if columns < 1 or rows < 1 or columns > MAX_GRID or rows > MAX_GRID:
        raise GaugeError(
            f"layout {columns}x{rows} is outside 1x1..{MAX_GRID}x{MAX_GRID}"
        )
    return int(columns), int(rows)


def grid_for(count: int, layout: Any = "auto") -> tuple[int, int]:
    """The ``(columns, rows)`` grid for ``count`` gauges.

    ``layout`` is ``"auto"`` (the grid sized to the count) or an explicit
    ``{cols, rows}`` mapping, or its JSON text.

    Raises:
        GaugeError: More than six gauges, or a layout with no room for them.
    """
    explicit = _parse_layout(layout)
    total = int(count)
    if explicit is None:
        if total <= 0:
            return GRID_FOR_COUNT[1]
        if total > MAX_GAUGES:
            raise GaugeError(
                f"a gauge instance holds at most {MAX_GAUGES} gauges; the maximum is "
                f"{MAX_GAUGES}, got {total}"
            )
        return GRID_FOR_COUNT[total]
    columns, rows = explicit
    if total > columns * rows:
        raise GaugeError(
            f"the layout {columns}x{rows} has room for {columns * rows} gauges, "
            f"but {total} are configured"
        )
    return columns, rows


@dataclass
class GaugeSet:
    """The gauges of one instance, with the 1..6 limit enforced on every change."""

    gauges: list[Gauge] = field(default_factory=list)

    @classmethod
    def from_settings(cls, raw: Any) -> GaugeSet:
        """Build the set from a ``gauges`` setting value.

        Raises:
            GaugeError: An entry is invalid, an id repeats, or there are more than six.
        """
        if raw is None:
            return cls()
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise GaugeError(f"gauges must be a list of gauge objects, got {raw!r}")
        if len(raw) > MAX_GAUGES:
            raise GaugeError(
                f"a gauge instance holds at most {MAX_GAUGES} gauges; the maximum is "
                f"{MAX_GAUGES}, got {len(raw)}"
            )
        parsed = cls()
        for entry in raw:
            if not isinstance(entry, Mapping):
                raise GaugeError(f"every gauge must be an object, got {entry!r}")
            parsed.add(Gauge.from_dict(entry))
        return parsed

    def __len__(self) -> int:
        """The number of gauges."""
        return len(self.gauges)

    def __iter__(self):
        """Iterate the gauges in configuration order."""
        return iter(self.gauges)

    def ids(self) -> list[str]:
        """The gauge ids in configuration order."""
        return [gauge.id for gauge in self.gauges]

    def get(self, gauge_id: str) -> Gauge | None:
        """The gauge with ``gauge_id``, or ``None``."""
        for gauge in self.gauges:
            if gauge.id == gauge_id:
                return gauge
        return None

    def add(self, gauge: Gauge) -> Gauge:
        """Append ``gauge``, refusing a duplicate id or a seventh gauge."""
        if self.get(gauge.id) is not None:
            raise GaugeError(f"gauge {gauge.id!r} already exists")
        if len(self.gauges) >= MAX_GAUGES:
            raise GaugeError(
                f"a gauge instance holds at most {MAX_GAUGES} gauges; the maximum is "
                f"{MAX_GAUGES}, so no further gauge can be added"
            )
        self.gauges.append(gauge)
        return gauge

    def replace(self, gauge_id: str, gauge: Gauge) -> Gauge:
        """Replace the gauge named ``gauge_id`` with ``gauge`` (whose id may change)."""
        for index, existing in enumerate(self.gauges):
            if existing.id == gauge_id:
                other = self.get(gauge.id)
                if gauge.id != gauge_id and other is not None:
                    raise GaugeError(f"gauge {gauge.id!r} already exists")
                self.gauges[index] = gauge
                return gauge
        raise GaugeError(f"no gauge named {gauge_id!r}")

    def remove(self, gauge_id: str) -> Gauge:
        """Drop the gauge named ``gauge_id``."""
        for index, existing in enumerate(self.gauges):
            if existing.id == gauge_id:
                return self.gauges.pop(index)
        raise GaugeError(f"no gauge named {gauge_id!r}")

    def to_list(self) -> list[dict[str, Any]]:
        """The gauges as JSON-shaped dicts, in configuration order."""
        return [gauge.to_dict() for gauge in self.gauges]
