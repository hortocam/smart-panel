"""The ``system`` source type: local metrics through ``psutil`` (T095, US4).

Publishes one topic per requested metric — ``<name>.cpu``, ``<name>.mem``,
``<name>.temp``, ``<name>.disk:<path>``, ``<name>.net_rx`` and ``<name>.net_tx``
— on the interval the ``sources`` plugin schedules (``interval_s`` at least 1,
default 2; FR-032/FR-033).

``research.md`` decision 12: ``psutil`` for CPU percent, memory percent, disk
usage, network throughput (a rate computed from the counter deltas, so the
previous counters and the monotonic instant are kept on the instance) and
temperature (``sensors_temperatures()``, falling back to
``/sys/class/thermal/thermal_zone0/temp`` on the Pi). A metric the platform
cannot read (temperature on macOS, say) is **omitted**: the topic is never
published, so consumers show their ``no data`` state instead of a stale number.

``psutil`` is imported lazily because it lives in the ``app`` extra. Under
``SMART_PANEL_TESTING=1`` the file named by ``SMART_PANEL_FAKE_PSUTIL`` is loaded
in its place, exactly the way ``--clock-file`` is honoured only in tests
(``smart_panel/core/runner.py``); nothing on a default path can reach it.

Only ``smart_panel.sdk`` and the standard library are imported (FR-043);
``psutil`` itself is an ``app``-extra dependency of this plugin, not of
``panel_driver``.
"""

from __future__ import annotations

import importlib.util
import os
import threading
import time
from collections.abc import Mapping
from typing import Any

from smart_panel.sdk.settings import Setting, SettingError, validate

__all__ = [
    "DEFAULT_INTERVAL_S",
    "DEFAULT_METRICS",
    "FAKE_PSUTIL_ENV",
    "INTERVAL_FLOOR_S",
    "METRIC_KINDS",
    "THERMAL_ZONE_PATH",
    "SourceError",
    "SystemSource",
    "load_psutil",
]

INTERVAL_FLOOR_S = 1
INTERVAL_MAX_S = 3600
DEFAULT_INTERVAL_S = 2
DEFAULT_METRICS = ("cpu", "mem")
METRIC_KINDS = ("cpu", "mem", "temp", "disk", "net_rx", "net_tx")

THERMAL_ZONE_PATH = "/sys/class/thermal/thermal_zone0/temp"
TESTING_ENV = "SMART_PANEL_TESTING"
FAKE_PSUTIL_ENV = "SMART_PANEL_FAKE_PSUTIL"

_SIMPLE_METRICS = ("cpu", "mem", "temp", "net_rx", "net_tx")
_DISK_PREFIX = "disk:"


class SourceError(ValueError):
    """A source's settings or its payload did not conform."""


