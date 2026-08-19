"""Cross-replica persistence for private-profile protection state."""

from __future__ import annotations

from pathlib import Path

import pytest

import omnigent.server.profile_protection as protection


def test_database_registry_survives_a_new_server_instance(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIGENT_PROFILE_PROTECTION_PATH", raising=False)
    monkeypatch.setattr(
        protection,
        "resolve_profile_protection_path",
        lambda: tmp_path / "server-data" / "profile_protection.json",
    )
    monkeypatch.setattr(protection, "_database_registry", None)
    protection.configure_profile_protection_registry(db_uri)
    configured = protection.configure_profile_protection(
        "0123456789abcdef0123456789abcdef",
        user_id="owner",
        host_id="host-a",
        passcode="secret",
        protected_roots=["/srv/private"],
    )
    token = protection.mint_profile_unlock(configured.profile_id, "secret", user_id="owner")
    generation = protection.profile_protection_generation()

    protection.configure_profile_protection_registry(db_uri)

    restored = protection.get_profile_protection(configured.profile_id, user_id="owner")
    assert restored is not None
    assert restored.profile_id == configured.profile_id
    assert restored.host_id == "host-a"
    assert restored.protected_roots == configured.protected_roots
    assert restored.unlock_token_hashes
    assert protection.validate_profile_unlock(
        token,
        profile_id=configured.profile_id,
        user_id="owner",
    )
    assert protection.profile_protection_generation() == generation


def test_database_registry_shares_runner_generation_leases(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIGENT_PROFILE_PROTECTION_PATH", raising=False)
    monkeypatch.setattr(
        protection,
        "resolve_profile_protection_path",
        lambda: tmp_path / "server-data" / "profile_protection.json",
    )
    monkeypatch.setattr(protection, "_database_registry", None)
    protection.configure_profile_protection_registry(db_uri)
    generation = protection.profile_protection_generation()
    assert protection.register_runner_generation_lease("runner-a", generation, "lease-a")

    protection.configure_profile_protection_registry(db_uri)

    assert [
        lease.runner_id for lease in protection.stale_runner_generation_leases(generation + 1)
    ] == ["runner-a"]
    protection.unregister_runner_generation_lease("lease-a")


def test_database_registry_shares_unlock_throttling(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIGENT_PROFILE_PROTECTION_PATH", raising=False)
    monkeypatch.setattr(
        protection,
        "resolve_profile_protection_path",
        lambda: tmp_path / "server-data" / "profile_protection.json",
    )
    monkeypatch.setattr(protection, "_database_registry", None)
    protection.configure_profile_protection_registry(db_uri)
    protection.configure_profile_protection(
        "0123456789abcdef0123456789abcdef",
        user_id="owner",
        host_id="host-a",
        passcode="secret",
        protected_roots=["/srv/private"],
    )
    for _ in range(protection._UNLOCK_ATTEMPT_LIMIT):
        with pytest.raises(protection.OmnigentError, match="Incorrect passcode"):
            protection.mint_profile_unlock(
                "0123456789abcdef0123456789abcdef",
                "wrong",
                user_id="owner",
            )

    protection.configure_profile_protection_registry(db_uri)

    with pytest.raises(protection.OmnigentError, match="Too many unlock attempts"):
        protection.mint_profile_unlock(
            "0123456789abcdef0123456789abcdef",
            "secret",
            user_id="owner",
        )


def test_database_registry_coordinates_membership_and_protection_transitions(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIGENT_PROFILE_PROTECTION_PATH", raising=False)
    monkeypatch.setattr(
        protection,
        "resolve_profile_protection_path",
        lambda: tmp_path / "server-data" / "profile_protection.json",
    )
    monkeypatch.setattr(protection, "_database_registry", None)
    protection.configure_profile_protection_registry(db_uri)

    operation_id = protection.begin_profile_membership_write(("profile-a",))
    with pytest.raises(protection.OmnigentError, match="membership is changing"):
        protection.begin_profile_protection_transition("profile-a")
    protection.finish_profile_membership_write(operation_id)

    protection.begin_profile_protection_transition("profile-a")
    protection.configure_profile_protection_registry(db_uri)
    with pytest.raises(protection.OmnigentError, match="protection is changing"):
        protection.begin_profile_membership_write(("profile-a",))
    protection.finish_profile_protection_transition("profile-a")
