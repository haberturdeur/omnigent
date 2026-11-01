"""Owner-scoped profile management; profiles organize rather than sandbox sessions."""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request, WebSocket
from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.profile_store import ProfileStore


class CreateProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    icon: str | None = Field(default=None, max_length=64)
    color: str | None = Field(default=None, max_length=32)
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Profile name must not be blank")
        return value


class UpdateProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    icon: str | None = Field(default=None, max_length=64)
    color: str | None = Field(default=None, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    config: dict[str, Any] | None = None

    _trim_name = field_validator("name")(CreateProfileRequest.trim_name)


def _response(profile: Any) -> dict[str, Any]:
    result = asdict(profile)
    result.pop("user_id")
    return {"object": "profile", **result}


async def resolve_profile(
    request: Request | WebSocket, user_id: str | None, profile_id: str | None = None
) -> str | None:
    store = getattr(request.app.state, "profile_store", None)
    if store is None:
        return None
    profile_id = profile_id or request.headers.get("X-Omnigent-Profile")
    if profile_id == "default":
        profile_id = None
    if profile_id:
        if re.fullmatch(r"[0-9a-f]{32}", profile_id) is None:
            raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
        profile = await asyncio.to_thread(store.get, profile_id, user_id=user_id)
        if profile is None:
            raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
    else:
        profile = await asyncio.to_thread(store.ensure_default, user_id=user_id)
    if profile.is_default:
        profile = await asyncio.to_thread(store.ensure_default, user_id=user_id)
    return profile.id


def create_profiles_router(store: ProfileStore, auth_provider: AuthProvider | None) -> APIRouter:
    router = APIRouter()

    @router.get("/profiles")
    async def list_profiles(request: Request) -> dict[str, Any]:
        profiles = await asyncio.to_thread(
            store.list, user_id=require_user(request, auth_provider)
        )
        return {"object": "list", "data": [_response(profile) for profile in profiles]}

    @router.post("/profiles")
    async def create_profile(request: Request, body: CreateProfileRequest) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        await asyncio.to_thread(store.ensure_default, user_id=user_id)
        profile = await asyncio.to_thread(
            store.create,
            uuid.uuid4().hex,
            body.name,
            user_id,
            icon=body.icon,
            color=body.color,
            config=body.config,
        )
        return _response(profile)

    @router.patch("/profiles/{profile_id}")
    async def update_profile(
        request: Request, profile_id: str, body: UpdateProfileRequest
    ) -> dict[str, Any]:
        profile = await asyncio.to_thread(
            store.update,
            profile_id,
            user_id=require_user(request, auth_provider),
            **body.model_dump(exclude_unset=True),
        )
        if profile is None:
            raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
        return _response(profile)

    @router.delete("/profiles/{profile_id}")
    async def delete_profile(request: Request, profile_id: str) -> dict[str, Any]:
        deleted = await asyncio.to_thread(
            store.delete, profile_id, user_id=require_user(request, auth_provider)
        )
        return {"id": profile_id, "deleted": deleted}

    return router
