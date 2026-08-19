"""Owner-scoped profile CRUD routes."""

from __future__ import annotations

import asyncio
import os
import time
import urllib.parse
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Request

from omnigent.entities import Profile
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.profile_protection import (
    PROFILE_UNLOCK_HEADER,
    RUNNER_GENERATION_LEASE_TTL_SECONDS,
    ProfileProtectionChange,
    apply_profile_protection_change,
    begin_profile_protection_transition,
    finish_profile_protection_transition,
    get_profile_protection,
    isolation_masks_for_workspace,
    mint_profile_unlock,
    plan_profile_protection,
    plan_profile_protection_removal,
    profile_is_accessible,
    profile_protection_generation,
    revert_profile_protection_change,
    revoke_profile_unlock,
    stale_runner_generation_leases,
    validate_profile_unlock,
    workspace_belongs_to_profile,
)
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.schemas import (
    AddProjectPrivateRootRequest,
    ConfigureProfileProtectionRequest,
    CreateProfileRequest,
    UnlockProfileRequest,
    UpdateProfileRequest,
)
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.profile_store import ProfileStore
from omnigent.stores.project_store import (
    LegacyProjectMembershipChangedError,
    ProjectDestinationProfileChangedError,
    ProjectMembershipChangedError,
    ProjectProfileChangedError,
    ProjectSessionProfileMismatchError,
    ProjectStore,
)

DESTINATION_PROFILE_UNLOCK_HEADER = "X-Omnigent-Destination-Profile-Unlock"

_protection_change_lock = asyncio.Lock()
_GENERATION_FENCE_POLL_SECONDS = 0.05


async def _invalidate_non_owning_environments(
    change: ProfileProtectionChange,
    conversation_store: ConversationStore | None,
    runner_router: Any,
) -> None:
    """Destroy live runner state that predates changed private roots."""
    if not change.changed_roots or conversation_store is None or runner_router is None:
        return

    after: str | None = None
    affected: list[Any] = []
    while True:
        page = await asyncio.to_thread(
            conversation_store.list_conversations,
            limit=100,
            after=after,
            kind=None,
            order="asc",
            include_archived=True,
        )
        for conversation in page.data:
            if not conversation.runner_id or not runner_router.runner_is_online(
                conversation.runner_id
            ):
                continue
            if conversation.workspace is None:
                affected.append(conversation)
                continue
            old_masks = isolation_masks_for_workspace(
                conversation.workspace,
                host_id=conversation.host_id,
                profiles=change.before,
            )
            new_masks = isolation_masks_for_workspace(
                conversation.workspace,
                host_id=conversation.host_id,
                profiles=change.after,
            )
            if frozenset(old_masks) != frozenset(new_masks):
                affected.append(conversation)
        if not page.has_more or page.last_id is None:
            break
        after = page.last_id

    for conversation in affected:
        try:
            routed = runner_router.client_for_existing_conversation(conversation.id)
            if routed is None:
                raise OmnigentError(
                    f"Could not invalidate running session {conversation.id!r}.",
                    code=ErrorCode.RUNNER_UNAVAILABLE,
                )
            stop_response = await routed.client.post(
                f"/v1/sessions/{urllib.parse.quote(conversation.id, safe='')}/events",
                json={"type": "stop_session"},
                timeout=15.0,
            )
            stop_response.raise_for_status()
            response = await routed.client.post(
                f"/v1/sessions/{urllib.parse.quote(conversation.id, safe='')}/reset-state",
                timeout=15.0,
            )
            response.raise_for_status()
        except (httpx.HTTPError, ConnectionError, RuntimeError) as exc:
            raise OmnigentError(
                "Private-profile roots were not changed because a running "
                f"session could not be safely restarted: {conversation.id}",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            ) from exc


async def _await_runner_generation_fence(change: ProfileProtectionChange) -> None:
    """Wait until every cross-worker runner using an old mask snapshot exits."""
    if not change.changed_roots:
        return
    generation = await asyncio.to_thread(profile_protection_generation)
    deadline = time.monotonic() + RUNNER_GENERATION_LEASE_TTL_SECONDS + 2.0
    while True:
        stale = await asyncio.to_thread(stale_runner_generation_leases, generation)
        if not stale:
            return
        if time.monotonic() >= deadline:
            runner_ids = ", ".join(sorted({lease.runner_id for lease in stale}))
            raise OmnigentError(
                f"Private-profile protection is waiting for stale runners to exit: {runner_ids}",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )
        await asyncio.sleep(_GENERATION_FENCE_POLL_SECONDS)


