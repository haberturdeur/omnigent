"""Tests for profile persistence and legacy-row adoption."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Protocol

import pytest
from sqlalchemy.exc import IntegrityError

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.profile_store.sqlalchemy_store import SqlAlchemyProfileStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore


def _uid(seed: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


class _SettableEvent(Protocol):
    def set(self) -> None: ...


def _set_project_label_in_process(
    storage_location: str,
    conversation_storage_location: str,
    conversation_id: str,
    project_name: str,
    started: _SettableEvent,
    finished: _SettableEvent,
) -> None:
    store = SqlAlchemyConversationStore(storage_location, conversation_storage_location)
    started.set()
    store.set_labels(conversation_id, {"omni_project": project_name})
    finished.set()


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyProfileStore:
    return SqlAlchemyProfileStore(db_uri)


def test_list_creates_one_default_profile(store: SqlAlchemyProfileStore) -> None:
    first = store.list(user_id="alice@example.com")
    second = store.list(user_id="alice@example.com")

    assert [(profile.name, profile.is_default) for profile in first] == [("Personal", True)]
    assert [profile.id for profile in second] == [first[0].id]


@pytest.mark.parametrize("user_id", [None, "alice@example.com"])
def test_concurrent_callers_share_one_default_profile(db_uri: str, user_id: str | None) -> None:
    callers = 8
    ready = Barrier(callers)

    def ensure_default() -> str:
        profile_store = SqlAlchemyProfileStore(db_uri)
        ready.wait()
        return profile_store.ensure_default(user_id=user_id).id

    with ThreadPoolExecutor(max_workers=callers) as executor:
        profile_ids = list(executor.map(lambda _index: ensure_default(), range(callers)))

    assert len(set(profile_ids)) == 1
    profiles = SqlAlchemyProfileStore(db_uri).list(user_id=user_id)
    assert [(profile.id, profile.is_default) for profile in profiles] == [(profile_ids[0], True)]


def test_profiles_are_owner_scoped(store: SqlAlchemyProfileStore) -> None:
    store.create(_uid("alice-work"), "Work", "alice@example.com")
    store.create(_uid("bob-work"), "Work", "bob@example.com")

    assert {profile.name for profile in store.list(user_id="alice@example.com")} == {
        "Personal",
        "Work",
    }
    assert store.get(_uid("alice-work"), user_id="bob@example.com") is None


def test_profile_name_constraint_closes_preflight_race(
    store: SqlAlchemyProfileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.create(_uid("first-work"), "Work", "alice@example.com")
    monkeypatch.setattr(store, "_name_taken", lambda *args, **kwargs: False)

    with pytest.raises(OmnigentError) as duplicate:
        store.create(_uid("second-work"), "Work", "alice@example.com")

    assert duplicate.value.code == ErrorCode.ALREADY_EXISTS


def test_profile_name_error_translation_does_not_mask_primary_key_collision(
    store: SqlAlchemyProfileStore,
) -> None:
    profile_id = _uid("profile-id-collision")
    store.create(profile_id, "First", None)

    with pytest.raises(IntegrityError):
        store.create(profile_id, "Second", None)


def test_project_names_are_unique_within_each_profile(
    store: SqlAlchemyProfileStore,
    db_uri: str,
) -> None:
    profiles = [
        store.create(_uid("profile-a"), "A", None),
        store.create(_uid("profile-b"), "B", None),
    ]
    projects = SqlAlchemyProjectStore(db_uri)

    first = projects.create(_uid("project-a"), "Dashboard", None, profile_id=profiles[0].id)
    second = projects.create(_uid("project-b"), "Dashboard", None, profile_id=profiles[1].id)

    assert first.name == second.name == "Dashboard"


def test_default_profile_adopts_legacy_single_user_rows(
    store: SqlAlchemyProfileStore,
    db_uri: str,
) -> None:
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    project = projects.create(_uid("legacy-project"), "Legacy", None)
    conversation = conversations.create_conversation(title="Legacy session")

    default = store.ensure_default(user_id=None)

    adopted_project = projects.get(project.id, user_id=None)
    adopted_conversation = conversations.get_conversation(conversation.id)
    assert adopted_project is not None
    assert adopted_conversation is not None
    assert adopted_project.profile_id == default.id
    assert adopted_conversation.profile_id == default.id


def test_default_and_nonempty_profiles_cannot_be_deleted(
    store: SqlAlchemyProfileStore,
    db_uri: str,
) -> None:
    default = store.ensure_default(user_id=None)
    with pytest.raises(OmnigentError) as default_error:
        store.delete(default.id, user_id=None)
    assert default_error.value.code == ErrorCode.CONFLICT

    profile = store.create(_uid("work"), "Work", None)
    SqlAlchemyProjectStore(db_uri).create(
        _uid("work-project"), "Project", None, profile_id=profile.id
    )
    with pytest.raises(OmnigentError) as nonempty_error:
        store.delete(profile.id, user_id=None)
    assert nonempty_error.value.code == ErrorCode.CONFLICT
