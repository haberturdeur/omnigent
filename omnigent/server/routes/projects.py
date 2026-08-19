"""REST API routes for projects (``/v1/projects``).

Projects are first-class, owner-private containers that group sessions (see
``designs/PROJECTS_PRD.md``). These endpoints let the web UI create empty
projects, list them, rename them, and delete them. Session membership
(filing a session into a project) is managed on the sessions API via the
conversation store's ``project_id``.

Because projects are owner-private and carry no ACL of their own, every handler
scopes to the requesting user: a caller only ever sees and mutates their own
projects. In single-user mode (no auth provider) the owner is ``None`` and all
projects share that scope.
"""

from __future__ import annotations

import asyncio
import posixpath
import secrets
import urllib.parse
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from omnigent.entities import Project
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import HostMoveDirFrame, encode_host_frame
from omnigent.server.auth import AuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.profile_protection import (
    PROFILE_UNLOCK_HEADER,
    profile_is_accessible,
    profile_membership_write,
    protected_profile_for_workspace,
    read_protected_profiles,
    workspace_belongs_to_profile,
)
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.schemas import (
    CreateProjectRequest,
    UpdateProjectRequest,
)
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.host_store import HostStore, host_is_live
from omnigent.stores.profile_store import ProfileStore
from omnigent.stores.project_store import (
    LegacyProjectMembershipChangedError,
    ProjectDestinationProfileChangedError,
    ProjectMembershipChangedError,
    ProjectProfileChangedError,
    ProjectSessionProfileMismatchError,
    ProjectStore,
    ProjectWorkspaceRelocationError,
)

DESTINATION_PROFILE_UNLOCK_HEADER = "X-Omnigent-Destination-Profile-Unlock"
_MOVE_DIR_TIMEOUT_S = 600.0
_MOVE_DIR_CAPABILITY = "move_dir"


def _coordinated_membership_call(
    profile_ids: tuple[str, ...],
    callback: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    with profile_membership_write(profile_ids):
        return callback(*args, **kwargs)


class MoveProjectFolderRequest(BaseModel):
    """Move a project's folder below a destination profile's workspace."""

    profile_id: str


async def _move_directory_on_host(
    host_registry: HostRegistry,
    host_conn: HostConnection,
    source_path: str,
    destination_path: str,
) -> dict[str, Any]:
    """Proxy one non-overwriting directory move through the host tunnel."""
    request_id = secrets.token_hex(8)
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    host_conn.pending_move_dirs[request_id] = future
    try:
        try:
            host_registry.send_text(
                host_conn,
                encode_host_frame(
                    HostMoveDirFrame(
                        request_id=request_id,
                        source_path=source_path,
                        destination_path=destination_path,
                    )
                ),
            )
        except ConnectionError as exc:
            raise HTTPException(status_code=502, detail="host connection was replaced") from exc
        try:
            return await asyncio.wait_for(future, timeout=_MOVE_DIR_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=504, detail="host directory move timed out") from exc
    finally:
        host_conn.pending_move_dirs.pop(request_id, None)


def _to_response(project: Project) -> dict[str, Any]:
    """Convert a :class:`Project` entity to a ``ProjectObject`` response dict.

    :param project: The entity to convert.
    :returns: Dict matching the :class:`ProjectObject` shape.
    """
    return {
        "id": project.id,
        "object": "project",
        "name": project.name,
        "profile_id": project.profile_id,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "config": project.config,
    }


def _require_profile_unlock(request: Request, profile_id: str) -> None:
    """Hide private-profile content from a client without its scoped bearer."""
    if not profile_is_accessible(profile_id, request.headers.get(PROFILE_UNLOCK_HEADER)):
        raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)