async def _revert_and_fence(change: ProfileProtectionChange) -> None:
    """Compensate a failed DB write and drain runners from the reverted generation."""
    await asyncio.to_thread(revert_profile_protection_change, change)
    await _await_runner_generation_fence(change)


async def _apply_and_fence(
    change: ProfileProtectionChange,
    *,
    required_unlock_token: str | None,
) -> Any:
    """Apply a registry mutation and compensate if its fence fails."""
    applied = await asyncio.to_thread(
        apply_profile_protection_change,
        change,
        required_unlock_token=required_unlock_token,
    )
    try:
        await _await_runner_generation_fence(change)
    except BaseException:
        await _revert_and_fence(change)
        raise
    return applied


def _workspace_is_within_roots(
    workspace: str,
    roots: tuple[Path, ...],
    *,
    host_id: str | None,
) -> bool:
    resolved = (
        Path(workspace).expanduser().resolve(strict=False)
        if host_id is None
        else Path(os.path.normpath(workspace))
    )
    return any(resolved == root or resolved.is_relative_to(root) for root in roots)


async def _validate_existing_profile_workspaces(
    profile_id: str,
    *,
    user_id: str | None,
    change: ProfileProtectionChange,
    project_store: ProjectStore | None,
    conversation_store: ConversationStore | None,
) -> None:
    """Reject protection roots that omit existing owned profile content."""
    configured = change.configured
    assert configured is not None
    roots = configured.protected_roots
    if project_store is not None:
        projects = await asyncio.to_thread(
            project_store.list,
            user_id=user_id,
            profile_id=profile_id,
        )
        for project in projects:
            workspace = project.config.get("workspace")
            project_host_id = project.config.get("host_id", configured.host_id)
            if project_host_id != configured.host_id or (
                isinstance(workspace, str)
                and workspace
                and not _workspace_is_within_roots(
                    workspace,
                    roots,
                    host_id=configured.host_id,
                )
            ):
                raise OmnigentError(
                    "An existing project workspace is outside the protected roots.",
                    code=ErrorCode.INVALID_INPUT,
                )
    if conversation_store is None:
        return
    after: str | None = None
    while True:
        page = await asyncio.to_thread(
            conversation_store.list_conversations,
            limit=100,
            after=after,
            kind=None,
            order="asc",
            include_archived=True,
            profile_id=profile_id,
            owned_by=user_id if user_id is not None else None,
        )
        for conversation in page.data:
            if conversation.host_id != configured.host_id or (
                conversation.workspace
                and not _workspace_is_within_roots(
                    conversation.workspace,
                    roots,
                    host_id=configured.host_id,
                )
            ):
                raise OmnigentError(
                    "An existing session workspace is outside the protected roots.",
                    code=ErrorCode.INVALID_INPUT,
                )
        if not page.has_more or page.last_id is None:
            return
        after = page.last_id


def _to_response(profile: Profile, *, unlock_token: str | None = None) -> dict[str, Any]:
    protection = dict(profile.protection)
    registry_protected = get_profile_protection(profile.id, user_id=profile.user_id) is not None
    declared_protected = protection.get("lock") in {"passcode", "device"}
    protected = registry_protected or declared_protected
    unlocked = registry_protected and validate_profile_unlock(
        unlock_token, profile_id=profile.id, user_id=profile.user_id
    )
    if protected:
        protection.update({"lock": "passcode", "notification_content": "generic"})
    else:
        protection.pop("lock", None)
    return {
        "id": profile.id,
        "object": "profile",
        "name": profile.name,
        "icon": profile.icon,
        "color": profile.color,
        "is_default": profile.is_default,
        "config": profile.config if not protected or unlocked else {},
        "protection": protection,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at if not protected or unlocked else None,
    }


def _profile_host_id(config: dict[str, Any]) -> str | None:
    """Return a validated profile host binding from opaque profile config."""
    host_id = config.get("host_id")
    if host_id is None:
        return None
    if not isinstance(host_id, str) or not host_id or host_id != host_id.strip():
        raise OmnigentError(
            "Profile host_id must be a non-empty string.",
            code=ErrorCode.INVALID_INPUT,
        )
    return host_id


