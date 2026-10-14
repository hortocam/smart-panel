"""TEMPORARY diagnostic probe for the macOS LOCAL_PEERCRED handshake close.

Card ``t_e2514bdf`` (follow-up to PR #42, run 38486563021): on macos-latest the
runner closes the control handshake with no reply for a real second uid, so the
client reports ``runner_not_running`` (exit 3) instead of the policy's answer.

``runner start`` spawns the runner with ``stderr=DEVNULL``
(``cli/commands/runner.py::_start_detached``), so an unhandled exception on a
control-connection thread is invisible in CI and shows up only as a silent close.
This module reuses the real two-account harness from ``test_uid_switch`` and, with
the temporary ``SMART_PANEL_RUNNER_STDERR`` hook in ``_start_detached``, prints the
runner's stderr after the failing client call — the server-side traceback.

Deleted before the fix is submitted; this is a probe, not a test of the product.
"""

from __future__ import annotations

import sys
import time

import pytest

from tests.integration.test_uid_switch import ALLOWED_ACCOUNT, OWNER_UID, SwitchHost


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only diagnostic probe")
def test_probe_second_uid_handshake_close(host: SwitchHost) -> None:
    stderr_path = host.base / "runner-stderr.log"
    host.env["SMART_PANEL_RUNNER_STDERR"] = str(stderr_path)

    report: list[str] = []
    start = host.start()
    report.append(f"start rc={start.returncode} stdout={start.stdout.strip()[:300]!r}")

    proc = host.as_account(ALLOWED_ACCOUNT, "status", runner_uid=OWNER_UID)
    report.append(
        f"client {ALLOWED_ACCOUNT} rc={proc.returncode} "
        f"stdout={proc.stdout.strip()[:400]!r} stderr={proc.stderr.strip()[:300]!r}"
    )
    time.sleep(0.5)

    report.append(
        "runner stderr:\n"
        + (stderr_path.read_text(errors="replace") if stderr_path.exists() else "<no file>")
    )
    host.stop()
    print("\n".join(report))
    raise AssertionError("probe report printed above (temporary diagnostic)")
