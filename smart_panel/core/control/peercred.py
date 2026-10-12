"""Kernel peer credentials for a Unix-domain socket (T046).

Every platform-specific byte of the peer-credential lookup lives here and
nowhere else (constitution, Principle V). A connection is identified by the
kernel, never by anything the client claims, so a local account cannot
impersonate another (``research.md`` decision 6).

The Linux path uses ``SO_PEERCRED``. The macOS path uses ``LOCAL_PEERCRED``
(``SOL_LOCAL``), which returns a Darwin ``struct xucred``; that branch is not
exercised on the Linux build host this card ran on and is labelled unverified
(constitution, Principle I) — the CI macOS job is the first place it executes.
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
_SOL_LOCAL = getattr(socket, "SOL_LOCAL", 0)
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
    # Unverified: see the module docstring.
    raw = conn.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, _XUCRED_SIZE)
    _version, uid, _ngroups = struct.unpack_from("IIH", raw, 0)
    gid = struct.unpack_from("I", raw, 12)[0]
    return uid, gid


def get_peer_groups(uid: int, gid: int) -> list[int]:
    """Return the groups the peer belongs to, primary ``gid`` included.

    Falls back to just ``gid`` when the account is unknown to the host, which
    keeps the primary-group membership check working.
    """
    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError:
        return [gid]
    try:
        return list(os.getgrouplist(name, gid))
    except OSError:
        return [gid]
