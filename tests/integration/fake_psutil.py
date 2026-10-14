"""A test-only stand-in for the ``psutil`` module (T094, US4).

``smart_panel/plugins/sources/system.py`` imports ``psutil`` lazily. Under
``SMART_PANEL_TESTING=1`` it will instead load the file named by
``SMART_PANEL_FAKE_PSUTIL`` — exactly the way ``--clock-file`` is honoured only
in tests (``smart_panel/core/runner.py``). This module is that stand-in: it
implements the small slice of the ``psutil`` API the ``system`` source uses and
reads its values from the JSON file named by ``SMART_PANEL_FAKE_METRICS``, so a
test can change a metric between refreshes (and drop one to simulate a sensor
that stopped reporting).

The file is read on every call, so a test never has to restart the runner::

    {"cpu": 25.0, "mem": 40.0, "disk": {"/": 61.0},
     "net": {"bytes_recv": 1000, "bytes_sent": 500}}

A key that is absent (or null) makes the matching reader fail the way the real
``psutil`` would, so the source omits that metric and its consumers see
``no_data``.

Nothing here is imported by production code on a default path.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

__all__ = [
    "cpu_percent",
    "disk_usage",
    "net_io_counters",
    "sensors_temperatures",
    "virtual_memory",
]


def _values() -> dict:
    """The metric values from ``SMART_PANEL_FAKE_METRICS`` (empty when unset)."""
    path = os.environ.get("SMART_PANEL_FAKE_METRICS", "")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def cpu_percent(interval: float | None = None) -> float:
    """CPU load percent; raises when the fake file has no ``cpu`` key."""
    data = _values()
    if data.get("cpu") is None:
        raise RuntimeError("cpu is not available in the fake metrics file")
    return float(data["cpu"])


def virtual_memory() -> SimpleNamespace:
    """Memory usage percent; raises when the fake file has no ``mem`` key."""
    data = _values()
    if data.get("mem") is None:
        raise RuntimeError("mem is not available in the fake metrics file")
    return SimpleNamespace(percent=float(data["mem"]))


def disk_usage(path: str) -> SimpleNamespace:
    """Disk usage percent for ``path``; raises when the fake file has no entry."""
    disks = _values().get("disk") or {}
    if not isinstance(disks, dict) or path not in disks:
        raise OSError(f"no such filesystem {path!r} in the fake metrics file")
    return SimpleNamespace(percent=float(disks[path]))


def sensors_temperatures() -> dict:
    """A single core temperature, or an empty mapping when none is configured."""
    data = _values()
    if data.get("temp") is None:
        return {}
    return {"coretemp": [SimpleNamespace(label="Core 0", current=float(data["temp"]))]}


def net_io_counters() -> SimpleNamespace:
    """Byte counters for the (single) fake interface."""
    net = _values().get("net") or {}
    if not isinstance(net, dict):
        net = {}
    return SimpleNamespace(
        bytes_recv=float(net.get("bytes_recv", 0.0)),
        bytes_sent=float(net.get("bytes_sent", 0.0)),
    )
