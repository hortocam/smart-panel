"""Unit tests for the ``system`` source (T091, US4).

Covers ``smart_panel/plugins/sources/system.py`` with a fake ``psutil``: ``cpu``
and ``mem`` (percent), ``temp`` (degrees C through ``sensors_temperatures()``
with a fallback to ``/sys/class/thermal/thermal_zone0/temp``, and ``no_data``
when neither is available), ``disk:<path>`` (percent), ``net_rx`` / ``net_tx``
(bytes per second from counter deltas) and ``interval_s`` (at least 1, default
2). The last test runs against the real ``psutil`` so the default path is
covered too.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from smart_panel.plugins.sources.system import (
    DEFAULT_INTERVAL_S,
    INTERVAL_FLOOR_S,
    METRIC_KINDS,
    SourceError,
    SystemSource,
)


class _FakePsutil:
    """A stand-in for the ``psutil`` module, with one attribute per metric."""

    def __init__(
        self,
        *,
        cpu: float | None = 12.5,
        mem: float | None = 40.0,
        temp: float | None = None,
        disk: dict[str, float] | None = None,
        bytes_recv: float = 0.0,
        bytes_sent: float = 0.0,
    ) -> None:
        self.cpu = cpu
        self.mem = mem
        self.temp = temp
        self.disk = dict(disk or {})
        self.bytes_recv = bytes_recv
        self.bytes_sent = bytes_sent

    def cpu_percent(self, interval: float | None = None) -> float:
        assert self.cpu is not None
        return float(self.cpu)

    def virtual_memory(self) -> SimpleNamespace:
        assert self.mem is not None
        return SimpleNamespace(percent=float(self.mem))

    def disk_usage(self, path: str) -> SimpleNamespace:
        if path not in self.disk:
            raise OSError(f"no such filesystem {path!r}")
        return SimpleNamespace(percent=float(self.disk[path]))

    def sensors_temperatures(self) -> dict[str, list[SimpleNamespace]]:
        if self.temp is None:
            return {}
        return {"coretemp": [SimpleNamespace(label="Core 0", current=float(self.temp))]}

    def net_io_counters(self) -> SimpleNamespace:
        return SimpleNamespace(bytes_recv=self.bytes_recv, bytes_sent=self.bytes_sent)


class _NoSensorsPsutil:
    """A stand-in without ``sensors_temperatures`` (as on macOS)."""

    def cpu_percent(self, interval: float | None = None) -> float:
        return 1.0

    def virtual_memory(self) -> SimpleNamespace:
        return SimpleNamespace(percent=2.0)


class _Clock:
    """A monotonic clock the test drives by hand."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def monotonic(self) -> float:
        return self._now

    def time(self) -> float:
        return 1_700_000_000.0 + self._now

    def advance(self, seconds: float) -> None:
        self._now += float(seconds)


def test_the_declared_metric_kinds_are_the_six_from_the_contract() -> None:
    assert set(METRIC_KINDS) == {"cpu", "mem", "temp", "disk", "net_rx", "net_tx"}
    assert SystemSource.type == "system"


def test_interval_defaults_to_two_seconds_with_a_floor_of_one() -> None:
    assert DEFAULT_INTERVAL_S == 2
    assert INTERVAL_FLOOR_S == 1
    assert SystemSource("sys").interval_s == 2
    assert SystemSource("sys", {"interval_s": 1}).interval_s == 1
    for bad in (0, -5, "soon"):
        with pytest.raises(SourceError):
            SystemSource("sys", {"interval_s": bad})


def test_cpu_mem_and_disk_are_read_as_percentages() -> None:
    module = _FakePsutil(cpu=12.5, mem=40.0, disk={"/": 61.0})
    source = SystemSource(
        "sys", {"metrics": ["cpu", "mem", "disk:/"]}, psutil_module=module
    )
    assert source.fetch() == {"cpu": 12.5, "mem": 40.0, "disk:/": 61.0}
    assert source.metrics == ("cpu", "mem", "disk:/")


def test_temperature_prefers_sensors_temperatures() -> None:
    module = _FakePsutil(temp=45.5)
    source = SystemSource("sys", {"metrics": ["temp"]}, psutil_module=module)
    assert source.fetch() == {"temp": 45.5}


