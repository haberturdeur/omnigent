"""End-to-end profile, project, and private-access user journey.

This exercises the public HTTP API against the real server process. Profile
selection itself is client-local, so switching is represented by querying the
profile-scoped project/session collections that the clients use after a switch.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx

from tests.e2e.conftest import create_runner_bound_session, register_inline_agent


def test_profile_project_move_keeps_session_history_and_requires_unlock(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
    tmp_path: Path,
) -> None:
    """A project moves intact and becomes hidden while its profile is locked."""
    suffix = uuid.uuid4().hex[:8]
    destination_name = f"Destination {suffix}"
    project_name = f"Move journey {suffix}"
    history_marker = f"history-survives-{suffix}"
    protected_root = tmp_path / "private-profile"

    profiles_response = http_client.get("/v1/profiles")
    profiles_response.raise_for_status()
    profiles = profiles_response.json()["data"]
    assert [(profile["name"], profile["is_default"]) for profile in profiles] == [
        ("Personal", True)
    ]
    source_id = profiles[0]["id"]

    destination_response = http_client.post(
        "/v1/profiles",
        json={
            "name": destination_name,
            "config": {"workspace": str(protected_root)},
        },
    )
    destination_response.raise_for_status()
    destination_id = destination_response.json()["id"]

    listed_profiles = http_client.get("/v1/profiles")
    listed_profiles.raise_for_status()
    assert {profile["name"] for profile in listed_profiles.json()["data"]} == {
        "Personal",
        destination_name,
    }

    project_response = http_client.post(
        "/v1/projects",
        json={"name": project_name, "profile_id": source_id},
    )
    project_response.raise_for_status()
    project_id = project_response.json()["id"]

    agent_name = register_inline_agent(
        http_client,
        name=f"profile-journey-{suffix}",
        harness="openai-agents",
        model=f"mock-profile-journey-{suffix}",
        profile="",
        prompt="You are a terse test assistant.",
        mock_llm_base_url=(f"{mock_llm_server_url}/v1" if mock_llm_server_url else None),
    )
    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )
    file_response = http_client.patch(
        f"/v1/sessions/{session_id}",
        json={"profile_id": source_id, "project_id": project_id},
    )
    file_response.raise_for_status()
    history_response = http_client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": agent_name, "text": history_marker},
        },
    )
    history_response.raise_for_status()

    source_projects = http_client.get("/v1/projects", params={"profile_id": source_id})
    source_projects.raise_for_status()
    assert [project["id"] for project in source_projects.json()["data"]] == [project_id]

    move_response = http_client.patch(
        f"/v1/projects/{project_id}",
        json={"profile_id": destination_id},
    )
    move_response.raise_for_status()
    assert move_response.json()["profile_id"] == destination_id

    source_after_move = http_client.get("/v1/projects", params={"profile_id": source_id})
    source_after_move.raise_for_status()
    assert source_after_move.json()["data"] == []
    destination_after_move = http_client.get("/v1/projects", params={"profile_id": destination_id})
    destination_after_move.raise_for_status()
    assert [project["id"] for project in destination_after_move.json()["data"]] == [project_id]

    moved_session = http_client.get(f"/v1/sessions/{session_id}")
    moved_session.raise_for_status()
    assert moved_session.json()["profile_id"] == destination_id
    assert moved_session.json()["project_id"] == project_id
    moved_items = http_client.get(
        f"/v1/sessions/{session_id}/items", params={"order": "asc", "limit": 100}
    )
    moved_items.raise_for_status()
    assert history_marker in json.dumps(moved_items.json()["data"])

    protection_response = http_client.put(
        f"/v1/profiles/{destination_id}/protection",
        json={
            "passcode": "journey-passcode",
            "protected_roots": [str(protected_root)],
        },
    )
    protection_response.raise_for_status()

    locked_profile = http_client.get(f"/v1/profiles/{destination_id}")
    locked_profile.raise_for_status()
    assert locked_profile.json()["config"] == {}
    assert (
        http_client.get("/v1/projects", params={"profile_id": destination_id}).status_code == 404
    )
    assert http_client.get(f"/v1/sessions/{session_id}").status_code == 404

    unlock_response = http_client.post(
        f"/v1/profiles/{destination_id}/unlock",
        json={"passcode": "journey-passcode"},
    )
    unlock_response.raise_for_status()
    unlock_headers = {"X-Omnigent-Profile-Unlock": unlock_response.json()["token"]}

    visible_projects = http_client.get(
        "/v1/projects",
        params={"profile_id": destination_id},
        headers=unlock_headers,
    )
    visible_projects.raise_for_status()
    assert [project["id"] for project in visible_projects.json()["data"]] == [project_id]
    visible_session = http_client.get(f"/v1/sessions/{session_id}", headers=unlock_headers)
    visible_session.raise_for_status()
    assert visible_session.json()["project_id"] == project_id
    visible_items = http_client.get(
        f"/v1/sessions/{session_id}/items",
        params={"order": "asc", "limit": 100},
        headers=unlock_headers,
    )
    visible_items.raise_for_status()
    assert history_marker in json.dumps(visible_items.json()["data"])
