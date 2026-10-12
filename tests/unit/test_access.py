"""Unit tests for the control access policy (T037, FR-051).

``access.is_allowed`` is the pure decision the runner makes for every
connection: the runner's own uid and root always pass, an account listed in
``access.allowed_users`` or belonging to ``access.allowed_group`` passes, and
everyone else is refused with ``not_permitted`` (``control-protocol.md``,
"Authorization"). ``requires_owner`` narrows the allow-list mutations and every
``secret.*`` command to the runner's uid or root, so an allowed non-owner
account can read status but cannot widen its own access.

The tests build :class:`Policy` values directly with numeric ids so the policy
is exercised without depending on the host's real ``passwd``/``group`` files.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from smart_panel.core.control import access
from smart_panel.core.control.access import Policy

RUNNER_UID = 4242
ROOT_UID = 0
ALLOWED_UID = 1500
OTHER_UID = 1000
ALLOWED_GID = 2000
OTHER_GID = 3000


def _policy(**kwargs: object) -> Policy:
    """A policy with the runner's uid set and everything else defaulted."""
    return Policy(runner_uid=RUNNER_UID, **kwargs)


class TestIsAllowed:
    """The allow-list decision for one connection."""

    def test_runner_uid_is_allowed(self) -> None:
        assert access.is_allowed(RUNNER_UID, [RUNNER_UID], _policy()) is True

    def test_root_is_allowed(self) -> None:
        assert access.is_allowed(ROOT_UID, [ROOT_UID], _policy()) is True

    def test_uid_in_allowed_users_is_allowed(self) -> None:
        policy = _policy(allowed_uids=frozenset({ALLOWED_UID}))
        assert access.is_allowed(ALLOWED_UID, [ALLOWED_UID], policy) is True

    def test_member_of_allowed_group_is_allowed(self) -> None:
        policy = _policy(allowed_gid=ALLOWED_GID)
        assert access.is_allowed(OTHER_UID, [OTHER_GID, ALLOWED_GID], policy) is True

    def test_denied_when_neither_listed_nor_in_the_group(self) -> None:
        policy = _policy(allowed_uids=frozenset({ALLOWED_UID}), allowed_gid=ALLOWED_GID)
        assert access.is_allowed(OTHER_UID, [OTHER_GID], policy) is False

    def test_default_policy_allows_no_additional_accounts(self) -> None:
        policy = Policy(runner_uid=RUNNER_UID)
        assert policy.allowed_uids == frozenset()
        assert policy.allowed_gid is None
        assert access.is_allowed(OTHER_UID, [OTHER_GID], policy) is False

    def test_no_allowed_group_denies_every_group(self) -> None:
        assert access.is_allowed(OTHER_UID, [OTHER_GID, ALLOWED_GID], _policy()) is False

    def test_from_config_resolves_account_and_group_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_getpwnam(name: str) -> SimpleNamespace:
            if name == "alice":
                return SimpleNamespace(pw_uid=ALLOWED_UID)
            raise KeyError(name)

        def fake_getgrnam(name: str) -> SimpleNamespace:
            if name == "operators":
                return SimpleNamespace(gr_gid=ALLOWED_GID)
            raise KeyError(name)

        monkeypatch.setattr(access.pwd, "getpwnam", fake_getpwnam)
        monkeypatch.setattr(access.grp, "getgrnam", fake_getgrnam)

        policy = Policy.from_config(
            {"allowed_users": ["alice", "ghost"], "allowed_group": "operators"},
            runner_uid=RUNNER_UID,
        )
        assert policy.runner_uid == RUNNER_UID
        # A name that does not resolve grants nothing; the resolved ones stand.
        assert policy.allowed_uids == frozenset({ALLOWED_UID})
        assert policy.allowed_gid == ALLOWED_GID
        assert access.is_allowed(ALLOWED_UID, [], policy) is True
        assert access.is_allowed(OTHER_UID, [ALLOWED_GID], policy) is True

    def test_from_config_with_an_unknown_group_grants_no_group(self) -> None:
        policy = Policy.from_config(
            {"allowed_users": [], "allowed_group": "ghost"}, runner_uid=RUNNER_UID
        )
        assert policy.allowed_gid is None


class TestRequiresOwner:
    """Which commands are restricted to the runner's uid or root."""

    @pytest.mark.parametrize(
        "cmd",
        ["access.allow_user", "access.deny_user", "access.set_group", "access.clear_group"],
    )
    def test_access_mutations_require_owner(self, cmd: str) -> None:
        assert access.requires_owner(cmd) is True

    @pytest.mark.parametrize("cmd", ["secret.set", "secret.delete", "secret.list"])
    def test_secret_commands_require_owner(self, cmd: str) -> None:
        assert access.requires_owner(cmd) is True

    def test_access_list_is_not_owner_only(self) -> None:
        assert access.requires_owner("access.list") is False

    @pytest.mark.parametrize(
        "cmd", ["runner.status", "runner.stop", "config.set", "plugin.call", "logs.tail"]
    )
    def test_ordinary_commands_are_not_owner_only(self, cmd: str) -> None:
        assert access.requires_owner(cmd) is False


class TestAuthorize:
    """The composed decision the server applies to each request."""

    def test_owner_may_run_an_owner_only_command(self) -> None:
        assert access.authorize(RUNNER_UID, [RUNNER_UID], _policy(), "secret.set") is None

    def test_root_may_run_an_owner_only_command(self) -> None:
        assert access.authorize(ROOT_UID, [ROOT_UID], _policy(), "access.allow_user") is None

    def test_allowed_non_owner_is_refused_not_permitted(self) -> None:
        policy = _policy(allowed_uids=frozenset({ALLOWED_UID}))
        assert access.authorize(ALLOWED_UID, [ALLOWED_UID], policy, "secret.set") == (
            "not_permitted"
        )

    def test_denied_peer_is_refused_every_command(self) -> None:
        assert access.authorize(OTHER_UID, [OTHER_GID], _policy(), "runner.status") == (
            "not_permitted"
        )

    def test_allowed_non_owner_may_run_an_ordinary_command(self) -> None:
        policy = _policy(allowed_uids=frozenset({ALLOWED_UID}))
        assert access.authorize(ALLOWED_UID, [ALLOWED_UID], policy, "runner.status") is None
