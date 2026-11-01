"""Resolve profile membership at the session creation boundary."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes.profiles import resolve_profile


async def resolve_session_profile(
    request: Request, body: Any, conversations: Any, projects: Any
) -> str | None:
    user_id = require_user(request, getattr(request.app.state, "auth_provider", None))
    selected = getattr(body, "profile_id", None) or request.headers.get("X-Omnigent-Profile")
    if selected == "default":
        selected = None
    inherited = None
    if body.parent_session_id:
        parent = await asyncio.to_thread(conversations.get_conversation, body.parent_session_id)
        inherited = parent.profile_id if parent is not None else None
    if getattr(body, "project_id", None) and projects is not None:
        project = await asyncio.to_thread(projects.get, body.project_id, user_id=user_id)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        inherited = project.profile_id
    if inherited is not None:
        if selected is not None and selected != inherited:
            raise OmnigentError(
                "Project or parent session belongs to another profile",
                code=ErrorCode.INVALID_INPUT,
            )
        return inherited
    return await resolve_profile(request, user_id, selected)
