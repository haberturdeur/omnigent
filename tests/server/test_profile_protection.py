"""Tests for private-profile locking and isolation roots."""

from __future__ import annotations

from pathlib import Path

import pytest

import omnigent.server.profile_protection as protection
from omnigent.errors import OmnigentError
from omnigent.server.profile_protection import (
    apply_profile_protection_change,
    begin_profile_membership_write,
    begin_profile_protection_transition,
    configure_profile_protection,
    finish_profile_membership_write,
    finish_profile_protection_transition,
    isolation_masks_for_workspace,
    mint_profile_unlock,
    plan_profile_protection,
    plan_profile_protection_removal,
    profile_is_accessible,
    profile_isolation_snapshot,
    profile_protection_generation,
    read_protected_profiles,
    register_runner_generation_lease,
    remove_profile_protection,
    revert_profile_protection_change,
    revoke_profile_unlock,
    stale_runner_generation_leases,
    unregister_runner_generation_lease,
    validate_profile_unlock,
)


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_PROFILE_PROTECTION_PATH", str(tmp_path / "protection.json"))


def test_masks_other_profiles_but_not_current_profile(tmp_path: Path) -> None:
    first = tmp_path / "private-a"
    second = tmp_path / "private-b"
    public = tmp_path / "public"
    for path in (first, second, public):
        path.mkdir()
    configure_profile_protection(
        "profile-a", user_id="owner", passcode="alpha", protected_roots=[str(first)]
    )
    configure_profile_protection(
        "profile-b", user_id="owner", passcode="beta", protected_roots=[str(second)]
    )

    assert isolation_masks_for_workspace(first / "project") == (second.resolve(),)
    assert isolation_masks_for_workspace(second) == (first.resolve(),)
    assert set(isolation_masks_for_workspace(public)) == {first.resolve(), second.resolve()}


def test_identical_roots_on_different_hosts_do_not_conflict() -> None:
    """Path strings are isolated within a host namespace."""
    root = "/srv/private"

    first = configure_profile_protection(
        "profile-a",
        user_id="owner",
        host_id="host-a",
        passcode="alpha",
        protected_roots=[root],
    )
    second = configure_profile_protection(
        "profile-b",
        user_id="owner",
        host_id="host-b",
        passcode="beta",
        protected_roots=[root],
    )

    assert first.host_id == "host-a"
    assert second.host_id == "host-b"
    assert isolation_masks_for_workspace("/srv/public", host_id="host-a") == (Path(root),)
    assert isolation_masks_for_workspace("/srv/private/project", host_id="host-a") == ()
    assert isolation_masks_for_workspace("/srv/public", host_id="host-c") == ()


