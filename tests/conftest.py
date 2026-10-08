"""Shared pytest fixtures for the smart-panel suite (T003).

Fixtures provided here:

- ``tmp_home`` — points ``$SMART_PANEL_HOME`` at a fresh temporary directory so
  a test never reads or writes the developer's real ``~/.config/smart-panel``.
- ``fake_clock`` — a deterministic clock with independent monotonic and
  wall-clock time, so logic that measures durations (timeouts, freshness,
  alert countdowns) can be tested without sleeping.

The ``FakeClock`` class is intentionally minimal for the T003 scaffold. Phase 2
(T031) adds the richer ``smart_panel.sdk.testing.FakeClock`` harness for plugin
authors; this one exists so the Phase 1 protocol/rotation tests, and any early
lifecycle test, can advance time deterministically.
"""

from __future__ import annotations

from pathlib import Path

import pytest


class FakeClock:
    """A deterministic clock with separate monotonic and wall time.

    Args:
        start: Initial monotonic time in seconds.
        wall: Initial wall-clock time (POSIX seconds). Defaults to a fixed
            instant so wall-time formatting is reproducible in tests.
    """

    def __init__(self, start: float = 0.0, wall: float = 1_700_000_000.0) -> None:
        self._monotonic = float(start)
        self._wall = float(wall)

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin; only differences are meaningful."""
        return self._monotonic

    def time(self) -> float:
        """Wall-clock POSIX seconds."""
        return self._wall

    def advance(self, seconds: float) -> None:
        """Move both clocks forward by ``seconds`` (which may be negative)."""
        self._monotonic += float(seconds)
        self._wall += float(seconds)


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set ``SMART_PANEL_HOME`` to a fresh temp dir for the duration of a test.

    Returns:
        The temporary home directory (created, empty).
    """
    home = tmp_path / "smart-panel-home"
    home.mkdir()
    monkeypatch.setenv("SMART_PANEL_HOME", str(home))
    return home


@pytest.fixture
def fake_clock() -> FakeClock:
    """Return a fresh :class:`FakeClock` starting at monotonic 0."""
    return FakeClock()
