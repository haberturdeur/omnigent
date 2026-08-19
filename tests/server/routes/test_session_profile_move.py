"""Tests for moving standalone sessions between profiles."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.entities import Conversation, Profile
from omnigent.errors import OmnigentError
from omnigent.server.routes.sessions import create_sessions_router, routes_core


async def test_move_standalone_session_between_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATCH profile_id persists the destination on an unfiled session."""
    source_id = "1" * 32
    destination_id = "2" * 32
    conversation = Conversation(
        id="4" * 32,
        created_at=1,
        updated_at=1,
        root_conversation_id="4" * 32,
        agent_id="3" * 32,
        profile_id=source_id,
        workspace="/work/standalone",
    )
    destination = Profile(
        id=destination_id,
        name="Work",
        user_id=None,
        created_at=1,
    )

    conversations = Mock()
    conversations.storage_location = "memory://profiles"
    conversations.get_conversation.return_value = conversation
    conversations.get_conversations.return_value = {conversation.id: conversation}
    conversations.list_child_conversation_ids_by_parent.return_value = {conversation.id: []}
    conversations.update_conversation.return_value = conversation

    def move_profile(
        conversation_ids: tuple[str, ...],
        *,
        expected_profile_id: str | None,
        destination_profile_id: str,
    ) -> bool:
        assert conversation_ids == (conversation.id,)
        assert expected_profile_id == source_id
        conversation.profile_id = destination_profile_id
        return True

    conversations.move_conversations_to_profile.side_effect = move_profile
    profiles = Mock()
    profiles.storage_location = conversations.storage_location
    profiles.get.return_value = destination
    agents = Mock()

    async def snapshot(*args: object, **kwargs: object) -> dict[str, str | None]:
        del args, kwargs
        return {"profile_id": conversation.profile_id}

    async def immediate_to_thread(function: object, *args: object, **kwargs: object) -> object:
        return function(*args, **kwargs)  # type: ignore[operator]

    async def allow(*args: object, **kwargs: object) -> None:
        del args, kwargs

    async def no_permission(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return

    monkeypatch.setattr(routes_core, "_get_session_snapshot", snapshot)
    monkeypatch.setattr(routes_core, "asyncio", SimpleNamespace(to_thread=immediate_to_thread))
    monkeypatch.setattr(routes_core, "_require_access", allow)
    monkeypatch.setattr(routes_core, "_get_permission_level", no_permission)
    monkeypatch.setattr(routes_core, "profile_is_accessible", lambda *_args: True)
    monkeypatch.setattr(
        routes_core,
        "protected_profile_for_workspace",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(routes_core, "get_profile_protection_by_id", lambda *_args: None)

    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_sessions_router(
            conversation_store=conversations,
            agent_store=agents,
            profile_store=profiles,
        ),
        prefix="/v1",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            f"/v1/sessions/{conversation.id}",
            json={"profile_id": destination_id},
        )

    assert response.status_code == 200
    assert response.json()["profile_id"] == destination_id
    assert conversation.profile_id == destination_id
