"""The kernel peer-credential lookup (T046), including the Darwin group quirk.

:func:`smart_panel.core.control.peercred.get_peer_groups` is a best-effort
refinement of an identity the *kernel* already established: a host that cannot
enumerate the peer's groups must degrade to the primary gid, never raise. A raise
there kills the connection thread and the control handshake is closed with no
reply, which is exactly what macos-latest did for the ``nobody`` peer before this
test existed (card ``t_e2514bdf``): Darwin's ``os.getgrouplist`` takes a C ``int``,
and the kernel reports ``nobody``'s group id as 4294967294.
"""

from __future__ import annotations

import pwd

import pytest

from smart_panel.core.control import peercred

# Darwin's uid_t/gid_t are unsigned, so the kernel reports the account's -2 as
# 2**32 - 2 (measured on macos-latest / Python 3.11.9).
DARWIN_NOBODY_ID = 4294967294

_DARWIN_NOBODY = pwd.struct_passwd(
    ("nobody", "*", DARWIN_NOBODY_ID, DARWIN_NOBODY_ID, "", "/var/empty", "/usr/bin/false")
)


def _darwin_getgrouplist(user: str, group: int) -> list[int]:
    """``os.getgrouplist`` as Darwin behaves: ``group`` is a C ``int``.

    Linux takes a ``gid_t`` there, which is why the same call succeeds on the
    Linux build host and raises on macOS.
    """
    if not -2**31 <= group < 2**31:
        raise OverflowError("Python int too large to convert to C int")
    return [group]


def test_a_gid_darwin_cannot_represent_falls_back_to_the_primary_gid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The peer's own group is still checked when the group table cannot be read."""
    monkeypatch.setattr(peercred.pwd, "getpwuid", lambda uid: _DARWIN_NOBODY)
    monkeypatch.setattr(peercred.os, "getgrouplist", _darwin_getgrouplist)

    assert peercred.get_peer_groups(DARWIN_NOBODY_ID, DARWIN_NOBODY_ID) == [DARWIN_NOBODY_ID]


def test_the_ids_darwin_reports_for_nobody_never_raise() -> None:
    """The real Darwin inputs are safe on every host, not just on macOS."""
    assert DARWIN_NOBODY_ID in peercred.get_peer_groups(DARWIN_NOBODY_ID, DARWIN_NOBODY_ID)
