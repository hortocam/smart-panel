"""Kernel peer credentials for a Unix-domain socket (T046).

Every platform-specific byte of the peer-credential lookup lives here and
nowhere else (constitution, Principle V). A connection is identified by the
kernel, never by anything the client claims, so a local account cannot
impersonate another (``research.md`` decision 6).

The Linux path uses ``SO_PEERCRED``. The macOS path uses ``LOCAL_PEERCRED``
(``SOL_LOCAL``), which returns a Darwin ``struct xucred``; both are exercised for
real by the cross-account test in ``tests/integration/test_uid_switch.py``, which
connects as ``nobody`` (uid 4294967294) and ``daemon`` (uid 1) on macos-latest and
ubuntu-latest. Python's ``socket`` module does not export ``SOL_LOCAL`` on Darwin,
so the ``0`` default below is load-bearing there rather than cosmetic: ``SOL_LOCAL``
is 0 (verified on macos-latest / Python 3.11.9, PR #43 run 38488135854).

Group enumeration is a separate story on Darwin: ``os.getgrouplist`` takes a C
``int`` there, so the group id the kernel reports for ``nobody`` (4294967294) does
not fit and raises ``OverflowError``. That exception used to escape
:func:`get_peer_groups`, kill the connection thread and close the handshake with
no reply, which is why a second uid got ``runner_not_running`` instead of a policy
answer (card ``t_e2514bdf``, found by T134's cross-account test).
"""

from __future__ import annotations

import os
import pwd
import socket
import struct
import sys

# Linux: struct ucred { pid_t pid; uid_t uid; gid_t gid; } — three 32-bit ints.
_UCRED_FORMAT = "3i"
_UCRED_SIZE = struct.calcsize(_UCRED_FORMAT)

# Darwin: struct xucred { u_int cr_version; uid_t cr_uid; short cr_ngroups;
# gid_t cr_groups[NGROUPS]; } with NGROUPS = 16. cr_groups is 4-byte aligned,
# so it starts at offset 12 and the whole struct is 12 + 16 * 4 = 76 bytes.
_XUCRED_SIZE = 12 + 16 * 4
_SOL_LOCAL = getattr(socket, "SOL_LOCAL", 0)  # Darwin: 0; not exported by Python
_LOCAL_PEERCRED = getattr(socket, "LOCAL_PEERCRED", 0x001)


def get_peer(conn: socket.socket) -> tuple[int, int]:
    """Return the ``(uid, gid)`` the kernel reports for the connected peer.

    Raises:
        OSError: The socket does not support peer credentials.
    """
    if sys.platform == "darwin":
        return _get_peer_macos(conn)
    return _get_peer_linux(conn)


def _get_peer_linux(conn: socket.socket) -> tuple[int, int]:
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED_SIZE)
    _pid, uid, gid = struct.unpack(_UCRED_FORMAT, raw)
    return uid, gid


def _get_peer_macos(conn: socket.socket) -> tuple[int, int]:
    # Darwin returns a struct xucred; the parse below is confirmed against real
    # peers (uid 1 and uid 4294967294) on macos-latest (PR #43 run 38488135854).
    raw = conn.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, _XUCRED_SIZE)
    _version, uid, _ngroups = struct.unpack_from("IIH", raw, 0)
    gid = struct.unpack_from("I", raw, 12)[0]
    return uid, gid


def get_peer_groups(uid: int, gid: int) -> list[int]:
    """Return the groups the peer belongs to, primary ``gid`` included.

    Falls back to just ``gid`` when the account is unknown to the host, which
    keeps the primary-group membership check working, and when the platform's
    group lookup cannot represent ``gid``: Darwin's ``os.getgrouplist`` takes a C
    ``int``, and the kernel reports ``nobody``'s group as 4294967294. Raising here
    would close the control handshake with no reply, so the fallback is total.
    """
    try:
        name = pwd.getpwuid(uid).pw_name
    except (KeyError, OverflowError):
        return [gid]
    try:
        return list(os.getgrouplist(name, gid))
    except (OSError, OverflowError, ValueError):
        return [gid]
