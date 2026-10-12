"""The control channel's access policy (T045, FR-051).

The decision for every connection is pure and takes numbers, so it can be
reasoned about and tested without touching the host's account database
(``contracts/control-protocol.md``, "Authorization"):

* :func:`is_allowed` — the runner's own uid, root, any uid in
  ``access.allowed_users`` and any member of ``access.allowed_group`` may use
  the channel; everyone else is refused with ``not_permitted``.
* :func:`requires_owner` — changing the allow-list or a secret is narrower:
  only the runner's uid or root.
* :func:`authorize` — the two combined into the one code the server needs
  (``None`` to proceed, else the error code to reply with).

:meth:`Policy.from_config` is the single impure step: it resolves the account
and group *names* stored in the config to the numeric ids the kernel reports.
A name that does not resolve grants nothing rather than raising, so an
allow-list that names an account that has since been deleted cannot wedge the
runner.
"""

from __future__ import annotations

import grp
import pwd
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

ROOT_UID = 0

# ``contracts/control-protocol.md``: "Mutations: owner or root only" for the
# access verbs; ``secret.*`` is "Owner or root only" in full.
_OWNER_COMMANDS = frozenset(
    {"access.allow_user", "access.deny_user", "access.set_group", "access.clear_group"}
)
_OWNER_PREFIXES = ("secret.",)

NOT_PERMITTED = "not_permitted"


@dataclass(frozen=True)
class Policy:
    """The resolved allow-list the server applies to every connection."""

    runner_uid: int
    allowed_uids: frozenset[int] = field(default_factory=frozenset)
    allowed_gid: int | None = None

    @classmethod
    def from_config(cls, access: Mapping[str, Any], *, runner_uid: int) -> Policy:
        """Build a policy from the ``access`` section of the config document."""
        users = access.get("allowed_users") or ()
        resolved: set[int] = set()
        if isinstance(users, Iterable):
            for name in users:
                try:
                    resolved.add(pwd.getpwnam(str(name)).pw_uid)
                except (KeyError, TypeError, ValueError):
                    continue
        gid: int | None = None
        group = access.get("allowed_group")
        if isinstance(group, str) and group:
            try:
                gid = grp.getgrnam(group).gr_gid
            except KeyError:
                gid = None
        return cls(runner_uid=runner_uid, allowed_uids=frozenset(resolved), allowed_gid=gid)


def is_owner(peer_uid: int, policy: Policy) -> bool:
    """Whether the peer is the runner's own account or root."""
    return peer_uid == ROOT_UID or peer_uid == policy.runner_uid


def is_allowed(peer_uid: int, peer_groups: Iterable[int], policy: Policy) -> bool:
    """Whether the peer may use the control channel at all.

    Args:
        peer_uid: The connecting account's uid, from the kernel.
        peer_groups: The peer's group ids (primary plus supplementary).
        policy: The resolved allow-list.
    """
    if is_owner(peer_uid, policy):
        return True
    if peer_uid in policy.allowed_uids:
        return True
    if policy.allowed_gid is not None:
        groups = set(peer_groups)
        if policy.allowed_gid in groups:
            return True
    return False


def requires_owner(cmd: str) -> bool:
    """Whether ``cmd`` is restricted to the runner's uid or root."""
    if cmd in _OWNER_COMMANDS:
        return True
    return any(cmd.startswith(prefix) for prefix in _OWNER_PREFIXES)


def authorize(
    peer_uid: int,
    peer_groups: Iterable[int],
    policy: Policy,
    cmd: str,
) -> str | None:
    """Return ``None`` when ``cmd`` may run for this peer, else the error code.

    The server writes the returned ``not_permitted`` response, logs the denial
    and closes the connection.
    """
    if not is_allowed(peer_uid, peer_groups, policy):
        return NOT_PERMITTED
    if requires_owner(cmd) and not is_owner(peer_uid, policy):
        return NOT_PERMITTED
    return None
