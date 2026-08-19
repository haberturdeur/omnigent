"""Tests for profile persistence and legacy-row adoption."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from multiprocessing import get_context
from threading import Barrier, Event
from typing import Protocol

import pytest
from sqlalchemy.exc import IntegrityError

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.conversation_store import ConversationProjectProfileMismatchError
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.profile_store.sqlalchemy_store import SqlAlchemyProfileStore
from omnigent.stores.project_store import (
    LegacyProjectMembershipChangedError,
    ProjectDestinationProfileChangedError,
    ProjectSessionProfileMismatchError,
    ProjectWorkspaceRelocationError,
)
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


def test_profile_round_trips_defaults_and_protection(store: SqlAlchemyProfileStore) -> None:
    profile = store.create(
        _uid("private"),
        "Private",
        None,
        config={"host_id": "host-1", "workspace": "/work"},
        protection={"lock": "device", "notification_content": "generic"},
    )

    assert profile.config["host_id"] == "host-1"
    assert store.get_protection(profile.id)["notification_content"] == "generic"
    assert store.list_protected_profile_ids() == frozenset({profile.id})


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


def test_project_and_member_sessions_move_between_profiles(
    store: SqlAlchemyProfileStore,
    db_uri: str,
) -> None:
    source = store.create(_uid("move-source"), "Source", None)
    destination = store.create(_uid("move-destination"), "Destination", None)
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    project = projects.create(_uid("movable-project"), "Movable", None, profile_id=source.id)
    member = conversations.create_conversation(
        profile_id=source.id,
        workspace="/work/movable",
    )
    conversations.set_conversation_project(member.id, project.id)

    validated: list[str] = []
    moved_project = projects.update_with_sessions(
        project.id,
        user_id=None,
        expected_profile_id=source.id,
        destination_profile_id=destination.id,
        validate_workspace=lambda workspace, _host_id: validated.append(workspace),
    )

    assert moved_project is not None and moved_project.profile_id == destination.id
    assert validated == ["/work/movable"]
    moved_member = conversations.get_conversation(member.id)
    assert moved_member is not None
    assert moved_member.profile_id == destination.id


def test_standalone_session_tree_moves_between_profiles(
    store: SqlAlchemyProfileStore,
    db_uri: str,
) -> None:
    """A guarded standalone move updates the parent and all sub-agent rows."""
    source = store.create(_uid("session-source"), "Source", None)
    destination = store.create(_uid("session-destination"), "Destination", None)
    conversations = SqlAlchemyConversationStore(db_uri)
    parent = conversations.create_conversation(profile_id=source.id, workspace="/work/app")
    child = conversations.create_conversation(
        profile_id=source.id,
        parent_conversation_id=parent.id,
        workspace="/work/app",
    )

    assert conversations.move_conversations_to_profile(
        (parent.id, child.id),
        expected_profile_id=source.id,
        destination_profile_id=destination.id,
    )
    moved_parent = conversations.get_conversation(parent.id)
    moved_child = conversations.get_conversation(child.id)
    assert moved_parent is not None and moved_parent.profile_id == destination.id
    assert moved_child is not None and moved_child.profile_id == destination.id


def test_individual_profile_move_rejects_project_member(
    store: SqlAlchemyProfileStore,
    db_uri: str,
) -> None:
    """Project members can only change profile through the project move."""
    source = store.create(_uid("member-source"), "Source", None)
    destination = store.create(_uid("member-destination"), "Destination", None)
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    project = projects.create(_uid("member-project"), "Project", None, profile_id=source.id)
    member = conversations.create_conversation(profile_id=source.id)
    conversations.set_conversation_project(member.id, project.id)

    with pytest.raises(ConversationProjectProfileMismatchError):
        conversations.move_conversations_to_profile(
            (member.id,),
            expected_profile_id=source.id,
            destination_profile_id=destination.id,
        )


def test_project_folder_move_rewrites_project_and_member_workspaces(
    store: SqlAlchemyProfileStore,
    db_uri: str,
) -> None:
    """Folder relocation preserves each member's path below the project root."""
    source = store.create(_uid("folder-source"), "Source", None)
    destination = store.create(_uid("folder-destination"), "Destination", None)
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    project = projects.create(
        _uid("folder-project"),
        "Movable",
        None,
        config={"workspace": "/work/app", "host_id": "host-1"},
        profile_id=source.id,
    )
    member = conversations.create_conversation(
        profile_id=source.id,
        workspace="/work/app/packages/mobile",
    )
    conversations.set_conversation_project(member.id, project.id)

    moved = projects.update_with_sessions(
        project.id,
        user_id=None,
        expected_profile_id=source.id,
        destination_profile_id=destination.id,
        workspace_relocation=("/work/app", "/private/app"),
    )

    moved_member = conversations.get_conversation(member.id)
    assert moved is not None
    assert moved.config == {"workspace": "/private/app", "host_id": "host-1"}
    assert moved_member is not None
    assert moved_member.workspace == "/private/app/packages/mobile"