def create_profiles_router(
    profile_store: ProfileStore,
    auth_provider: AuthProvider | None = None,
    conversation_store: ConversationStore | None = None,
    runner_router: Any = None,
    project_store: ProjectStore | None = None,
    host_store: HostStore | None = None,
    quiesce_protection_change: (
        Callable[[ProfileProtectionChange], Awaitable[None]] | None
    ) = None,
) -> APIRouter:
    router = APIRouter()
    if quiesce_protection_change is None:

        async def quiesce_protection_change(change: ProfileProtectionChange) -> None:
            await _invalidate_non_owning_environments(change, conversation_store, runner_router)

    @router.get("/profiles")
    async def list_profiles(request: Request) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        profiles = await asyncio.to_thread(profile_store.list, user_id=user_id)
        token = request.headers.get(PROFILE_UNLOCK_HEADER)
        return {
            "object": "list",
            "data": [_to_response(profile, unlock_token=token) for profile in profiles],
        }

    @router.post("/profiles")
    async def create_profile(request: Request, body: CreateProfileRequest) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        if body.protection.get("lock") is not None:
            raise OmnigentError(
                "Configure profile locking through the protection endpoint.",
                code=ErrorCode.INVALID_INPUT,
            )
        await asyncio.to_thread(profile_store.ensure_default, user_id=user_id)
        profile = await asyncio.to_thread(
            profile_store.create,
            uuid.uuid4().hex,
            body.name,
            user_id,
            icon=body.icon,
            color=body.color,
            config=body.config,
            protection=body.protection,
        )
        return _to_response(profile, unlock_token=request.headers.get(PROFILE_UNLOCK_HEADER))

    @router.get("/profiles/{profile_id}")
    async def get_profile(request: Request, profile_id: str) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        profile = await asyncio.to_thread(profile_store.get, profile_id, user_id=user_id)
        if profile is None:
            raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
        return _to_response(profile, unlock_token=request.headers.get(PROFILE_UNLOCK_HEADER))

    @router.patch("/profiles/{profile_id}")
    async def update_profile(
        request: Request,
        profile_id: str,
        body: UpdateProfileRequest,
    ) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        async with _protection_change_lock:
            current_profile = await asyncio.to_thread(
                profile_store.get, profile_id, user_id=user_id
            )
            if current_profile is None:
                raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
            protected = await asyncio.to_thread(
                get_profile_protection, profile_id, user_id=user_id
            )
            if not profile_is_accessible(
                profile_id,
                request.headers.get(PROFILE_UNLOCK_HEADER),
            ):
                raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
            if protected is None and body.protection and body.protection.get("lock") is not None:
                raise OmnigentError(
                    "Configure profile locking through the protection endpoint.",
                    code=ErrorCode.INVALID_INPUT,
                )
            if (
                protected is not None
                and body.config is not None
                and _profile_host_id(body.config) != protected.host_id
            ):
                raise OmnigentError(
                    "Disable private-profile protection before changing its host.",
                    code=ErrorCode.CONFLICT,
                )
            profile = await asyncio.to_thread(
                profile_store.update,
                profile_id,
                user_id=user_id,
                name=body.name,
                icon=body.icon,
                color=body.color,
                config=body.config,
                protection=(
                    {"lock": "passcode", "notification_content": "generic"}
                    if protected is not None
                    else body.protection
                ),
            )
            if profile is None:
                raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
            return _to_response(
                profile,
                unlock_token=request.headers.get(PROFILE_UNLOCK_HEADER),
            )

    @router.delete("/profiles/{profile_id}")
    async def delete_profile(request: Request, profile_id: str) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        profile = await asyncio.to_thread(profile_store.get, profile_id, user_id=user_id)
        if profile is None:
            raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
        protected = await asyncio.to_thread(get_profile_protection, profile_id, user_id=user_id)
        if not profile_is_accessible(
            profile_id,
            request.headers.get(PROFILE_UNLOCK_HEADER),
        ):
            raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
        async with _protection_change_lock:
            profile = await asyncio.to_thread(profile_store.get, profile_id, user_id=user_id)
            if profile is None:
                raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
            protected = await asyncio.to_thread(
                get_profile_protection, profile_id, user_id=user_id
            )
            token = request.headers.get(PROFILE_UNLOCK_HEADER)
            if not profile_is_accessible(profile_id, token):
                raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
            removal = (
                await asyncio.to_thread(
                    plan_profile_protection_removal, profile_id, user_id=user_id
                )
                if protected is not None
                else None
            )
            if removal is not None:
                transition_started = False
                try:
                    await asyncio.to_thread(begin_profile_protection_transition, profile_id)
                    transition_started = True
                    await quiesce_protection_change(removal)
                    deleted = await asyncio.to_thread(
                        profile_store.delete, profile_id, user_id=user_id
                    )
                    if not deleted:
                        raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
                    try:
                        await _apply_and_fence(
                            removal,
                            required_unlock_token=token,
                        )
                    except BaseException:
                        await asyncio.to_thread(profile_store.restore, profile)
                        raise
                finally:
                    if transition_started:
                        await asyncio.to_thread(finish_profile_protection_transition, profile_id)
            else:
                deleted = await asyncio.to_thread(
                    profile_store.delete, profile_id, user_id=user_id
                )
                if not deleted:
                    raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
        return {"id": profile_id, "object": "profile.deleted", "deleted": True}

    @router.get("/profiles/{profile_id}/protection")
    async def get_protection_status(request: Request, profile_id: str) -> dict[str, Any]:
        """Return lock state without exposing the persisted passcode hash."""
        user_id = require_user(request, auth_provider)
        profile = await asyncio.to_thread(profile_store.get, profile_id, user_id=user_id)
        if profile is None:
            raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
        protected = await asyncio.to_thread(get_profile_protection, profile_id, user_id=user_id)
        token = request.headers.get(PROFILE_UNLOCK_HEADER)
        return {
            "profile_id": profile_id,
            "configured": protected is not None,
            "unlocked": validate_profile_unlock(token, profile_id=profile_id, user_id=user_id),
            "protected_roots": (
                [str(root) for root in protected.protected_roots]
                if protected
                and validate_profile_unlock(token, profile_id=profile_id, user_id=user_id)
                else []
            ),
        }

    @router.put("/profiles/{profile_id}/protection")
    async def configure_protection(
        request: Request,
        profile_id: str,
        body: ConfigureProfileProtectionRequest,
    ) -> dict[str, Any]:
        """Enable or update one profile's lock and bwrap isolation roots."""
        user_id = require_user(request, auth_provider)
        profile = await asyncio.to_thread(profile_store.get, profile_id, user_id=user_id)
        if profile is None:
            raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
        current = await asyncio.to_thread(get_profile_protection, profile_id, user_id=user_id)
        if not profile_is_accessible(
            profile_id,
            request.headers.get(PROFILE_UNLOCK_HEADER),
        ):
            raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
        if current is not None:
            token = request.headers.get(PROFILE_UNLOCK_HEADER)
            if not validate_profile_unlock(token, profile_id=profile_id, user_id=user_id):
                raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
        async with _protection_change_lock:
            profile = await asyncio.to_thread(profile_store.get, profile_id, user_id=user_id)
            if profile is None:
                raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
            host_id = _profile_host_id(profile.config)
            if host_id is not None and host_store is not None:
                host = await asyncio.to_thread(host_store.get_host, host_id)
                if host is None or (user_id is not None and host.user_id != user_id):
                    raise OmnigentError(
                        "Configured profile host not found.",
                        code=ErrorCode.NOT_FOUND,
                    )
            current = await asyncio.to_thread(get_profile_protection, profile_id, user_id=user_id)
            token = request.headers.get(PROFILE_UNLOCK_HEADER)
            if current is not None and not validate_profile_unlock(
                token, profile_id=profile_id, user_id=user_id
            ):
                raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
            change = await asyncio.to_thread(
                plan_profile_protection,
                profile_id,
                user_id=user_id,
                host_id=host_id,
                passcode=body.passcode,
                protected_roots=body.protected_roots,
            )
            transition_started = False
            try:
                await asyncio.to_thread(begin_profile_protection_transition, profile_id)
                transition_started = True
                await _validate_existing_profile_workspaces(
                    profile_id,
                    user_id=user_id,
                    change=change,
                    project_store=project_store,
                    conversation_store=conversation_store,
                )
                await quiesce_protection_change(change)
                protected = await _apply_and_fence(
                    change,
                    required_unlock_token=(token if current is not None else None),
                )
                try:
                    updated = await asyncio.to_thread(
                        profile_store.update,
                        profile_id,
                        user_id=user_id,
                        protection={"lock": "passcode", "notification_content": "generic"},
                    )
                except Exception:
                    await _revert_and_fence(change)
                    raise
                if updated is None:
                    await _revert_and_fence(change)
                    raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
            finally:
                if transition_started:
                    await asyncio.to_thread(finish_profile_protection_transition, profile_id)
        assert protected is not None
        return {
            "profile_id": profile_id,
            "configured": True,
            "unlocked": False,
            "protected_roots": [str(root) for root in protected.protected_roots],
        }

    @router.post("/profiles/{profile_id}/protected-roots/projects")
    async def add_project_private_root(
        request: Request,
        profile_id: str,
        body: AddProjectPrivateRootRequest,
    ) -> dict[str, Any]:
        """Protect a project's current folder and move its metadata here."""
        user_id = require_user(request, auth_provider)
        if project_store is None or conversation_store is None:
            raise OmnigentError("Project moves are unavailable.", code=ErrorCode.CONFLICT)
        if project_store.storage_location != profile_store.storage_location:
            raise OmnigentError(
                "Moving projects requires profiles and projects to share storage.",
                code=ErrorCode.CONFLICT,
            )
        profile = await asyncio.to_thread(profile_store.get, profile_id, user_id=user_id)
        current_protection = await asyncio.to_thread(
            get_profile_protection, profile_id, user_id=user_id
        )
        destination_token = request.headers.get(DESTINATION_PROFILE_UNLOCK_HEADER)
        if (
            profile is None
            or current_protection is None
            or not validate_profile_unlock(
                destination_token,
                profile_id=profile_id,
                user_id=user_id,
            )
        ):
            raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
        assert current_protection is not None

        project = await asyncio.to_thread(project_store.get, body.project_id, user_id=user_id)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        if project.profile_id is not None:
            source_protection = await asyncio.to_thread(
                get_profile_protection, project.profile_id, user_id=user_id
            )
            if source_protection is not None and not validate_profile_unlock(
                request.headers.get(PROFILE_UNLOCK_HEADER),
                profile_id=project.profile_id,
                user_id=user_id,
            ):
                raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        workspace = project.config.get("workspace")
        if not isinstance(workspace, str) or not workspace.startswith("/"):
            raise OmnigentError(
                "Set an absolute project workspace before protecting its folder.",
                code=ErrorCode.INVALID_INPUT,
            )

        labels_are_colocated = project_store.storage_location == (
            conversation_store.conversation_storage_location or conversation_store.storage_location
        )
        protection_host_id = current_protection.host_id

        def validate_protected_workspace(value: str, host_id: str | None) -> None:
            if host_id != protection_host_id:
                raise OmnigentError(
                    "Every project session must use the private profile's host.",
                    code=ErrorCode.INVALID_INPUT,
                )
            if not workspace_belongs_to_profile(
                profile_id,
                value,
                host_id=host_id,
            ):
                raise OmnigentError(
                    "A project session is outside the protected project root.",
                    code=ErrorCode.INVALID_INPUT,
                )

        async with _protection_change_lock:
            current_protection = await asyncio.to_thread(
                get_profile_protection, profile_id, user_id=user_id
            )
            if current_protection is None or not validate_profile_unlock(
                destination_token,
                profile_id=profile_id,
                user_id=user_id,
            ):
                raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
            roots = [str(root) for root in current_protection.protected_roots]
            if not workspace_belongs_to_profile(
                profile_id,
                workspace,
                host_id=current_protection.host_id,
            ):
                roots.append(workspace)
            change = await asyncio.to_thread(
                plan_profile_protection,
                profile_id,
                user_id=user_id,
                host_id=current_protection.host_id,
                passcode=None,
                protected_roots=roots,
            )
            await quiesce_protection_change(change)
            protection_applied = False
            try:
                await _apply_and_fence(
                    change,
                    required_unlock_token=destination_token,
                )
                protection_applied = True
                moved = await asyncio.to_thread(
                    project_store.update_with_sessions,
                    project.id,
                    user_id=user_id,
                    expected_profile_id=project.profile_id,
                    destination_profile_id=profile_id,
                    resolve_legacy_session_ids=conversation_store.get_legacy_project_session_ids,
                    hold_legacy_membership_lock=(
                        None
                        if labels_are_colocated
                        else conversation_store.hold_legacy_project_membership_lock
                    ),
                    legacy_project_name=project.name,
                    legacy_labels_are_colocated=labels_are_colocated,
                    clear_legacy_labels=(
                        None
                        if labels_are_colocated
                        else conversation_store.delete_legacy_project_labels
                    ),
                    validate_workspace=validate_protected_workspace,
                )
                if moved is None:
                    raise ProjectProfileChangedError(project.id)
            except Exception as exc:
                if protection_applied:
                    await _revert_and_fence(change)
                if isinstance(
                    exc,
                    (
                        LegacyProjectMembershipChangedError,
                        ProjectDestinationProfileChangedError,
                        ProjectMembershipChangedError,
                        ProjectProfileChangedError,
                        ProjectSessionProfileMismatchError,
                    ),
                ):
                    raise OmnigentError(
                        "Project membership changed while it was being moved; retry the request.",
                        code=ErrorCode.CONFLICT,
                    ) from exc
                raise

        return {
            "id": moved.id,
            "object": "project",
            "name": moved.name,
            "profile_id": moved.profile_id,
            "created_at": moved.created_at,
            "updated_at": moved.updated_at,
            "config": moved.config,
        }

    @router.delete("/profiles/{profile_id}/protection")
    async def disable_protection(request: Request, profile_id: str) -> dict[str, Any]:
        """Remove a profile from the private domain after explicit unlock."""
        user_id = require_user(request, auth_provider)
        profile = await asyncio.to_thread(profile_store.get, profile_id, user_id=user_id)
        if profile is None:
            raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
        token = request.headers.get(PROFILE_UNLOCK_HEADER)
        if not validate_profile_unlock(token, profile_id=profile_id, user_id=user_id):
            raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
        async with _protection_change_lock:
            token = request.headers.get(PROFILE_UNLOCK_HEADER)
            if not validate_profile_unlock(token, profile_id=profile_id, user_id=user_id):
                raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
            removal = await asyncio.to_thread(
                plan_profile_protection_removal, profile_id, user_id=user_id
            )
            if removal is None:
                raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
            transition_started = False
            try:
                await asyncio.to_thread(begin_profile_protection_transition, profile_id)
                transition_started = True
                await quiesce_protection_change(removal)
                updated = await asyncio.to_thread(
                    profile_store.update,
                    profile_id,
                    user_id=user_id,
                    protection={},
                )
                if updated is None:
                    raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
                try:
                    await _apply_and_fence(
                        removal,
                        required_unlock_token=token,
                    )
                except BaseException:
                    await asyncio.to_thread(
                        profile_store.update,
                        profile_id,
                        user_id=user_id,
                        protection=profile.protection,
                    )
                    raise
            finally:
                if transition_started:
                    await asyncio.to_thread(finish_profile_protection_transition, profile_id)
        return {"profile_id": profile_id, "configured": False, "unlocked": False}

    @router.post("/profiles/{profile_id}/unlock")
    async def unlock_profile(
        request: Request, profile_id: str, body: UnlockProfileRequest
    ) -> dict[str, str]:
        """Exchange a profile passcode for a process-memory bearer."""
        user_id = require_user(request, auth_provider)
        if await asyncio.to_thread(profile_store.get, profile_id, user_id=user_id) is None:
            raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
        token = await asyncio.to_thread(
            mint_profile_unlock, profile_id, body.passcode, user_id=user_id
        )
        return {"token": token}

    @router.delete("/profiles/{profile_id}/unlock")
    async def lock_profile(request: Request, profile_id: str) -> dict[str, bool]:
        """Forget the current tab's unlock bearer."""
        user_id = require_user(request, auth_provider)
        revoke_profile_unlock(
            request.headers.get(PROFILE_UNLOCK_HEADER),
            profile_id=profile_id,
            user_id=user_id,
        )
        return {"locked": True}

    return router