def _validate_private_workspace(profile_id: str, config: dict[str, Any]) -> None:
    """Keep project defaults on the correct side of every private root."""
    workspace = config.get("workspace")
    if not isinstance(workspace, str) or not workspace:
        return
    protected_profile = next(
        (profile for profile in read_protected_profiles() if profile.profile_id == profile_id),
        None,
    )
    host_id = config.get(
        "host_id",
        protected_profile.host_id if protected_profile is not None else None,
    )
    if host_id is not None and not isinstance(host_id, str):
        raise OmnigentError("Project host_id must be a string.", code=ErrorCode.INVALID_INPUT)
    owner = protected_profile_for_workspace(workspace, host_id=host_id)
    is_private = protected_profile is not None
    if owner is not None and owner != profile_id:
        raise OmnigentError(
            "This workspace belongs to another private profile.",
            code=ErrorCode.INVALID_INPUT,
        )
    if is_private and not workspace_belongs_to_profile(
        profile_id,
        workspace,
        host_id=host_id,
    ):
        raise OmnigentError(
            "A private profile's workspace must be inside one of its protected roots.",
            code=ErrorCode.INVALID_INPUT,
        )


def _profile_is_private(profile_id: str) -> bool:
    """Return whether the profile currently owns protected roots."""
    return any(profile.profile_id == profile_id for profile in read_protected_profiles())


def _legacy_labels_are_colocated(
    project_store: ProjectStore,
    conversation_store: ConversationStore,
) -> bool:
    """Return whether project metadata and legacy labels share one database."""
    conversation_location = (
        conversation_store.conversation_storage_location or conversation_store.storage_location
    )
    return project_store.storage_location == conversation_location