def test_project_folder_move_rejects_member_outside_project_root(
    store: SqlAlchemyProfileStore,
    db_uri: str,
) -> None:
    """An inconsistent member path aborts every metadata change."""
    source = store.create(_uid("outside-source"), "Source", None)
    destination = store.create(_uid("outside-destination"), "Destination", None)
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    project = projects.create(
        _uid("outside-project"),
        "Movable",
        None,
        config={"workspace": "/work/app"},
        profile_id=source.id,
    )
    member = conversations.create_conversation(
        profile_id=source.id,
        workspace="/other/repository",
    )
    conversations.set_conversation_project(member.id, project.id)

    with pytest.raises(ProjectWorkspaceRelocationError):
        projects.update_with_sessions(
            project.id,
            user_id=None,
            expected_profile_id=source.id,
            destination_profile_id=destination.id,
            workspace_relocation=("/work/app", "/private/app"),
        )

    unchanged = projects.get(project.id, user_id=None)
    unchanged_member = conversations.get_conversation(member.id)
    assert unchanged is not None and unchanged.profile_id == source.id
    assert unchanged.config["workspace"] == "/work/app"
    assert unchanged_member is not None
    assert unchanged_member.profile_id == source.id
    assert unchanged_member.workspace == "/other/repository"


def test_project_move_rolls_back_project_and_members_on_validation_failure(
    store: SqlAlchemyProfileStore,
    db_uri: str,
) -> None:
    source = store.create(_uid("rollback-source"), "Source", None)
    destination = store.create(_uid("rollback-destination"), "Destination", None)
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    project = projects.create(_uid("rollback-project"), "Before", None, profile_id=source.id)
    member = conversations.create_conversation(profile_id=source.id, workspace="/blocked")
    conversations.set_conversation_project(member.id, project.id)

    def reject_workspace(_workspace: str, _host_id: str | None) -> None:
        raise ValueError("blocked workspace")

    with pytest.raises(ValueError, match="blocked workspace"):
        projects.update_with_sessions(
            project.id,
            user_id=None,
            expected_profile_id=source.id,
            destination_profile_id=destination.id,
            name="After",
            config={"workspace": "/allowed"},
            validate_workspace=reject_workspace,
        )

    unchanged = projects.get(project.id, user_id=None)
    unchanged_member = conversations.get_conversation(member.id)
    assert unchanged is not None
    assert (unchanged.name, unchanged.profile_id, unchanged.config) == (
        "Before",
        source.id,
        {},
    )
    assert unchanged_member is not None
    assert unchanged_member.profile_id == source.id


