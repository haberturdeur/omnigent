"""Tests for the projects CRUD routes (``/v1/projects``).

The projects router is only mounted when ``create_app`` receives a
``project_store``. The standard conftest ``app`` fixture does not supply one, so
these tests build their own app/client that include it.

Two auth setups are exercised:

- **Single-user** (``project_client``) — no auth provider, so the owner scope is
  the reserved ``None``. This is the OSS / local default.
- **Multi-user** (``multi_user_client`` + ``as_user``) — header auth
  (``UnifiedAuthProvider(source="header")``), so each request's owner is the
  ``X-Forwarded-Email`` identity. Used to prove projects are owner-private: one
  user can never see or mutate another's projects.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import HostHelloFrame, HostMoveDirFrame, decode_host_frame
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import RESERVED_USER_LOCAL, UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)
from omnigent.stores.profile_store.sqlalchemy_store import SqlAlchemyProfileStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

ALICE = "alice@example.com"
BOB = "bob@example.com"


def _as_user(user: str) -> dict[str, str]:
    """Header identifying the requesting user under header auth."""
    return {"X-Forwarded-Email": user}


@pytest.fixture()
def project_app(
    runtime_init: None, db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> FastAPI:
    """Build a FastAPI app that includes the project store."""
    monkeypatch.setenv(
        "OMNIGENT_PROFILE_PROTECTION_PATH", str(tmp_path / "profile-protection.json")
    )
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    project_store = SqlAlchemyProjectStore(db_uri)
    profile_store = SqlAlchemyProfileStore(db_uri)
    host_store = HostStore(db_uri)
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        project_store=project_store,
        profile_store=profile_store,
        host_store=host_store,
    )
    app.state.test_project_store = project_store
    app.state.test_profile_store = profile_store
    app.state.test_host_store = host_store
    return app


@pytest_asyncio.fixture()
async def project_client(
    project_app: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the project-enabled app."""
    transport = httpx.ASGITransport(app=project_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture()
async def split_project_client(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[httpx.AsyncClient, SqlAlchemyConversationStore]]:
    """Project client with labels stored in a separate Agent Platform database."""
    monkeypatch.setenv(
        "OMNIGENT_PROFILE_PROTECTION_PATH", str(tmp_path / "split-profile-protection.json")
    )
    artifact_store = LocalArtifactStore(str(tmp_path / "split-artifacts"))
    conversations = SqlAlchemyConversationStore(
        db_uri,
        f"sqlite:///{tmp_path / 'agent-platform.db'}",
    )
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conversations,
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "split-cache",
        ),
        project_store=SqlAlchemyProjectStore(db_uri),
        profile_store=SqlAlchemyProfileStore(db_uri),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, conversations