def test_temperature_falls_back_to_the_thermal_zone_file(tmp_path) -> None:
    zone = tmp_path / "thermal_zone0_temp"
    zone.write_text("45000\n", encoding="utf-8")
    module = _FakePsutil(temp=None)
    source = SystemSource(
        "sys", {"metrics": ["temp"]}, psutil_module=module, thermal_zone=str(zone)
    )
    assert source.fetch() == {"temp": 45.0}


def test_temperature_is_reported_as_no_data_when_unavailable(tmp_path) -> None:
    """Neither source of temperature works: the metric is simply not published."""
    missing = tmp_path / "absent"
    module = _FakePsutil(temp=None)
    source = SystemSource(
        "sys", {"metrics": ["temp", "cpu"]}, psutil_module=module, thermal_zone=str(missing)
    )
    fetched = source.fetch()
    assert "temp" not in fetched, "an unavailable temperature must not publish a stale value"
    assert fetched == {"cpu": 12.5}

    # psutil without ``sensors_temperatures`` (macOS) takes the same path.
    other = SystemSource(
        "sys", {"metrics": ["temp"]}, psutil_module=_NoSensorsPsutil(), thermal_zone=str(missing)
    )
    assert other.fetch() == {}


def test_network_throughput_is_computed_from_counter_deltas() -> None:
    clock = _Clock()
    module = _FakePsutil(bytes_recv=1_000, bytes_sent=500)
    source = SystemSource(
        "sys", {"metrics": ["net_rx", "net_tx"]}, psutil_module=module, clock=clock
    )
    # The first read has no previous counter, so there is no interval to divide by.
    assert source.fetch() == {"net_rx": 0.0, "net_tx": 0.0}

    clock.advance(2.0)
    module.bytes_recv += 2_000
    module.bytes_sent += 600
    assert source.fetch() == {"net_rx": 1_000.0, "net_tx": 300.0}

    clock.advance(1.0)
    module.bytes_recv += 500
    module.bytes_sent += 0
    assert source.fetch() == {"net_rx": 500.0, "net_tx": 0.0}


def test_a_counter_reset_reports_zero_rather_than_a_negative_rate() -> None:
    clock = _Clock()
    module = _FakePsutil(bytes_recv=10_000, bytes_sent=10_000)
    source = SystemSource(
        "sys", {"metrics": ["net_rx", "net_tx"]}, psutil_module=module, clock=clock
    )
    source.fetch()
    clock.advance(1.0)
    module.bytes_recv = 100  # the interface was reset
    module.bytes_sent = 50
    assert source.fetch() == {"net_rx": 0.0, "net_tx": 0.0}


def test_an_unknown_metric_is_refused() -> None:
    for metrics in (["bogus"], ["cpu", "temperature"], ["disk"], ["disk:"]):
        with pytest.raises(SourceError):
            SystemSource("sys", {"metrics": metrics}, psutil_module=_FakePsutil())


def test_an_unreadable_metric_does_not_stop_the_others() -> None:
    """One failing reader (a path that does not exist) must not lose the rest."""
    module = _FakePsutil(cpu=3.0, disk={})
    source = SystemSource("sys", {"metrics": ["disk:/nope", "cpu"]}, psutil_module=module)
    assert source.fetch() == {"cpu": 3.0}


def test_the_settings_are_declared_for_the_source_type() -> None:
    names = {setting.name for setting in SystemSource.SETTINGS}
    assert names == {"metrics", "interval_s"}


def test_the_real_psutil_reads_cpu_and_memory() -> None:
    """The production path (no fake) still answers with percentages."""
    source = SystemSource("sys", {"metrics": ["cpu", "mem"]})
    fetched = source.fetch()
    assert 0.0 <= fetched["cpu"] <= 100.0
    assert 0.0 <= fetched["mem"] <= 100.0


def test_the_network_rate_is_not_stateful_across_threads_incorrectly() -> None:
    """Two concurrent fetches of one source must not corrupt the counter state."""
    clock = _Clock()
    module = _FakePsutil(bytes_recv=0, bytes_sent=0)
    source = SystemSource(
        "sys", {"metrics": ["net_rx", "net_tx"]}, psutil_module=module, clock=clock
    )
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            source.fetch()
        except BaseException as exc:  # pragma: no cover - a failure is asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not errors
    assert source.fetch() == {"net_rx": 0.0, "net_tx": 0.0}