def test_filing_racing_a_project_move_observes_the_destination_profile(
    store: SqlAlchemyProfileStore,
    db_uri: str,
) -> None:
    source = store.create(_uid("race-source"), "Source", None)
    destination = store.create(_uid("race-destination"), "Destination", None)
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    project = projects.create(_uid("race-project"), "Race", None, profile_id=source.id)
    existing = conversations.create_conversation(profile_id=source.id, workspace="/existing")
    joining = conversations.create_conversation(profile_id=source.id, workspace="/joining")
    conversations.set_conversation_project(existing.id, project.id)
    move_locked = Event()
    release_move = Event()

    def pause_with_project_lock(_workspace: str, _host_id: str | None) -> None:
        move_locked.set()
        assert release_move.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=2) as executor:
        move_future = executor.submit(
            projects.update_with_sessions,
            project.id,
            user_id=None,
            expected_profile_id=source.id,
            destination_profile_id=destination.id,
            validate_workspace=pause_with_project_lock,
        )
        assert move_locked.wait(timeout=5)
        file_future = executor.submit(
            projects.file_session,
            project.id,
            joining.id,
            user_id=None,
        )
        release_move.set()
        assert move_future.result(timeout=5) is not None
        with pytest.raises(ProjectSessionProfileMismatchError):
            file_future.result(timeout=5)

    joining_after = conversations.get_conversation(joining.id)
    assert joining_after is not None
    assert joining_after.project_id is None
    assert joining_after.profile_id == source.id


def test_destination_profile_delete_waits_for_project_move(
    store: SqlAlchemyProfileStore,
    db_uri: str,
) -> None:
    """A destination cannot disappear after a move has locked and validated it."""
    source = store.create(_uid("delete-race-source"), "Source", None)
    destination = store.create(_uid("delete-race-destination"), "Destination", None)
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    project = projects.create(_uid("delete-race-project"), "Moving", None, profile_id=source.id)
    member = conversations.create_conversation(
        profile_id=source.id,
        workspace="/moving",
    )
    conversations.set_conversation_project(member.id, project.id)
    move_locked = Event()
    release_move = Event()

    def pause_after_destination_lock(_workspace: str, _host_id: str | None) -> None:
        move_locked.set()
        assert release_move.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=2) as executor:
        move_future = executor.submit(
            projects.update_with_sessions,
            project.id,
            user_id=None,
            expected_profile_id=source.id,
            destination_profile_id=destination.id,
            validate_workspace=pause_after_destination_lock,
        )
        assert move_locked.wait(timeout=5)
        delete_future = executor.submit(store.delete, destination.id, user_id=None)
        assert not delete_future.done()
        release_move.set()
        assert move_future.result(timeout=5) is not None
        with pytest.raises(OmnigentError) as delete_error:
            delete_future.result(timeout=5)

    assert delete_error.value.code == ErrorCode.CONFLICT
    assert store.get(destination.id, user_id=None) is not None
    moved_project = projects.get(project.id, user_id=None)
    moved_member = conversations.get_conversation(member.id)
    assert moved_project is not None and moved_project.profile_id == destination.id
    assert moved_member is not None and moved_member.profile_id == destination.id


def test_project_move_rejects_a_missing_destination_profile(
    store: SqlAlchemyProfileStore,
    db_uri: str,
) -> None:
    """The store revalidates destination existence in the move transaction."""
    source = store.create(_uid("missing-destination-source"), "Source", None)
    destination = store.create(_uid("missing-destination"), "Destination", None)
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    project = projects.create(
        _uid("missing-destination-project"), "Moving", None, profile_id=source.id
    )
    member = conversations.create_conversation(profile_id=source.id)
    conversations.set_conversation_project(member.id, project.id)
    assert store.delete(destination.id, user_id=None)

    with pytest.raises(ProjectDestinationProfileChangedError):
        projects.update_with_sessions(
            project.id,
            user_id=None,
            expected_profile_id=source.id,
            destination_profile_id=destination.id,
        )

    unchanged_project = projects.get(project.id, user_id=None)
    unchanged_member = conversations.get_conversation(member.id)
    assert unchanged_project is not None and unchanged_project.profile_id == source.id
    assert unchanged_member is not None and unchanged_member.profile_id == source.id


def test_project_move_revalidates_legacy_membership_before_writing(
    store: SqlAlchemyProfileStore,
    db_uri: str,
) -> None:
    """A changed legacy snapshot aborts the whole project move."""
    source = store.create(_uid("revalidate-source"), "Source", None)
    destination = store.create(_uid("revalidate-destination"), "Destination", None)
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    project = projects.create(_uid("revalidate-project"), "Legacy", None, profile_id=source.id)
    member = conversations.create_conversation(profile_id=source.id)
    snapshots = iter(((member.id,), ()))

    with pytest.raises(LegacyProjectMembershipChangedError):
        projects.update_with_sessions(
            project.id,
            user_id=None,
            expected_profile_id=source.id,
            destination_profile_id=destination.id,
            resolve_legacy_session_ids=lambda _name, _profile_id, _project_id: next(snapshots),
        )

    unchanged_project = projects.get(project.id, user_id=None)
    unchanged_member = conversations.get_conversation(member.id)
    assert unchanged_project is not None
    assert unchanged_project.profile_id == source.id
    assert unchanged_member is not None
    assert unchanged_member.profile_id == source.id
    assert unchanged_member.project_id is None