def test_remote_root_is_not_created_on_api_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remote paths are normalized lexically without server filesystem I/O."""
    remote_root = Path("/remote-host-only/private")
    original_mkdir = Path.mkdir

    def guarded_mkdir(path: Path, *args, **kwargs) -> None:
        if path == remote_root:
            raise AssertionError("remote root was created on the API server")
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", guarded_mkdir)

    configured = configure_profile_protection(
        "profile-a",
        user_id="owner",
        host_id="host-a",
        passcode="alpha",
        protected_roots=[str(remote_root / ".." / "private")],
    )

    assert configured.protected_roots == (remote_root,)


def test_old_registry_entry_without_host_id_remains_server_local(tmp_path: Path) -> None:
    """Legacy entries retain the pre-host-scoping local/server semantics."""
    root = tmp_path / "private"
    root.mkdir()
    registry = tmp_path / "protection.json"
    registry.write_text(
        '{"generation":1,"profiles":[{"profile_id":"legacy",'
        '"user_id":"owner","passcode_hash":"hash",'
        f'"protected_roots":["{root}"],"unlock_token_hashes":[]}}]}}'
    )

    protected = read_protected_profiles()

    assert protected[0].host_id is None
    assert isolation_masks_for_workspace(tmp_path / "public") == (root,)
    assert isolation_masks_for_workspace(tmp_path / "public", host_id="host-a") == ()


def test_configure_creates_a_missing_protected_root(tmp_path: Path) -> None:
    """A new private profile may name a directory that is not created yet."""
    root = tmp_path / "new" / "private"

    configured = configure_profile_protection(
        "profile-a", user_id="owner", passcode="alpha", protected_roots=[str(root)]
    )

    assert root.is_dir()
    assert configured.protected_roots == (root.resolve(),)


def test_rejects_overlapping_roots_across_profiles(tmp_path: Path) -> None:
    root = tmp_path / "private"
    nested = root / "nested"
    nested.mkdir(parents=True)
    configure_profile_protection(
        "profile-a", user_id="owner", passcode="alpha", protected_roots=[str(root)]
    )

    with pytest.raises(OmnigentError, match="overlap"):
        configure_profile_protection(
            "profile-b", user_id="owner", passcode="beta", protected_roots=[str(nested)]
        )


def test_unlock_bearer_is_scoped_to_profile_and_owner(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    configure_profile_protection(
        "profile-a", user_id="owner", passcode="correct", protected_roots=[str(root)]
    )

    token = mint_profile_unlock("profile-a", "correct", user_id="owner")

    assert validate_profile_unlock(token, profile_id="profile-a", user_id="owner")
    assert token not in (tmp_path / "protection.json").read_text(encoding="utf-8")
    assert not validate_profile_unlock(token, profile_id="profile-b", user_id="owner")
    assert not validate_profile_unlock(token, profile_id="profile-a", user_id="other")
    with pytest.raises(OmnigentError, match="Incorrect passcode"):
        mint_profile_unlock("profile-a", "wrong", user_id="owner")


def test_reconfigure_and_remove_revoke_protection(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    configure_profile_protection(
        "profile-a", user_id="owner", passcode="correct", protected_roots=[str(root)]
    )
    token = mint_profile_unlock("profile-a", "correct", user_id="owner")

    configure_profile_protection(
        "profile-a", user_id="owner", passcode=None, protected_roots=[str(root)]
    )
    assert not validate_profile_unlock(token, profile_id="profile-a", user_id="owner")
    assert remove_profile_protection("profile-a", user_id="owner")
    assert read_protected_profiles() == ()


def test_prepared_change_is_not_effective_until_applied(tmp_path: Path) -> None:
    """Lifecycle coordination can finish before a new root becomes visible."""
    root = tmp_path / "private"

    change = plan_profile_protection(
        "profile-a",
        user_id="owner",
        passcode="correct",
        protected_roots=[str(root)],
    )

    assert change.changed_roots == (root.resolve(),)
    assert read_protected_profiles() == ()
    assert apply_profile_protection_change(change) == change.configured
    assert read_protected_profiles() == change.after


def test_topology_generation_changes_only_with_isolation_roots(tmp_path: Path) -> None:
    root = tmp_path / "private"
    public = tmp_path / "public"
    public.mkdir()
    assert profile_protection_generation() == 0

    configure_profile_protection(
        "profile-a", user_id="owner", passcode="correct", protected_roots=[str(root)]
    )
    generation, masks = profile_isolation_snapshot(public)

    assert generation == 1
    assert masks == (root.resolve(),)
    mint_profile_unlock("profile-a", "correct", user_id="owner")
    assert profile_protection_generation() == generation

    configure_profile_protection(
        "profile-a", user_id="owner", passcode=None, protected_roots=[str(root)]
    )
    assert profile_protection_generation() == generation


def test_compensation_is_conditional_on_unchanged_applied_state(tmp_path: Path) -> None:
    change = plan_profile_protection(
        "profile-a",
        user_id="owner",
        passcode="correct",
        protected_roots=[str(tmp_path / "private")],
    )
    apply_profile_protection_change(change)
    token = mint_profile_unlock("profile-a", "correct", user_id="owner")

    with pytest.raises(OmnigentError, match="concurrently"):
        revert_profile_protection_change(change)

    assert validate_profile_unlock(token, profile_id="profile-a", user_id="owner")


def test_prepared_change_rejects_a_stale_registry(tmp_path: Path) -> None:
    """A concurrent mutation cannot bypass the lifecycle step of its own plan."""
    first = plan_profile_protection(
        "profile-a",
        user_id="owner",
        passcode="first",
        protected_roots=[str(tmp_path / "first")],
    )
    second = plan_profile_protection(
        "profile-b",
        user_id="owner",
        passcode="second",
        protected_roots=[str(tmp_path / "second")],
    )
    apply_profile_protection_change(second)

    with pytest.raises(OmnigentError, match="concurrently"):
        apply_profile_protection_change(first)


def test_removal_plan_keeps_roots_protected_until_applied(tmp_path: Path) -> None:
    root = tmp_path / "private"
    configure_profile_protection(
        "profile-a", user_id="owner", passcode="correct", protected_roots=[str(root)]
    )

    change = plan_profile_protection_removal("profile-a", user_id="owner")

    assert change is not None
    assert change.changed_roots == (root.resolve(),)
    assert read_protected_profiles()
    apply_profile_protection_change(change)
    assert read_protected_profiles() == ()


def test_apply_removal_revalidates_unlock_inside_registry_transaction(tmp_path: Path) -> None:
    """A bearer revoked after planning cannot remove protection."""
    root = tmp_path / "private"
    configure_profile_protection(
        "profile-a", user_id="owner", passcode="correct", protected_roots=[str(root)]
    )
    token = mint_profile_unlock("profile-a", "correct", user_id="owner")
    change = plan_profile_protection_removal("profile-a", user_id="owner")
    assert change is not None
    revoke_profile_unlock(token, profile_id="profile-a", user_id="owner")

    with pytest.raises(OmnigentError, match="unlock"):
        apply_profile_protection_change(change, required_unlock_token=token)

    assert read_protected_profiles()


def test_generation_change_fences_cross_worker_runner_lease(tmp_path: Path) -> None:
    """A root mutation durably exposes every runner on the old generation."""
    assert register_runner_generation_lease("runner-a", 0, "lease-a")
    change = plan_profile_protection(
        "profile-a",
        user_id="owner",
        passcode="correct",
        protected_roots=[str(tmp_path / "private")],
    )
    apply_profile_protection_change(change)

    assert [lease.runner_id for lease in stale_runner_generation_leases(1)] == ["runner-a"]
    assert not register_runner_generation_lease("runner-b", 0, "lease-b")

    unregister_runner_generation_lease("lease-a")
    assert stale_runner_generation_leases(1) == ()


def test_runner_lease_does_not_expire_as_proof_of_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(protection.time, "time", lambda: 100.0)
    assert register_runner_generation_lease("runner-a", 0, "lease-a")

    monkeypatch.setattr(protection.time, "time", lambda: 10_000.0)

    assert [lease.lease_id for lease in stale_runner_generation_leases(1)] == ["lease-a"]
    unregister_runner_generation_lease("lease-a")


def test_protection_transition_and_membership_writes_exclude_each_other() -> None:
    operation_id = begin_profile_membership_write(("profile-a",))
    with pytest.raises(OmnigentError, match="membership is changing"):
        begin_profile_protection_transition("profile-a")
    finish_profile_membership_write(operation_id)

    begin_profile_protection_transition("profile-a")
    with pytest.raises(OmnigentError, match="protection is changing"):
        begin_profile_membership_write(("profile-a",))
    finish_profile_protection_transition("profile-a")


def test_declared_lock_without_registry_entry_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        protection,
        "_declared_protected_profile_ids",
        lambda: frozenset({"profile-a"}),
    )

    assert not profile_is_accessible("profile-a", None)
    assert profile_is_accessible("profile-b", None)
