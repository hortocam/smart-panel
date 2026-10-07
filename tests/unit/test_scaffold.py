"""Self-tests for the T003 scaffold fixtures.

These pin the behaviour the scaffold promises other tests will rely on:
``tmp_home`` points ``$SMART_PANEL_HOME`` at a fresh temp directory (so tests
never touch the developer's real config), and ``fake_clock`` is deterministic
with independent monotonic and wall time. If either stops working, every test
that uses it silently loses its isolation - so the fixtures are tested here.
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.conftest import FakeClock


def test_tmp_home_sets_the_env_var_to_an_existing_empty_dir(tmp_home: Path) -> None:
    assert os.environ["SMART_PANEL_HOME"] == str(tmp_home)
    assert tmp_home.is_dir()
    assert list(tmp_home.iterdir()) == []


def test_fake_clock_starts_deterministic(fake_clock: FakeClock) -> None:
    assert fake_clock.monotonic() == 0.0
    assert fake_clock.time() == 1_700_000_000.0


def test_fake_clock_advance_moves_both_clocks(fake_clock: FakeClock) -> None:
    fake_clock.advance(61.5)
    assert fake_clock.monotonic() == 61.5
    assert fake_clock.time() == 1_700_000_061.5