def create_projects_router(
    project_store: ProjectStore,
    auth_provider: AuthProvider | None = None,
    profile_store: ProfileStore | None = None,
    conversation_store: ConversationStore | None = None,
    host_registry: HostRegistry | None = None,
    host_store: HostStore | None = None,
    runner_router: Any = None,
) -> APIRouter:
    """Build the projects router (``/v1/projects``).

    :param project_store: The store backing project persistence.
    :param auth_provider: Auth provider used to identify the requesting user.
        ``None`` in single-user mode (owner scope is ``None``).
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    @router.post("/projects")
    async def create_project(
        request: Request,
        body: CreateProjectRequest,
    ) -> dict[str, Any]:
        """Create a new, empty project owned by the caller.

        :param request: The incoming request, used to identify the user.
        :param body: Project payload (name).
        :returns: The created project as a serialized dict.
        :raises OmnigentError: 401 if unauthenticated in multi-user mode, 409
            if the caller already has a project with this name.
        """
        user_id = require_user(request, auth_provider)
        profile_id = body.profile_id
        if profile_store is not None:
            if profile_id is None:
                default_profile = await asyncio.to_thread(
                    profile_store.ensure_default, user_id=user_id
                )
                profile_id = default_profile.id
            elif await asyncio.to_thread(profile_store.get, profile_id, user_id=user_id) is None:
                raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
            _require_profile_unlock(request, profile_id)
            _validate_private_workspace(profile_id, body.config)
        project = await asyncio.to_thread(
            _coordinated_membership_call,
            (profile_id,) if profile_id is not None else (),
            project_store.create,
            uuid.uuid4().hex,
            body.name,
            user_id,
            body.config,
            profile_id,
        )
        return _to_response(project)

    @router.get("/projects")
    async def list_projects(
        request: Request,
        profile_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """List the caller's projects.

        :param request: The incoming request, used to identify the user.
        :returns: ``{"object": "list", "data": [...]}``.
        :raises OmnigentError: 401 if unauthenticated in multi-user mode.
        """
        user_id = require_user(request, auth_provider)
        if profile_id is not None and profile_store is not None:
            if await asyncio.to_thread(profile_store.get, profile_id, user_id=user_id) is None:
                raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
            _require_profile_unlock(request, profile_id)
        projects = await asyncio.to_thread(
            project_store.list, user_id=user_id, profile_id=profile_id
        )
        projects = [
            project
            for project in projects
            if project.profile_id is None
            or profile_is_accessible(
                project.profile_id, request.headers.get(PROFILE_UNLOCK_HEADER)
            )
        ]
        return {"object": "list", "data": [_to_response(p) for p in projects]}

    @router.get("/projects/{project_id}")
    async def get_project(request: Request, project_id: str) -> dict[str, Any]:
        """Return one of the caller's projects.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to fetch.
        :returns: The project as a serialized dict.
        :raises OmnigentError: 401 if unauthenticated, 404 if not found / not
            owned by the caller.
        """
        user_id = require_user(request, auth_provider)
        project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        if project.profile_id is not None:
            _require_profile_unlock(request, project.profile_id)
        return _to_response(project)

    @router.patch("/projects/{project_id}")
    async def update_project(
        request: Request,
        project_id: str,
        body: UpdateProjectRequest,
    ) -> dict[str, Any]:
        """Update one of the caller's projects (e.g. rename).

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to update.
        :param body: Fields to change; ``None`` fields are left unchanged.
        :returns: The updated project as a serialized dict.
        :raises OmnigentError: 401 if unauthenticated, 404 if not found / not
            owned, 409 if the new name collides with another of the caller's
            projects.
        """
        user_id = require_user(request, auth_provider)
        current = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        if current is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        if current.profile_id is not None:
            _require_profile_unlock(request, current.profile_id)
        destination_profile_id = (
            body.profile_id if "profile_id" in body.model_fields_set else current.profile_id
        )
        moving = destination_profile_id != current.profile_id
        if moving:
            if (
                destination_profile_id is None
                or profile_store is None
                or conversation_store is None
            ):
                raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
            destination = await asyncio.to_thread(
                profile_store.get, destination_profile_id, user_id=user_id
            )
            if destination is None:
                raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
            if project_store.storage_location != profile_store.storage_location:
                raise OmnigentError(
                    "Moving projects requires profiles and projects to share storage.",
                    code=ErrorCode.CONFLICT,
                )
            if not profile_is_accessible(
                destination_profile_id,
                request.headers.get(DESTINATION_PROFILE_UNLOCK_HEADER),
            ):
                raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)

        def validate_destination_workspace(workspace: str, host_id: str | None) -> None:
            if destination_profile_id is not None:
                _validate_private_workspace(
                    destination_profile_id,
                    {"workspace": workspace, "host_id": host_id},
                )

        move_requested = "profile_id" in body.model_fields_set
        reconcile_legacy_members = conversation_store is not None and (
            move_requested or body.name is not None or body.adopt_legacy_name is not None
        )
        labels_are_colocated = (
            _legacy_labels_are_colocated(project_store, conversation_store)
            if reconcile_legacy_members and conversation_store is not None
            else False
        )
        try:
            project = await asyncio.to_thread(
                _coordinated_membership_call,
                tuple(
                    item
                    for item in (current.profile_id, destination_profile_id)
                    if item is not None
                ),
                project_store.update_with_sessions,
                project_id,
                user_id=user_id,
                expected_profile_id=current.profile_id,
                destination_profile_id=destination_profile_id,
                name=body.name,
                config=body.config,
                resolve_legacy_session_ids=(
                    conversation_store.get_legacy_project_session_ids
                    if reconcile_legacy_members and conversation_store is not None
                    else None
                ),
                hold_legacy_membership_lock=(
                    conversation_store.hold_legacy_project_membership_lock
                    if reconcile_legacy_members
                    and conversation_store is not None
                    and not labels_are_colocated
                    else None
                ),
                legacy_project_name=body.adopt_legacy_name,
                legacy_labels_are_colocated=labels_are_colocated,
                clear_legacy_labels=(
                    conversation_store.delete_legacy_project_labels
                    if reconcile_legacy_members
                    and conversation_store is not None
                    and not labels_are_colocated
                    else None
                ),
                validate_workspace=validate_destination_workspace,
            )
        except (
            LegacyProjectMembershipChangedError,
            ProjectDestinationProfileChangedError,
            ProjectMembershipChangedError,
            ProjectProfileChangedError,
            ProjectSessionProfileMismatchError,
        ) as exc:
            raise OmnigentError(
                "Project membership changed while it was being moved; retry the request.",
                code=ErrorCode.CONFLICT,
            ) from exc
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return _to_response(project)

    @router.delete("/projects/{project_id}")
    async def delete_project(request: Request, project_id: str) -> dict[str, Any]:
        """Delete one of the caller's projects.

        Member sessions are not deleted; they are left for the caller to
        unfile (clearing their ``project_id``).

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to delete.
        :returns: ``{"id": ..., "object": "project.deleted", "deleted": True}``.
        :raises OmnigentError: 401 if unauthenticated, 404 if not found / not
            owned by the caller.
        """
        user_id = require_user(request, auth_provider)
        project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        if project.profile_id is not None:
            _require_profile_unlock(request, project.profile_id)
        deleted = await asyncio.to_thread(project_store.delete, project_id, user_id=user_id)
        if not deleted:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return {"id": project_id, "object": "project.deleted", "deleted": True}

    @router.post("/projects/{project_id}/move-folder")
    async def move_project_folder(
        request: Request,
        project_id: str,
        body: MoveProjectFolderRequest,
    ) -> dict[str, Any]:
        """Move a project directory and atomically re-home its metadata."""
        user_id = require_user(request, auth_provider)
        if (
            profile_store is None
            or conversation_store is None
            or host_registry is None
            or host_store is None
            or runner_router is None
        ):
            raise OmnigentError(
                "Project folder moves are unavailable on this server.",
                code=ErrorCode.CONFLICT,
            )
        current = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        if current is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        if current.profile_id is not None:
            _require_profile_unlock(request, current.profile_id)
        destination = await asyncio.to_thread(
            profile_store.get,
            body.profile_id,
            user_id=user_id,
        )
        if destination is None or not profile_is_accessible(
            body.profile_id,
            request.headers.get(DESTINATION_PROFILE_UNLOCK_HEADER),
        ):
            raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)

        source_workspace = current.config.get("workspace")
        source_host_id = current.config.get("host_id")
        destination_root = destination.config.get("workspace")
        destination_host_id = destination.config.get("host_id") or source_host_id
        if not isinstance(source_workspace, str) or not source_workspace.startswith("/"):
            raise OmnigentError(
                "Set an absolute project workspace before moving its folder.",
                code=ErrorCode.INVALID_INPUT,
            )
        if not isinstance(source_host_id, str) or not source_host_id:
            raise OmnigentError(
                "Set a concrete project host before moving its folder.",
                code=ErrorCode.INVALID_INPUT,
            )
        if not isinstance(destination_root, str) or not destination_root.startswith("/"):
            raise OmnigentError(
                "The destination profile needs an absolute default workspace.",
                code=ErrorCode.INVALID_INPUT,
            )
        if destination_host_id != source_host_id:
            raise OmnigentError(
                "Project folders can only move between profiles on the same host.",
                code=ErrorCode.INVALID_INPUT,
            )
        destination_workspace = posixpath.join(
            posixpath.normpath(destination_root),
            posixpath.basename(posixpath.normpath(source_workspace)),
        )
        _validate_private_workspace(
            body.profile_id,
            {"workspace": destination_workspace, "host_id": destination_host_id},
        )

        host = await asyncio.to_thread(host_store.get_host, source_host_id)
        if host is None:
            raise HTTPException(status_code=404, detail="host not found")
        if user_id is not None and host.user_id != user_id:
            raise HTTPException(status_code=403, detail="not your host")
        conn = host_registry.get(source_host_id)
        if conn is None:
            code = ErrorCode.WRONG_REPLICA if host_is_live(host) else ErrorCode.CONFLICT
            raise OmnigentError("host is unavailable", code=code)
        if _MOVE_DIR_CAPABILITY not in conn.hello.capabilities:
            raise OmnigentError(
                "Upgrade this host before moving project folders.",
                code=ErrorCode.CONFLICT,
            )

        first_class_ids = await asyncio.to_thread(
            conversation_store.get_project_member_session_ids,
            current.id,
            expected_profile_id=current.profile_id,
        )
        legacy_ids = await asyncio.to_thread(
            conversation_store.get_legacy_project_session_ids,
            current.name,
            current.profile_id,
            current.id,
        )
        expected_member_ids = tuple(sorted(set(first_class_ids) | set(legacy_ids)))
        members_by_id = await asyncio.to_thread(
            conversation_store.get_conversations,
            list(expected_member_ids),
        )
        if set(members_by_id) != set(expected_member_ids):
            raise OmnigentError(
                "Project membership changed while it was being moved; retry the request.",
                code=ErrorCode.CONFLICT,
            )
        members = [members_by_id[member_id] for member_id in expected_member_ids]
        for member in members:
            if not member.runner_id or not runner_router.runner_is_online(member.runner_id):
                continue
            routed = runner_router.client_for_existing_conversation(member.id)
            if routed is None:
                raise OmnigentError(
                    f"Could not stop running project session {member.id!r}.",
                    code=ErrorCode.RUNNER_UNAVAILABLE,
                )
            try:
                response = await routed.client.post(
                    f"/v1/sessions/{urllib.parse.quote(member.id, safe='')}/events",
                    json={"type": "stop_session"},
                    timeout=15.0,
                )
                response.raise_for_status()
                response = await routed.client.post(
                    f"/v1/sessions/{urllib.parse.quote(member.id, safe='')}/reset-state",
                    timeout=15.0,
                )
                response.raise_for_status()
            except (httpx.HTTPError, ConnectionError, RuntimeError) as exc:
                raise OmnigentError(
                    f"Could not stop running project session {member.id!r}.",
                    code=ErrorCode.RUNNER_UNAVAILABLE,
                ) from exc

        moved = await _move_directory_on_host(
            host_registry,
            conn,
            source_workspace,
            destination_workspace,
        )
        if moved.get("status") != "ok":
            raise HTTPException(
                status_code=502,
                detail=str(moved.get("error") or "host directory move failed"),
            )
        if moved.get("error"):
            raise HTTPException(status_code=409, detail=str(moved["error"]))
        canonical_source = moved.get("source_path")
        canonical_destination = moved.get("destination_path")
        if not isinstance(canonical_source, str) or not isinstance(canonical_destination, str):
            raise HTTPException(status_code=502, detail="host returned invalid move paths")
        if _profile_is_private(body.profile_id) and not workspace_belongs_to_profile(
            body.profile_id,
            canonical_destination,
            host_id=destination_host_id,
        ):
            rollback = await _move_directory_on_host(
                host_registry,
                conn,
                canonical_destination,
                canonical_source,
            )
            if rollback.get("status") != "ok" or rollback.get("error"):
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "The host moved the folder outside the protected root and rollback failed."
                    ),
                )
            raise OmnigentError(
                "The host-verified destination is outside the private profile's protected roots.",
                code=ErrorCode.INVALID_INPUT,
            )

        def validate_destination_workspace(workspace: str, host_id: str | None) -> None:
            if host_id != destination_host_id:
                raise OmnigentError(
                    "Every project session must use the private profile's host.",
                    code=ErrorCode.INVALID_INPUT,
                )
            _validate_private_workspace(
                body.profile_id,
                {"workspace": workspace, "host_id": host_id},
            )

        try:
            project = await asyncio.to_thread(
                _coordinated_membership_call,
                tuple(item for item in (current.profile_id, body.profile_id) if item is not None),
                project_store.update_with_sessions,
                project_id,
                user_id=user_id,
                expected_profile_id=current.profile_id,
                destination_profile_id=body.profile_id,
                validate_workspace=validate_destination_workspace,
                workspace_relocation=(source_workspace, canonical_destination),
                expected_member_ids=expected_member_ids,
            )
            if project is None:
                raise ProjectProfileChangedError(project_id)
        except Exception as exc:
            rollback = await _move_directory_on_host(
                host_registry,
                conn,
                canonical_destination,
                canonical_source,
            )
            if rollback.get("status") != "ok" or rollback.get("error"):
                raise HTTPException(
                    status_code=500,
                    detail="Project metadata failed to move and the folder rollback also failed.",
                ) from None
            if isinstance(exc, ProjectWorkspaceRelocationError):
                raise OmnigentError(
                    "A project session uses a workspace outside the project folder.",
                    code=ErrorCode.INVALID_INPUT,
                ) from exc
            if isinstance(exc, ProjectMembershipChangedError):
                raise OmnigentError(
                    "Project membership changed while it was being moved; retry the request.",
                    code=ErrorCode.CONFLICT,
                ) from exc
            raise
        return _to_response(project)

    return router