async def test_create_project(project_client: httpx.AsyncClient) -> None:
    """Creating a project returns the project object."""
    resp = await project_client.post("/v1/projects", json={"name": "My Project"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "My Project"
    assert body["object"] == "project"
    assert len(body["id"]) == 32
    assert body["updated_at"] is None


async def test_create_trims_and_rejects_empty(project_client: httpx.AsyncClient) -> None:
    """Names are trimmed; empty/whitespace-only names are rejected with 422."""
    resp = await project_client.post("/v1/projects", json={"name": "  Padded  "})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Padded"

    resp = await project_client.post("/v1/projects", json={"name": "   "})
    assert resp.status_code == 422


async def test_create_duplicate_name_conflicts(project_client: httpx.AsyncClient) -> None:
    """Two projects with the same name for one owner returns 409."""
    await project_client.post("/v1/projects", json={"name": "dup"})
    resp = await project_client.post("/v1/projects", json={"name": "dup"})
    assert resp.status_code == 409


async def test_list_projects(project_client: httpx.AsyncClient) -> None:
    """Listing returns the created projects."""
    await project_client.post("/v1/projects", json={"name": "A"})
    await project_client.post("/v1/projects", json={"name": "B"})
    resp = await project_client.get("/v1/projects")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert {p["name"] for p in body["data"]} == {"A", "B"}


async def test_profiles_crud_and_project_partition(project_client: httpx.AsyncClient) -> None:
    """Profiles persist defaults and partition project listings."""
    profiles = (await project_client.get("/v1/profiles")).json()["data"]
    assert [(profile["name"], profile["is_default"]) for profile in profiles] == [
        ("Personal", True)
    ]
    personal_id = profiles[0]["id"]

    work_response = await project_client.post(
        "/v1/profiles",
        json={
            "name": "Work",
            "config": {"host_id": "host-1", "workspace": "/work"},
            "protection": {"notification_content": "generic"},
        },
    )
    assert work_response.status_code == 200
    work = work_response.json()

    personal_project = (
        await project_client.post(
            "/v1/projects", json={"name": "Dashboard", "profile_id": personal_id}
        )
    ).json()
    work_project = (
        await project_client.post(
            "/v1/projects", json={"name": "Dashboard", "profile_id": work["id"]}
        )
    ).json()
    listed = (await project_client.get(f"/v1/projects?profile_id={work['id']}")).json()
    assert [project["id"] for project in listed["data"]] == [work_project["id"]]
    assert personal_project["id"] != work_project["id"]

    updated = await project_client.patch(
        f"/v1/profiles/{work['id']}", json={"name": "Client work"}
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Client work"


async def test_direct_profile_creation_establishes_personal_default(
    project_client: httpx.AsyncClient,
) -> None:
    created = await project_client.post("/v1/profiles", json={"name": "Work"})

    assert created.status_code == 200
    profiles = (await project_client.get("/v1/profiles")).json()["data"]
    assert [(profile["name"], profile["is_default"]) for profile in profiles] == [
        ("Personal", True),
        ("Work", False),
    ]


async def test_concurrent_direct_profile_creation_shares_personal_default(
    project_client: httpx.AsyncClient,
) -> None:
    responses = await asyncio.gather(
        project_client.post("/v1/profiles", json={"name": "Work"}),
        project_client.post("/v1/profiles", json={"name": "Home"}),
    )

    assert [response.status_code for response in responses] == [200, 200]
    profiles = (await project_client.get("/v1/profiles")).json()["data"]
    defaults = [profile for profile in profiles if profile["is_default"]]
    assert [(profile["name"], profile["is_default"]) for profile in defaults] == [
        ("Personal", True)
    ]
    assert {profile["name"] for profile in profiles} == {"Personal", "Work", "Home"}


async def test_move_project_moves_its_sessions_between_profiles(
    project_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Moving a project moves its first-class container and every member session."""
    profiles = (await project_client.get("/v1/profiles")).json()["data"]
    source_id = profiles[0]["id"]
    destination = (await project_client.post("/v1/profiles", json={"name": "Destination"})).json()
    project = (
        await project_client.post(
            "/v1/projects", json={"name": "Movable", "profile_id": source_id}
        )
    ).json()
    conversation_store = SqlAlchemyConversationStore(db_uri)
    conversation = conversation_store.create_conversation(
        profile_id=source_id,
    )
    conversation_store.set_conversation_project(conversation.id, project["id"])
    legacy = conversation_store.create_conversation(profile_id=source_id)
    conversation_store.set_labels(legacy.id, {"omni_project": "Movable"})

    moved = await project_client.patch(
        f"/v1/projects/{project['id']}",
        json={"profile_id": destination["id"]},
    )

    assert moved.status_code == 200
    assert moved.json()["profile_id"] == destination["id"]
    moved_conversation = conversation_store.get_conversation(conversation.id)
    assert moved_conversation is not None
    assert moved_conversation.profile_id == destination["id"]
    moved_legacy = conversation_store.get_conversation(legacy.id)
    assert moved_legacy is not None
    assert moved_legacy.profile_id == destination["id"]
    assert moved_legacy.project_id == project["id"]
    source_projects = (await project_client.get(f"/v1/projects?profile_id={source_id}")).json()[
        "data"
    ]
    destination_projects = (
        await project_client.get(f"/v1/projects?profile_id={destination['id']}")
    ).json()["data"]
    assert source_projects == []
    assert [item["id"] for item in destination_projects] == [project["id"]]


async def test_protection_rejects_existing_project_outside_roots(
    project_client: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    """Enabling protection cannot strand an existing project outside its roots."""
    profile = (await project_client.post("/v1/profiles", json={"name": "Private"})).json()
    outside = tmp_path / "outside-project"
    outside.mkdir()
    root = tmp_path / "protected"
    root.mkdir()
    await project_client.post(
        "/v1/projects",
        json={
            "name": "Existing",
            "profile_id": profile["id"],
            "config": {"workspace": str(outside)},
        },
    )

    response = await project_client.put(
        f"/v1/profiles/{profile['id']}/protection",
        json={"passcode": "secret", "protected_roots": [str(root)]},
    )

    assert response.status_code == 400
    assert "existing project workspace" in response.text.lower()


async def test_private_profile_protection_binds_to_configured_host(
    project_client: httpx.AsyncClient,
    project_app: FastAPI,
) -> None:
    """Protection uses profile host_id and prevents later host detachment."""
    host_id = "a" * 32
    project_app.state.test_host_store.upsert_on_connect(
        host_id,
        "remote-host",
        RESERVED_USER_LOCAL,
    )
    profile = (
        await project_client.post(
            "/v1/profiles",
            json={"name": "Remote private", "config": {"host_id": host_id}},
        )
    ).json()
    protected = await project_client.put(
        f"/v1/profiles/{profile['id']}/protection",
        json={"passcode": "secret", "protected_roots": ["/srv/private"]},
    )
    assert protected.status_code == 200

    unlocked = await project_client.post(
        f"/v1/profiles/{profile['id']}/unlock",
        json={"passcode": "secret"},
    )
    token = unlocked.json()["token"]
    detached = await project_client.patch(
        f"/v1/profiles/{profile['id']}",
        json={"config": {"host_id": "b" * 32}},
        headers={"X-Omnigent-Profile-Unlock": token},
    )

    assert detached.status_code == 409
    assert "before changing its host" in detached.text


async def test_private_profile_rejects_unknown_configured_host(
    project_client: httpx.AsyncClient,
) -> None:
    """An arbitrary host id cannot arm private-root isolation for that host."""
    profile = (
        await project_client.post(
            "/v1/profiles",
            json={"name": "Unknown host", "config": {"host_id": "c" * 32}},
        )
    ).json()

    protected = await project_client.put(
        f"/v1/profiles/{profile['id']}/protection",
        json={"passcode": "secret", "protected_roots": ["/srv/private"]},
    )

    assert protected.status_code == 404
    assert protected.json()["error"]["code"] == "not_found"


async def test_protection_rejects_existing_session_outside_roots(
    project_client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
) -> None:
    """Enabling protection validates unfiled sessions as well as projects."""
    profile = (await project_client.post("/v1/profiles", json={"name": "Private"})).json()
    outside = tmp_path / "outside-session"
    outside.mkdir()
    root = tmp_path / "protected"
    root.mkdir()
    SqlAlchemyConversationStore(db_uri).create_conversation(
        profile_id=profile["id"],
        workspace=str(outside),
    )

    response = await project_client.put(
        f"/v1/profiles/{profile['id']}/protection",
        json={"passcode": "secret", "protected_roots": [str(root)]},
    )

    assert response.status_code == 400
    assert "existing session workspace" in response.text.lower()


async def test_rename_project_adopts_and_clears_legacy_members(
    project_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """One project PATCH renames the row and adopts label-only members."""
    profile_id = (await project_client.get("/v1/profiles")).json()["data"][0]["id"]
    project = (
        await project_client.post(
            "/v1/projects", json={"name": "Before", "profile_id": profile_id}
        )
    ).json()
    conversations = SqlAlchemyConversationStore(db_uri)
    legacy = conversations.create_conversation(profile_id=profile_id)
    conversations.set_labels(legacy.id, {"omni_project": "Before", "keep": "yes"})

    response = await project_client.patch(f"/v1/projects/{project['id']}", json={"name": "After"})

    assert response.status_code == 200
    assert response.json()["name"] == "After"
    adopted = conversations.get_conversation(legacy.id)
    assert adopted is not None
    assert adopted.project_id == project["id"]
    assert adopted.labels == {"keep": "yes"}


async def test_project_patch_can_adopt_a_differently_named_legacy_folder(
    project_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A promoted label-only folder is adopted without per-session PATCHes."""
    profile_id = (await project_client.get("/v1/profiles")).json()["data"][0]["id"]
    conversations = SqlAlchemyConversationStore(db_uri)
    legacy = conversations.create_conversation(profile_id=profile_id)
    conversations.set_labels(legacy.id, {"omni_project": "Legacy folder"})
    project = (
        await project_client.post(
            "/v1/projects", json={"name": "Renamed", "profile_id": profile_id}
        )
    ).json()

    response = await project_client.patch(
        f"/v1/projects/{project['id']}",
        json={"name": "Renamed", "adopt_legacy_name": "Legacy folder"},
    )

    assert response.status_code == 200
    adopted = conversations.get_conversation(legacy.id)
    assert adopted is not None
    assert adopted.project_id == project["id"]
    assert "omni_project" not in adopted.labels


async def test_split_store_move_retry_cleans_labels_after_committed_adoption(
    split_project_client: tuple[httpx.AsyncClient, SqlAlchemyConversationStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replaying the same move repairs cleanup after a transient AP failure."""
    client, conversations = split_project_client
    source_id = (await client.get("/v1/profiles")).json()["data"][0]["id"]
    destination = (await client.post("/v1/profiles", json={"name": "Destination"})).json()
    project = (
        await client.post("/v1/projects", json={"name": "Legacy", "profile_id": source_id})
    ).json()
    member = conversations.create_conversation(profile_id=source_id)
    conversations.set_labels(member.id, {"omni_project": project["name"]})
    real_cleanup = conversations.delete_legacy_project_labels
    attempts = 0

    def flaky_cleanup(session_ids: tuple[str, ...]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary AP failure")
        real_cleanup(session_ids)

    monkeypatch.setattr(conversations, "delete_legacy_project_labels", flaky_cleanup)
    payload = {"profile_id": destination["id"]}
    with pytest.raises(RuntimeError, match="temporary AP failure"):
        await client.patch(f"/v1/projects/{project['id']}", json=payload)

    committed = conversations.get_conversation(member.id)
    assert committed is not None
    assert committed.profile_id == destination["id"]
    assert committed.project_id == project["id"]
    assert committed.labels["omni_project"] == project["name"]

    retried = await client.patch(f"/v1/projects/{project['id']}", json=payload)

    assert retried.status_code == 200
    cleaned = conversations.get_conversation(member.id)
    assert cleaned is not None
    assert "omni_project" not in cleaned.labels
    assert attempts == 2


async def test_move_project_rejects_a_destination_name_collision(
    project_client: httpx.AsyncClient,
) -> None:
    """A move cannot create duplicate project names inside one profile."""
    source_id = (await project_client.get("/v1/profiles")).json()["data"][0]["id"]
    destination = (await project_client.post("/v1/profiles", json={"name": "Destination"})).json()
    source_project = (
        await project_client.post("/v1/projects", json={"name": "Same", "profile_id": source_id})
    ).json()
    await project_client.post(
        "/v1/projects", json={"name": "Same", "profile_id": destination["id"]}
    )

    response = await project_client.patch(
        f"/v1/projects/{source_project['id']}",
        json={"profile_id": destination["id"]},
    )

    assert response.status_code == 409


async def test_move_project_fails_closed_when_profile_storage_is_split(
    project_client: httpx.AsyncClient,
    project_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A move is rejected when its destination cannot join the project transaction."""
    source_id = (await project_client.get("/v1/profiles")).json()["data"][0]["id"]
    destination = (await project_client.post("/v1/profiles", json={"name": "Remote"})).json()
    project = (
        await project_client.post(
            "/v1/projects", json={"name": "Movable", "profile_id": source_id}
        )
    ).json()
    monkeypatch.setattr(
        project_app.state.test_profile_store,
        "storage_location",
        "sqlite:///separate-profile-store.db",
    )

    response = await project_client.patch(
        f"/v1/projects/{project['id']}",
        json={"profile_id": destination["id"]},
    )

    assert response.status_code == 409
    unchanged = project_app.state.test_project_store.get(project["id"], user_id=None)
    assert unchanged is not None
    assert unchanged.profile_id == source_id


async def test_move_project_requires_destination_private_profile_unlock(
    project_client: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    """A source-profile bearer cannot authorize entry into a locked destination."""
    source_id = (await project_client.get("/v1/profiles")).json()["data"][0]["id"]
    root = tmp_path / "move-private"
    private = (
        await project_client.post(
            "/v1/profiles",
            json={"name": "Private destination", "config": {"workspace": str(root)}},
        )
    ).json()
    await project_client.put(
        f"/v1/profiles/{private['id']}/protection",
        json={"passcode": "move-secret", "protected_roots": [str(root)]},
    )
    project = (
        await project_client.post(
            "/v1/projects", json={"name": "Movable", "profile_id": source_id}
        )
    ).json()

    locked = await project_client.patch(
        f"/v1/projects/{project['id']}", json={"profile_id": private["id"]}
    )
    assert locked.status_code == 404

    token = (
        await project_client.post(
            f"/v1/profiles/{private['id']}/unlock", json={"passcode": "move-secret"}
        )
    ).json()["token"]
    moved = await project_client.patch(
        f"/v1/projects/{project['id']}",
        json={"profile_id": private["id"]},
        headers={"X-Omnigent-Destination-Profile-Unlock": token},
    )
    assert moved.status_code == 200


async def test_move_project_folder_moves_files_and_rewrites_workspaces(
    project_client: httpx.AsyncClient,
    project_app: FastAPI,
    db_uri: str,
    tmp_path: Path,
) -> None:
    """The explicit folder action moves files before committing metadata."""
    host_id = "1234567890abcdef1234567890abcdef"
    source_root = tmp_path / "public"
    source_workspace = source_root / "app"
    destination_root = tmp_path / "private"
    source_workspace.mkdir(parents=True)
    destination_root.mkdir()
    (source_workspace / "README.md").write_text("preserved")

    project_app.state.test_host_store.upsert_on_connect(
        host_id,
        "folder-host",
        RESERVED_USER_LOCAL,
    )

    source_id = (await project_client.get("/v1/profiles")).json()["data"][0]["id"]
    private = (
        await project_client.post(
            "/v1/profiles",
            json={
                "name": "Private destination",
                "config": {"workspace": str(destination_root), "host_id": host_id},
            },
        )
    ).json()
    await project_client.put(
        f"/v1/profiles/{private['id']}/protection",
        json={"passcode": "move-secret", "protected_roots": [str(destination_root)]},
    )
    token = (
        await project_client.post(
            f"/v1/profiles/{private['id']}/unlock", json={"passcode": "move-secret"}
        )
    ).json()["token"]
    project = (
        await project_client.post(
            "/v1/projects",
            json={
                "name": "Movable",
                "profile_id": source_id,
                "config": {"workspace": str(source_workspace), "host_id": host_id},
            },
        )
    ).json()
    conversations = SqlAlchemyConversationStore(db_uri)
    member = conversations.create_conversation(
        profile_id=source_id,
        host_id=host_id,
        workspace=str(source_workspace / "packages" / "mobile"),
    )
    conversations.set_conversation_project(member.id, project["id"])

    class _FakeWebSocket:
        async def send_text(self, _data: str) -> None:
            return None

    conn = project_app.state.host_registry.register(
        host_id=host_id,
        ws=_FakeWebSocket(),
        hello=HostHelloFrame(
            version="test",
            frame_protocol_version=1,
            name="folder-host",
            capabilities=["move_dir"],
        ),
        owner=RESERVED_USER_LOCAL,
    )

    async def reply_to_move() -> None:
        frame_text = await conn.outbound_queue.get()
        assert isinstance(frame_text, str)
        frame = decode_host_frame(frame_text)
        assert isinstance(frame, HostMoveDirFrame)
        moved_path = shutil.move(frame.source_path, frame.destination_path)
        future = conn.pending_move_dirs.get(frame.request_id)
        assert future is not None
        future.set_result(
            {
                "status": "ok",
                "source_path": str(source_workspace.resolve()),
                "destination_path": str(Path(moved_path).resolve()),
                "error": None,
            }
        )

    reply = asyncio.create_task(reply_to_move())
    response = await project_client.post(
        f"/v1/projects/{project['id']}/move-folder",
        json={"profile_id": private["id"]},
        headers={"X-Omnigent-Destination-Profile-Unlock": token},
    )
    if response.status_code != 200:
        reply.cancel()
        await asyncio.gather(reply, return_exceptions=True)
        pytest.fail(response.text)
    await reply

    assert response.status_code == 200, response.text
    destination_workspace = destination_root / "app"
    assert (destination_workspace / "README.md").read_text() == "preserved"
    assert response.json()["config"]["workspace"] == str(destination_workspace)
    moved_member = conversations.get_conversation(member.id)
    assert moved_member is not None
    assert moved_member.profile_id == private["id"]
    assert moved_member.workspace == str(destination_workspace / "packages" / "mobile")


async def test_add_project_root_to_private_profile_moves_without_moving_files(
    project_client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
) -> None:
    """An existing folder can become another root of a private profile."""
    source_workspace = tmp_path / "public" / "app"
    original_private_root = tmp_path / "private"
    source_workspace.mkdir(parents=True)
    original_private_root.mkdir()
    source_id = (await project_client.get("/v1/profiles")).json()["data"][0]["id"]
    private = (
        await project_client.post(
            "/v1/profiles",
            json={"name": "Private", "config": {"workspace": str(original_private_root)}},
        )
    ).json()
    await project_client.put(
        f"/v1/profiles/{private['id']}/protection",
        json={"passcode": "root-secret", "protected_roots": [str(original_private_root)]},
    )
    token = (
        await project_client.post(
            f"/v1/profiles/{private['id']}/unlock", json={"passcode": "root-secret"}
        )
    ).json()["token"]
    project = (
        await project_client.post(
            "/v1/projects",
            json={
                "name": "Stay put",
                "profile_id": source_id,
                "config": {"workspace": str(source_workspace)},
            },
        )
    ).json()
    conversations = SqlAlchemyConversationStore(db_uri)
    member = conversations.create_conversation(
        profile_id=source_id,
        workspace=str(source_workspace / "packages"),
    )
    conversations.set_conversation_project(member.id, project["id"])

    response = await project_client.post(
        f"/v1/profiles/{private['id']}/protected-roots/projects",
        json={"project_id": project["id"]},
        headers={"X-Omnigent-Destination-Profile-Unlock": token},
    )

    assert response.status_code == 200, response.text
    assert response.json()["profile_id"] == private["id"]
    assert response.json()["config"]["workspace"] == str(source_workspace)
    assert source_workspace.is_dir()
    moved_member = conversations.get_conversation(member.id)
    assert moved_member is not None and moved_member.profile_id == private["id"]
    status = await project_client.get(
        f"/v1/profiles/{private['id']}/protection",
        headers={"X-Omnigent-Profile-Unlock": token},
    )
    assert status.json()["unlocked"] is False
    refreshed_token = (
        await project_client.post(
            f"/v1/profiles/{private['id']}/unlock", json={"passcode": "root-secret"}
        )
    ).json()["token"]
    roots = await project_client.get(
        f"/v1/profiles/{private['id']}/protection",
        headers={"X-Omnigent-Profile-Unlock": refreshed_token},
    )
    assert set(roots.json()["protected_roots"]) == {
        str(original_private_root.resolve()),
        str(source_workspace.resolve()),
    }


async def test_private_profile_requires_scoped_unlock(
    project_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """Locked profile collections and direct project reads reveal no content."""
    root = tmp_path / "private-workspace"
    root.mkdir()
    profile = (
        await project_client.post(
            "/v1/profiles",
            json={"name": "Private", "config": {"workspace": str(root)}},
        )
    ).json()
    configured = await project_client.put(
        f"/v1/profiles/{profile['id']}/protection",
        json={"passcode": "correct horse", "protected_roots": [str(root)]},
    )
    assert configured.status_code == 200
    assert "passcode" not in configured.text
    locked_profile = await project_client.get(f"/v1/profiles/{profile['id']}")
    assert locked_profile.json()["config"] == {}
    assert (
        await project_client.patch(
            f"/v1/profiles/{profile['id']}", json={"name": "Leaked mutation"}
        )
    ).status_code == 404
    locked_status = await project_client.get(f"/v1/profiles/{profile['id']}/protection")
    assert locked_status.json()["protected_roots"] == []

    public_profile = next(
        item
        for item in (await project_client.get("/v1/profiles")).json()["data"]
        if item["is_default"]
    )
    cross_boundary = await project_client.post(
        "/v1/projects",
        json={
            "name": "Boundary bypass",
            "profile_id": public_profile["id"],
            "config": {"workspace": str(root)},
        },
    )
    assert cross_boundary.status_code == 400

    assert (
        await project_client.get(f"/v1/projects?profile_id={profile['id']}")
    ).status_code == 404
    unlocked = await project_client.post(
        f"/v1/profiles/{profile['id']}/unlock", json={"passcode": "correct horse"}
    )
    token = unlocked.json()["token"]
    headers = {"X-Omnigent-Profile-Unlock": token}
    unlocked_status = await project_client.get(
        f"/v1/profiles/{profile['id']}/protection", headers=headers
    )
    assert unlocked_status.json()["protected_roots"] == [str(root.resolve())]
    created = await project_client.post(
        "/v1/projects",
        headers=headers,
        json={
            "name": "Hidden",
            "profile_id": profile["id"],
            "config": {"workspace": str(root / "project")},
        },
    )
    assert created.status_code == 200
    project_id = created.json()["id"]

    assert (await project_client.get(f"/v1/projects/{project_id}")).status_code == 404
    visible = await project_client.get(f"/v1/projects?profile_id={profile['id']}", headers=headers)
    assert [item["id"] for item in visible.json()["data"]] == [project_id]


async def test_disable_keeps_registry_locked_until_metadata_is_public(
    project_client: httpx.AsyncClient,
    project_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server.routes import profiles as profile_routes

    root = tmp_path / "disable-order"
    profile = (await project_client.post("/v1/profiles", json={"name": "Private"})).json()
    await project_client.put(
        f"/v1/profiles/{profile['id']}/protection",
        json={"passcode": "secret", "protected_roots": [str(root)]},
    )
    token = (
        await project_client.post(
            f"/v1/profiles/{profile['id']}/unlock",
            json={"passcode": "secret"},
        )
    ).json()["token"]
    events: list[str] = []
    store = project_app.state.test_profile_store
    original_update = store.update
    original_apply = profile_routes._apply_and_fence

    def recording_update(*args, **kwargs):
        if kwargs.get("protection") == {}:
            events.append("metadata-public")
        return original_update(*args, **kwargs)

    async def recording_apply(*args, **kwargs):
        events.append("registry-remove")
        return await original_apply(*args, **kwargs)

    monkeypatch.setattr(store, "update", recording_update)
    monkeypatch.setattr(profile_routes, "_apply_and_fence", recording_apply)

    response = await project_client.delete(
        f"/v1/profiles/{profile['id']}/protection",
        headers={"X-Omnigent-Profile-Unlock": token},
    )

    assert response.status_code == 200
    assert events == ["metadata-public", "registry-remove"]


async def test_failed_protected_profile_delete_restores_metadata(
    project_client: httpx.AsyncClient,
    project_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server.routes import profiles as profile_routes

    root = tmp_path / "delete-rollback"
    profile = (await project_client.post("/v1/profiles", json={"name": "Private"})).json()
    await project_client.put(
        f"/v1/profiles/{profile['id']}/protection",
        json={"passcode": "secret", "protected_roots": [str(root)]},
    )
    token = (
        await project_client.post(
            f"/v1/profiles/{profile['id']}/unlock",
            json={"passcode": "secret"},
        )
    ).json()["token"]

    async def fail_apply(*_args, **_kwargs):
        raise OmnigentError("fence failed", code=ErrorCode.RUNNER_UNAVAILABLE)

    monkeypatch.setattr(profile_routes, "_apply_and_fence", fail_apply)
    response = await project_client.delete(
        f"/v1/profiles/{profile['id']}",
        headers={"X-Omnigent-Profile-Unlock": token},
    )

    assert response.status_code == 503
    restored = project_app.state.test_profile_store.get(profile["id"], user_id=None)
    assert restored is not None
    assert restored.protection["lock"] == "passcode"


async def test_unknown_profile_filter_is_rejected(project_client: httpx.AsyncClient) -> None:
    """Profile-scoped endpoints fail closed for an unknown profile id."""
    unknown = "0" * 32
    assert (await project_client.get(f"/v1/projects?profile_id={unknown}")).status_code == 404
    assert (await project_client.get(f"/v1/sessions?profile_id={unknown}")).status_code == 404
    assert (
        await project_client.get(f"/v1/sessions/projects?profile_id={unknown}")
    ).status_code == 404


async def test_get_project(project_client: httpx.AsyncClient) -> None:
    """A created project can be fetched by id; unknown ids 404."""
    created = (await project_client.post("/v1/projects", json={"name": "X"})).json()
    resp = await project_client.get(f"/v1/projects/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "X"

    missing = await project_client.get(f"/v1/projects/{'0' * 32}")
    assert missing.status_code == 404


async def test_rename_project(project_client: httpx.AsyncClient) -> None:
    """PATCH renames the project and stamps ``updated_at``."""
    created = (await project_client.post("/v1/projects", json={"name": "Old"})).json()
    resp = await project_client.patch(f"/v1/projects/{created['id']}", json={"name": "New"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "New"
    assert body["updated_at"] is not None


async def test_rename_missing_project_404(project_client: httpx.AsyncClient) -> None:
    """Renaming an unknown project returns 404."""
    resp = await project_client.patch(f"/v1/projects/{'0' * 32}", json={"name": "X"})
    assert resp.status_code == 404


async def test_create_defaults_to_empty_config(project_client: httpx.AsyncClient) -> None:
    """A project created without a config field reports an empty config."""
    body = (await project_client.post("/v1/projects", json={"name": "P"})).json()
    assert body["config"] == {}


async def test_create_and_get_roundtrips_config(project_client: httpx.AsyncClient) -> None:
    """A config passed on create round-trips through the create + get responses."""
    cfg = {"host_id": "host_abc", "workspace": "/work/repo", "model": "claude-opus-4-8"}
    created = (
        await project_client.post("/v1/projects", json={"name": "Configured", "config": cfg})
    ).json()
    assert created["config"] == cfg
    fetched = (await project_client.get(f"/v1/projects/{created['id']}")).json()
    assert fetched["config"] == cfg


async def test_patch_replaces_config(project_client: httpx.AsyncClient) -> None:
    """PATCH with a new config replaces the stored one and stamps updated_at."""
    created = (
        await project_client.post("/v1/projects", json={"name": "P", "config": {"host_id": "old"}})
    ).json()
    resp = await project_client.patch(
        f"/v1/projects/{created['id']}", json={"config": {"host_id": "new", "model": "m"}}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["config"] == {"host_id": "new", "model": "m"}
    assert body["updated_at"] is not None


async def test_patch_without_config_preserves_it(project_client: httpx.AsyncClient) -> None:
    """A PATCH that omits config (e.g. a rename) leaves the stored config intact."""
    created = (
        await project_client.post(
            "/v1/projects", json={"name": "P", "config": {"host_id": "keep"}}
        )
    ).json()
    renamed = await project_client.patch(f"/v1/projects/{created['id']}", json={"name": "P2"})
    assert renamed.json()["config"] == {"host_id": "keep"}


async def test_patch_empty_config_clears_it(project_client: httpx.AsyncClient) -> None:
    """PATCH with config={} clears the stored defaults (distinct from omitting)."""
    created = (
        await project_client.post(
            "/v1/projects", json={"name": "P", "config": {"host_id": "drop"}}
        )
    ).json()
    resp = await project_client.patch(f"/v1/projects/{created['id']}", json={"config": {}})
    assert resp.json()["config"] == {}


async def test_delete_project(project_client: httpx.AsyncClient) -> None:
    """DELETE removes the project; a second delete 404s."""
    created = (await project_client.post("/v1/projects", json={"name": "Doomed"})).json()
    resp = await project_client.delete(f"/v1/projects/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    second_delete = await project_client.delete(f"/v1/projects/{created['id']}")
    assert second_delete.status_code == 404


async def test_session_projects_unions_first_class_and_labels(
    project_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """GET /v1/sessions/projects dual-reads: first-class projects (with id,
    even when empty) unioned with label-only projects (id=None), by name."""
    conv_store = SqlAlchemyConversationStore(db_uri)

    # An empty first-class project — invisible to the label path, must appear.
    empty = (await project_client.post("/v1/projects", json={"name": "Empty FC"})).json()
    # A name that exists BOTH as first-class and as a label — one entry, fc id.
    both = (await project_client.post("/v1/projects", json={"name": "Both"})).json()
    conv_both = conv_store.create_conversation()
    conv_store.set_labels(conv_both.id, {"omni_project": "Both"})
    # A label-only project — no first-class row, id=None.
    conv_label = conv_store.create_conversation()
    conv_store.set_labels(conv_label.id, {"omni_project": "Label Only"})

    resp = await project_client.get("/v1/sessions/projects")
    assert resp.status_code == 200
    assert resp.json() == [
        {"id": both["id"], "name": "Both", "icon": None},
        {"id": empty["id"], "name": "Empty FC", "icon": None},
        {"id": None, "name": "Label Only", "icon": None},
    ]


async def test_session_projects_surfaces_config_icon(
    project_client: httpx.AsyncClient,
) -> None:
    """A first-class project's ``config.icon`` surfaces in the sidebar listing
    so the folder can render the emoji; a non-string/absent icon stays None."""
    fire = (
        await project_client.post("/v1/projects", json={"name": "Fire", "config": {"icon": "🔥"}})
    ).json()
    # A config without an icon key surfaces None (default folder glyph).
    plain = (
        await project_client.post(
            "/v1/projects", json={"name": "Plain", "config": {"host_id": "h"}}
        )
    ).json()

    resp = await project_client.get("/v1/sessions/projects")
    assert resp.status_code == 200
    assert resp.json() == [
        {"id": fire["id"], "name": "Fire", "icon": "🔥"},
        {"id": plain["id"], "name": "Plain", "icon": None},
    ]


# ── Multi-user: projects are owner-private ─────────────────────────────


@pytest.fixture()
def multi_user_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    """A project-enabled app with header auth, so each request has an owner."""
    monkeypatch.setenv(
        "OMNIGENT_PROFILE_PROTECTION_PATH", str(tmp_path / "profile-protection.json")
    )
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        project_store=SqlAlchemyProjectStore(db_uri),
        profile_store=SqlAlchemyProfileStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
    )


@pytest_asyncio.fixture()
async def multi_user_client(
    multi_user_app: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the multi-user (header-auth) app."""
    transport = httpx.ASGITransport(app=multi_user_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_list_scoped_to_requesting_owner(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """Each user sees only their own projects."""
    await multi_user_client.post("/v1/projects", json={"name": "Alice A"}, headers=_as_user(ALICE))
    await multi_user_client.post("/v1/projects", json={"name": "Bob B"}, headers=_as_user(BOB))

    alice = (await multi_user_client.get("/v1/projects", headers=_as_user(ALICE))).json()
    bob = (await multi_user_client.get("/v1/projects", headers=_as_user(BOB))).json()
    assert {p["name"] for p in alice["data"]} == {"Alice A"}
    assert {p["name"] for p in bob["data"]} == {"Bob B"}


async def test_shared_private_session_is_reachable_without_owner_unlock(
    multi_user_client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
) -> None:
    """A grantee can read a shared session while its owner still sees a lock."""
    root = tmp_path / "bob-private"
    root.mkdir()
    profile = (
        await multi_user_client.post(
            "/v1/profiles",
            json={"name": "Bob private"},
            headers=_as_user(BOB),
        )
    ).json()
    protected = await multi_user_client.put(
        f"/v1/profiles/{profile['id']}/protection",
        json={"passcode": "secret", "protected_roots": [str(root)]},
        headers=_as_user(BOB),
    )
    assert protected.status_code == 200
    conversations = SqlAlchemyConversationStore(db_uri)
    session = conversations.create_conversation(
        profile_id=profile["id"],
        workspace=str(root),
    )
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.grant(BOB, session.id, 4)
    permissions.grant(ALICE, session.id, 1)

    shared = await multi_user_client.get(
        f"/v1/sessions/{session.id}/items",
        headers=_as_user(ALICE),
    )
    owner = await multi_user_client.get(
        f"/v1/sessions/{session.id}/items",
        headers=_as_user(BOB),
    )

    assert shared.status_code == 200
    assert owner.status_code == 404


async def test_same_name_allowed_across_users(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """Two users may each own a project with the same name (per-owner uniqueness)."""
    a = await multi_user_client.post(
        "/v1/projects", json={"name": "Shared"}, headers=_as_user(ALICE)
    )
    b = await multi_user_client.post(
        "/v1/projects", json={"name": "Shared"}, headers=_as_user(BOB)
    )
    assert a.status_code == 200
    assert b.status_code == 200


async def test_cannot_get_another_users_project(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """Bob's project is 404 (not found), never readable, for Alice."""
    created = (
        await multi_user_client.post(
            "/v1/projects", json={"name": "Bob only"}, headers=_as_user(BOB)
        )
    ).json()
    resp = await multi_user_client.get(f"/v1/projects/{created['id']}", headers=_as_user(ALICE))
    assert resp.status_code == 404
    # The owner still sees it.
    assert (
        await multi_user_client.get(f"/v1/projects/{created['id']}", headers=_as_user(BOB))
    ).status_code == 200


async def test_cannot_rename_another_users_project(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """Alice cannot rename Bob's project (404), and it stays unchanged."""
    created = (
        await multi_user_client.post(
            "/v1/projects", json={"name": "Bob only"}, headers=_as_user(BOB)
        )
    ).json()
    resp = await multi_user_client.patch(
        f"/v1/projects/{created['id']}", json={"name": "Hacked"}, headers=_as_user(ALICE)
    )
    assert resp.status_code == 404
    still = (
        await multi_user_client.get(f"/v1/projects/{created['id']}", headers=_as_user(BOB))
    ).json()
    assert still["name"] == "Bob only"


async def test_cannot_delete_another_users_project(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """Alice cannot delete Bob's project (404), and it survives."""
    created = (
        await multi_user_client.post(
            "/v1/projects", json={"name": "Bob only"}, headers=_as_user(BOB)
        )
    ).json()
    resp = await multi_user_client.delete(f"/v1/projects/{created['id']}", headers=_as_user(ALICE))
    assert resp.status_code == 404
    assert (
        await multi_user_client.get(f"/v1/projects/{created['id']}", headers=_as_user(BOB))
    ).status_code == 200