def test_legacy_label_removal_racing_move_waits_for_adoption(
    store: SqlAlchemyProfileStore,
    db_uri: str,
) -> None:
    """A concurrent legacy-label delete cannot split adoption from the move."""
    source = store.create(_uid("legacy-race-source"), "Source", None)
    destination = store.create(_uid("legacy-race-destination"), "Destination", None)
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    project = projects.create(
        _uid("legacy-race-project"), "Legacy race", None, profile_id=source.id
    )
    member = conversations.create_conversation(
        profile_id=source.id,
        workspace="/legacy-race",
    )
    conversations.set_labels(member.id, {"omni_project": project.name})
    move_locked = Event()
    release_move = Event()

    def pause_after_member_lock(_workspace: str, _host_id: str | None) -> None:
        move_locked.set()
        assert release_move.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=2) as executor:
        move_future = executor.submit(
            projects.update_with_sessions,
            project.id,
            user_id=None,
            expected_profile_id=source.id,
            destination_profile_id=destination.id,
            resolve_legacy_session_ids=conversations.get_legacy_project_session_ids,
            validate_workspace=pause_after_member_lock,
        )
        assert move_locked.wait(timeout=5)
        delete_future = executor.submit(
            conversations.delete_label,
            member.id,
            "omni_project",
        )
        assert not delete_future.done()
        release_move.set()
        assert move_future.result(timeout=5) is not None
        delete_future.result(timeout=5)

    moved_member = conversations.get_conversation(member.id)
    assert moved_member is not None
    assert moved_member.profile_id == destination.id
    assert moved_member.project_id == project.id


def test_split_store_label_writer_waits_for_project_adoption(
    split_db_conversation_store: SqlAlchemyConversationStore,
) -> None:
    """The AP database lock gives a concurrent label write a deterministic order."""
    conversations = split_db_conversation_store
    profiles = SqlAlchemyProfileStore(conversations.storage_location)
    projects = SqlAlchemyProjectStore(conversations.storage_location)
    source = profiles.create(_uid("split-lock-source"), "Source", None)
    destination = profiles.create(_uid("split-lock-destination"), "Destination", None)
    project = projects.create(_uid("split-lock-project"), "Legacy", None, profile_id=source.id)
    adopted = conversations.create_conversation(
        profile_id=source.id,
        workspace="/adopted",
    )
    later = conversations.create_conversation(profile_id=source.id)
    conversations.set_labels(adopted.id, {"omni_project": project.name})
    move_locked = Event()
    release_move = Event()

    def pause_after_membership_snapshot(_workspace: str, _host_id: str | None) -> None:
        move_locked.set()
        assert release_move.wait(timeout=5)

    process_context = get_context("spawn")
    writer_started = process_context.Event()
    writer_finished = process_context.Event()
    assert conversations.conversation_storage_location is not None
    with ThreadPoolExecutor(max_workers=1) as executor:
        move_future = executor.submit(
            projects.update_with_sessions,
            project.id,
            user_id=None,
            expected_profile_id=source.id,
            destination_profile_id=destination.id,
            resolve_legacy_session_ids=conversations.get_legacy_project_session_ids,
            hold_legacy_membership_lock=conversations.hold_legacy_project_membership_lock,
            validate_workspace=pause_after_membership_snapshot,
        )
        assert move_locked.wait(timeout=5)
        label_process = process_context.Process(
            target=_set_project_label_in_process,
            args=(
                conversations.storage_location,
                conversations.conversation_storage_location,
                later.id,
                project.name,
                writer_started,
                writer_finished,
            ),
        )
        label_process.start()
        assert writer_started.wait(timeout=5)
        assert not writer_finished.wait(timeout=0.25)
        release_move.set()
        assert move_future.result(timeout=5) is not None
        assert writer_finished.wait(timeout=5)
        label_process.join(timeout=5)
        assert label_process.exitcode == 0

    adopted_after = conversations.get_conversation(adopted.id)
    later_after = conversations.get_conversation(later.id)
    assert adopted_after is not None
    assert adopted_after.profile_id == destination.id
    assert adopted_after.project_id == project.id
    assert later_after is not None
    assert later_after.profile_id == source.id
    assert later_after.project_id is None
    assert later_after.labels["omni_project"] == project.name