def _load_module_file(path: str) -> Any:
    """Import a Python file as a module (the test-only ``psutil`` stand-in)."""
    spec = importlib.util.spec_from_file_location("smart_panel_fake_psutil", path)
    if spec is None or spec.loader is None:
        raise SourceError(f"cannot load the fake psutil module at {path!r}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_psutil() -> Any:
    """The ``psutil`` module, or the test-only stand-in under ``SMART_PANEL_TESTING=1``.

    Raises:
        SourceError: ``psutil`` (the ``app`` extra) is not installed.
    """
    if os.environ.get(TESTING_ENV) == "1":
        path = os.environ.get(FAKE_PSUTIL_ENV)
        if path:
            return _load_module_file(path)
    try:
        import psutil
    except ImportError as exc:  # pragma: no cover - exercised by a base install
        raise SourceError(
            "the system source needs the 'app' extra: pip install 'panel-driver[app]'"
        ) from exc
    return psutil


class SystemSource:
    """One ``system`` source: the metrics to publish and their refresh interval."""

    type = "system"
    SETTINGS: tuple[Setting, ...] = (
        Setting(
            name="metrics",
            type="list",
            default=list(DEFAULT_METRICS),
            description=(
                "Metrics to publish: cpu, mem, temp, disk:<path>, net_rx, net_tx"
            ),
            item=Setting(name="metric", type="str", default="cpu"),
        ),
        Setting(
            name="interval_s",
            type="int",
            default=DEFAULT_INTERVAL_S,
            description=f"Seconds between refreshes (at least {INTERVAL_FLOOR_S})",
            min=INTERVAL_FLOOR_S,
            max=INTERVAL_MAX_S,
        ),
    )
    METRICS: tuple[str, ...] = METRIC_KINDS

    def __init__(
        self,
        name: str,
        settings: Mapping[str, Any] | None = None,
        *,
        psutil_module: Any = None,
        clock: Any = None,
        thermal_zone: str = THERMAL_ZONE_PATH,
    ) -> None:
        self.name = str(name)
        try:
            self.settings = validate(self.SETTINGS, settings or {})
        except SettingError as exc:
            raise SourceError(str(exc)) from exc
        self._metrics = self._parse_metrics(self.settings.get("metrics"))
        self._module = psutil_module if psutil_module is not None else load_psutil()
        self._clock = clock
        self._thermal_zone = str(thermal_zone)
        self._lock = threading.Lock()
        self._net: tuple[float, float, float] | None = None

    # --------------------------------------------------------------- settings

    @staticmethod
    def _parse_metrics(raw: Any) -> tuple[str, ...]:
        """Validate the ``metrics`` list, keeping the caller's order."""
        entries = list(raw or []) if isinstance(raw, (list, tuple)) else [raw]
        if not entries:
            raise SourceError("a system source needs at least one metric")
        cleaned: list[str] = []
        for entry in entries:
            text = str(entry).strip()
            if text in _SIMPLE_METRICS:
                pass
            elif text.startswith(_DISK_PREFIX) and len(text) > len(_DISK_PREFIX):
                pass
            else:
                raise SourceError(
                    f"unknown metric {entry!r}; expected one of "
                    "cpu, mem, temp, disk:<path>, net_rx, net_tx"
                )
            if text not in cleaned:
                cleaned.append(text)
        return tuple(cleaned)

    @property
    def interval_s(self) -> int:
        """Seconds between refreshes."""
        return int(self.settings["interval_s"])

    @property
    def metrics(self) -> tuple[str, ...]:
        """The metrics this source publishes."""
        return tuple(self._metrics)

    # --------------------------------------------------------------- fetching

    def fetch(self, http: Any = None) -> dict[str, float]:
        """Read every declared metric.

        A metric the platform cannot read is left out of the result (the sources
        plugin then publishes nothing for it, and a gauge shows ``no data``); an
        unknown metric is refused when the source is built, not here.
        """
        published: dict[str, float] = {}
        net: tuple[float, float] | None = None
        for metric in self._metrics:
            if metric in ("net_rx", "net_tx"):
                if net is None:
                    net = self._net_rates()
                if net is not None:
                    published[metric] = net[0] if metric == "net_rx" else net[1]
                continue
            value = self._read(metric)
            if value is not None:
                published[metric] = value
        return published

    def _read(self, metric: str) -> float | None:
        """One metric's value, or ``None`` when the platform cannot supply it."""
        try:
            if metric == "cpu":
                return float(self._module.cpu_percent(interval=None))
            if metric == "mem":
                return float(self._module.virtual_memory().percent)
            if metric == "temp":
                return self._temperature()
            if metric.startswith(_DISK_PREFIX):
                return float(self._module.disk_usage(metric[len(_DISK_PREFIX) :]).percent)
        except Exception:  # an unavailable metric must not lose the others
            return None
        return None

    def _temperature(self) -> float | None:
        """Degrees C from ``sensors_temperatures()``, else the thermal zone file."""
        sensors = getattr(self._module, "sensors_temperatures", None)
        if sensors is not None:
            try:
                readings = sensors() or {}
            except Exception:  # pragma: no cover - psutil raises on some platforms
                readings = {}
            if isinstance(readings, Mapping):
                for entries in readings.values():
                    for entry in entries or []:
                        current = getattr(entry, "current", None)
                        if (
                            isinstance(current, (int, float))
                            and not isinstance(current, bool)
                        ):
                            return float(current)
        try:
            with open(self._thermal_zone, encoding="utf-8") as handle:
                raw = handle.read().strip()
        except OSError:
            return None
        try:
            # The kernel reports millidegrees C.
            return float(raw) / 1000.0
        except ValueError:
            return None

    def _net_rates(self) -> tuple[float, float] | None:
        """``(rx, tx)`` bytes per second from the counter deltas since the last read."""
        try:
            counters = self._module.net_io_counters()
            received = float(counters.bytes_recv)
            sent = float(counters.bytes_sent)
        except Exception:
            return None
        now = self._monotonic()
        with self._lock:
            previous = self._net
            self._net = (now, received, sent)
        if previous is None:
            # No earlier counter to subtract: there is no rate yet.
            return (0.0, 0.0)
        elapsed = now - previous[0]
        if elapsed <= 0:
            return (0.0, 0.0)
        rx = (received - previous[1]) / elapsed if received >= previous[1] else 0.0
        tx = (sent - previous[2]) / elapsed if sent >= previous[2] else 0.0
        return (max(0.0, rx), max(0.0, tx))

    def _monotonic(self) -> float:
        """Monotonic seconds from the injected clock, or the process clock."""
        clock = self._clock
        if clock is not None and hasattr(clock, "monotonic"):
            return float(clock.monotonic())
        return time.monotonic()
