"""Shared database persistence for private-profile isolation state."""

from __future__ import annotations

import pytest

from omnigent.errors import OmnigentError
from omnigent.stores.profile_protection_registry import (
    ProfileProtectionRegistryStore,
    empty_profile_protection_payload,
)


def test_registry_is_shared_between_store_instances(db_uri: str) -> None:
    first = ProfileProtectionRegistryStore(db_uri)
    second = ProfileProtectionRegistryStore(db_uri)

    with first.locked() as payload:
        payload["generation"] = 3
        payload["profiles"] = [{"profile_id": "private"}]

    assert second.read()["generation"] == 3
    assert second.read()["profiles"] == [{"profile_id": "private"}]


def test_seed_rejects_divergent_legacy_state(db_uri: str) -> None:
    store = ProfileProtectionRegistryStore(db_uri)
    legacy = {"generation": 2, "profiles": [{"profile_id": "legacy"}], "leases": []}

    store.seed_if_empty(legacy)
    store.seed_if_empty(legacy)
    with pytest.raises(OmnigentError, match="disagree"):
        store.seed_if_empty({"generation": 9, "profiles": [], "leases": []})

    assert store.read()["profiles"] == legacy["profiles"]
    assert store.read()["generation"] == legacy["generation"]
    assert store.read() != empty_profile_protection_payload()