def test_split_store_label_cleanup_waits_for_membership_commit(
    store: SqlAlchemyProfileStore,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed owning transaction never runs external label cleanup."""
    profile = store.create(_uid("cleanup-order-profile"), "Profile", None)
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    project = projects.create(
        _uid("cleanup-order-project"), "Project", None, profile_id=profile.id
    )
    member = conversations.create_conversation(profile_id=profile.id)
    cleanup_calls: list[str] = []
    real_session_immediate = projects._session_immediate

    @contextmanager
    def fail_before_commit(operation: str) -> Iterator[object]:
        with real_session_immediate(operation) as session:
            yield session
            raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(projects, "_session_immediate", fail_before_commit)

    with pytest.raises(RuntimeError, match="simulated commit failure"):
        projects.file_session(
            project.id,
            member.id,
            user_id=None,
            clear_legacy_label=True,
            legacy_label_is_colocated=False,
            clear_legacy_label_fallback=lambda: cleanup_calls.append(member.id),
        )

    assert cleanup_calls == []
    unchanged = conversations.get_conversation(member.id)
    assert unchanged is not None
    assert unchanged.project_id is None


def test_split_store_label_cleanup_can_be_retried_after_adoption_commit(
    split_db_conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A transient AP cleanup failure is repaired by retrying the same adoption."""
    conversations = split_db_conversation_store
    profiles = SqlAlchemyProfileStore(conversations.storage_location)
    projects = SqlAlchemyProjectStore(conversations.storage_location)
    source = profiles.create(_uid("retry-cleanup-source"), "Source", None)
    destination = profiles.create(_uid("retry-cleanup-destination"), "Destination", None)
    project = projects.create(_uid("retry-cleanup-project"), "Legacy", None, profile_id=source.id)
    member = conversations.create_conversation(profile_id=source.id)
    conversations.set_labels(member.id, {"omni_project": project.name})
    cleanup_attempts = 0

    def flaky_cleanup(session_ids: tuple[str, ...]) -> None:
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        if cleanup_attempts == 1:
            raise RuntimeError("temporary AP failure")
        conversations.delete_legacy_project_labels(session_ids)

    with pytest.raises(RuntimeError, match="temporary AP failure"):
        projects.update_with_sessions(
            project.id,
            user_id=None,
            expected_profile_id=source.id,
            destination_profile_id=destination.id,
            resolve_legacy_session_ids=conversations.get_legacy_project_session_ids,
            hold_legacy_membership_lock=conversations.hold_legacy_project_membership_lock,
            clear_legacy_labels=flaky_cleanup,
        )

    committed = conversations.get_conversation(member.id)
    assert committed is not None
    assert committed.profile_id == destination.id
    assert committed.project_id == project.id
    assert committed.labels["omni_project"] == project.name

    retried = projects.update_with_sessions(
        project.id,
        user_id=None,
        expected_profile_id=destination.id,
        destination_profile_id=destination.id,
        resolve_legacy_session_ids=conversations.get_legacy_project_session_ids,
        hold_legacy_membership_lock=conversations.hold_legacy_project_membership_lock,
        clear_legacy_labels=flaky_cleanup,
    )

    assert retried is not None
    cleaned = conversations.get_conversation(member.id)
    assert cleaned is not None
    assert cleaned.profile_id == destination.id
    assert cleaned.project_id == project.id
    assert "omni_project" not in cleaned.labels
    assert cleanup_attempts == 2


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
