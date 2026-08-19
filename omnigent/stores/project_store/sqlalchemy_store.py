"""SQLAlchemy-backed project store."""

from __future__ import annotations

import json
import posixpath
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, nullcontext
from typing import Any

from sqlalchemy import and_, asc, delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlConversationLabel,
    SqlConversationMetadata,
    SqlProfile,
    SqlProject,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
)
from omnigent.entities import Project
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.conversation_store import PROJECT_LABEL_KEY
from omnigent.stores.project_store import (
    LegacyProjectMembershipChangedError,
    ProjectDestinationProfileChangedError,
    ProjectMembershipChangedError,
    ProjectProfileChangedError,
    ProjectSessionProfileMismatchError,
    ProjectStore,
    ProjectWorkspaceRelocationError,
    legacy_project_membership_guard,
)
from omnigent.stores.project_store.legacy_membership import (
    acquire_legacy_membership_db_lock,
)

# Max serialized length of a project's config blob. The value is persisted
# verbatim and reflected back on every read, so an unbounded blob is a mild
# storage/response-size amplifier on an otherwise cheap CRUD row. 64 KiB is far
# above any realistic set of default-session hints (a few short keys) while
# still capping abuse.
_CONFIG_MAX_SERIALIZED_LEN = 64 * 1024


def _relocated_workspace(path: str, source: str, destination: str) -> str:
    """Rewrite a project-root path while rejecting members outside that root."""
    normalized_path = posixpath.normpath(path)
    normalized_source = posixpath.normpath(source)
    try:
        if posixpath.commonpath((normalized_source, normalized_path)) != normalized_source:
            raise ProjectWorkspaceRelocationError(path)
    except ValueError as exc:
        raise ProjectWorkspaceRelocationError(path) from exc
    relative = posixpath.relpath(normalized_path, normalized_source)
    return (
        posixpath.normpath(destination)
        if relative == "."
        else posixpath.join(posixpath.normpath(destination), relative)
    )


def _owner_scope(user_id: str | None) -> tuple[bool, str]:
    """Return the normalized owner columns used by project constraints."""
    return user_id is None, user_id or ""


_NAME_CONSTRAINTS = {
    "uq_projects_workspace_owner_profile_name",
    "uq_projects_workspace_owner_unprofiled_name",
}


def _is_name_conflict(error: IntegrityError) -> bool:
    """Recognize project-name constraints without masking other DB faults."""
    original = error.orig
    diagnostic = getattr(original, "diag", None)
    if getattr(diagnostic, "constraint_name", None) in _NAME_CONSTRAINTS:
        return True
    message = str(original).lower()
    return "unique constraint failed" in message and "projects.name" in message


def _raise_name_conflict(name: str, error: IntegrityError) -> None:
    if not _is_name_conflict(error):
        raise error
    raise OmnigentError(
        f"A project named {name!r} already exists",
        code=ErrorCode.ALREADY_EXISTS,
    ) from error


def _encode_config(config: dict[str, Any] | None) -> str | None:
    """Pack a project's config dict into a compact JSON blob for storage.

    An empty or ``None`` config stores SQL ``NULL`` rather than ``"{}"``, so
    "no defaults" is one canonical representation.

    :param config: The config object, or ``None``.
    :returns: Compact JSON object string, or ``None`` when empty.
    :raises OmnigentError: ``INVALID_INPUT`` if the serialized config exceeds
        :data:`_CONFIG_MAX_SERIALIZED_LEN`.
    """
    if not config:
        return None
    blob = json.dumps(config, separators=(",", ":"))
    if len(blob) > _CONFIG_MAX_SERIALIZED_LEN:
        raise OmnigentError(
            f"project config too large ({len(blob)} bytes; max {_CONFIG_MAX_SERIALIZED_LEN})",
            code=ErrorCode.INVALID_INPUT,
        )
    return blob


def _decode_config(raw: str | None) -> dict[str, Any]:
    """Unpack the stored ``config`` blob to a dict (``{}`` when unset).

    Defensive against a non-object blob: the encode path only ever writes JSON
    objects, but a future writer or a manual DB edit could store a scalar/array,
    which would otherwise flow back as a non-dict. Coerce anything that isn't a
    dict to ``{}`` so callers can always treat config as a mapping.

    :param raw: The stored JSON blob, or ``None``.
    :returns: The decoded object, or an empty dict when ``NULL`` / empty / non-object.
    """
    if not raw:
        return {}
    decoded = json.loads(raw)
    return decoded if isinstance(decoded, dict) else {}


def _to_entity(row: SqlProject) -> Project:
    """
    Convert a :class:`SqlProject` ORM row to a :class:`Project`.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`Project` dataclass instance.
    """
    return Project(
        id=row.id,
        name=row.name,
        user_id=row.user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        profile_id=row.profile_id,
        config=_decode_config(row.config),
    )


class SqlAlchemyProjectStore(ProjectStore):
    """
    SQLAlchemy-backed implementation of :class:`ProjectStore`.

    Persists projects in a relational database via the SQLAlchemy ORM. Every
    query is scoped by ``workspace_id`` (tenant partition) and ``user_id``
    (projects are owner-private).
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy project store.

        Creates or reuses a SQLAlchemy engine and session factory for the given
        database URI.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.project_store",
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.project_store",
            immediate=True,
        )
        self._supports_for_update = self._engine.dialect.name != "sqlite"

    def _name_taken(
        self,
        session: Session,
        *,
        user_id: str | None,
        profile_id: str | None,
        name: str,
        exclude_id: str | None,
    ) -> bool:
        """Return whether this profile already has a project named ``name``.

        This gives callers an early, descriptive conflict. Database constraints
        remain the authoritative guard against concurrent writers.

        :param session: The active SQLAlchemy session.
        :param user_id: The owner scope.
        :param profile_id: The profile scope.
        :param name: The candidate name.
        :param exclude_id: A project id to exclude (the row being renamed).
        :returns: ``True`` if this owner/profile already has the name.
        """
        stmt = select(SqlProject.id).where(
            SqlProject.workspace_id == current_workspace_id(),
            SqlProject.user_id == user_id,
            SqlProject.profile_id == profile_id,
            SqlProject.name == name,
        )
        if exclude_id is not None:
            stmt = stmt.where(SqlProject.id != exclude_id)
        return session.execute(stmt).first() is not None

    def create(
        self,
        project_id: str,
        name: str,
        user_id: str | None,
        config: dict[str, Any] | None = None,
        profile_id: str | None = None,
    ) -> Project:
        """Insert a new, empty project.

        Rejects a name the profile already uses. A database constraint closes
        the race between the descriptive pre-check and the insert.
        """
        with self._session("create_project") as session:
            if self._name_taken(
                session,
                user_id=user_id,
                profile_id=profile_id,
                name=name,
                exclude_id=None,
            ):
                raise OmnigentError(
                    f"A project named {name!r} already exists",
                    code=ErrorCode.ALREADY_EXISTS,
                )
            owner_is_anonymous, owner_scope = _owner_scope(user_id)
            row = SqlProject(
                id=project_id,
                name=name,
                user_id=user_id,
                owner_is_anonymous=owner_is_anonymous,
                owner_scope=owner_scope,
                profile_id=profile_id,
                profile_unassigned_slot=1 if profile_id is None else None,
                created_at=now_epoch(),
                updated_at=None,
                config=_encode_config(config),
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError as error:
                _raise_name_conflict(name, error)
            return _to_entity(row)

    def get(self, project_id: str, *, user_id: str | None) -> Project | None:
        """Return an owned project by id, or ``None`` if not found."""
        with self._session("resolve_owned_project") as session:
            row = session.get(SqlProject, (current_workspace_id(), project_id))
            if row is None or row.user_id != user_id:
                return None
            return _to_entity(row)

    def list(self, *, user_id: str | None, profile_id: str | None = None) -> list[Project]:
        """List the owner's projects ordered by ``created_at ASC, id ASC``."""
        with self._session("list_projects") as session:
            stmt = select(SqlProject).where(
                SqlProject.workspace_id == current_workspace_id(),
                SqlProject.user_id == user_id,
            )
            if profile_id is not None:
                stmt = stmt.where(SqlProject.profile_id == profile_id)
            stmt = stmt.order_by(asc(SqlProject.created_at), asc(SqlProject.id))
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def update(
        self,
        project_id: str,
        *,
        user_id: str | None,
        name: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> Project | None:
        """Update mutable fields of an owned project.

        ``None`` leaves a field unchanged. Returns ``None`` if the project does
        not exist or is not owned by ``user_id``. A ``config`` of ``{}`` clears
        the stored defaults (distinct from ``None`` = leave unchanged).

        A rename re-checks ``_name_taken`` for a descriptive error, while the
        database constraint closes concurrent rename races.
        """
        with self._session("update_project") as session:
            row = session.get(SqlProject, (current_workspace_id(), project_id))
            if row is None or row.user_id != user_id:
                return None
            changed = False
            if name is not None and row.name != name:
                if self._name_taken(
                    session,
                    user_id=user_id,
                    profile_id=row.profile_id,
                    name=name,
                    exclude_id=project_id,
                ):
                    raise OmnigentError(
                        f"A project named {name!r} already exists",
                        code=ErrorCode.ALREADY_EXISTS,
                    )
                row.name = name
                changed = True
            if config is not None:
                encoded = _encode_config(config)
                if row.config != encoded:
                    row.config = encoded
                    changed = True
            if changed:
                row.updated_at = now_epoch()
            try:
                session.flush()
            except IntegrityError as error:
                _raise_name_conflict(name or row.name, error)
            return _to_entity(row)

    def delete(self, project_id: str, *, user_id: str | None) -> bool:
        """Delete an owned project. Idempotent; returns ``False`` if not found."""
        with self._session("delete_project") as session:
            row = session.get(SqlProject, (current_workspace_id(), project_id))
            if row is None or row.user_id != user_id:
                return False
            session.delete(row)
            return True

    def update_with_sessions(
        self,
        project_id: str,
        *,
        user_id: str | None,
        expected_profile_id: str | None,
        destination_profile_id: str | None,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        resolve_legacy_session_ids: (
            Callable[[str, str | None, str], Iterable[str]] | None
        ) = None,
        hold_legacy_membership_lock: (Callable[[], AbstractContextManager[None]] | None) = None,
        legacy_project_name: str | None = None,
        legacy_labels_are_colocated: bool = False,
        clear_legacy_labels: Callable[[tuple[str, ...]], None] | None = None,
        validate_workspace: Callable[[str, str | None], None] | None = None,
        workspace_relocation: tuple[str, str] | None = None,
        expected_member_ids: tuple[str, ...] | None = None,
    ) -> Project | None:
        """Update a project and every member metadata row in one transaction.

        The project row is locked before its members. Session filing uses the
        same order, so a concurrent file either joins this move or observes the
        destination profile and is rejected when the session is still in the
        source profile.
        """
        cleanup_legacy_ids: tuple[str, ...] = ()
        result: Project | None = None
        membership_db_guard = (
            hold_legacy_membership_lock()
            if hold_legacy_membership_lock is not None
            else nullcontext()
        )
        with (
            legacy_project_membership_guard(),
            membership_db_guard,
            self._session_immediate("move_project_with_sessions") as session,
        ):
            project_stmt = select(SqlProject).where(
                SqlProject.workspace_id == current_workspace_id(),
                SqlProject.id == project_id,
                SqlProject.user_id == user_id,
            )
            if self._supports_for_update:
                project_stmt = project_stmt.with_for_update()
            row = session.execute(project_stmt).scalar_one_or_none()
            if row is None:
                return None
            if row.profile_id != expected_profile_id:
                raise ProjectProfileChangedError(project_id)

            if destination_profile_id is not None:
                destination_stmt = select(SqlProfile).where(
                    SqlProfile.workspace_id == current_workspace_id(),
                    SqlProfile.id == destination_profile_id,
                    SqlProfile.user_id == user_id,
                )
                if self._supports_for_update:
                    destination_stmt = destination_stmt.with_for_update()
                destination = session.execute(destination_stmt).scalar_one_or_none()
                if destination is None:
                    raise ProjectDestinationProfileChangedError(destination_profile_id)

            effective_name = name if name is not None else row.name
            legacy_name = legacy_project_name or row.name
            if legacy_labels_are_colocated:
                acquire_legacy_membership_db_lock(session)
            legacy_ids = (
                self._resolve_colocated_legacy_members(session, legacy_name, expected_profile_id)
                if legacy_labels_are_colocated
                else self._resolve_legacy_members(
                    resolve_legacy_session_ids,
                    legacy_name,
                    expected_profile_id,
                    project_id,
                )
            )
            if self._name_taken(
                session,
                user_id=user_id,
                profile_id=destination_profile_id,
                name=effective_name,
                exclude_id=project_id,
            ):
                raise OmnigentError(
                    f"A project named {effective_name!r} already exists",
                    code=ErrorCode.ALREADY_EXISTS,
                )

            member_filter = SqlConversationMetadata.project_id == project_id
            if legacy_ids:
                member_filter = or_(
                    member_filter,
                    SqlConversationMetadata.id.in_(legacy_ids),
                )
            members_stmt = (
                select(SqlConversationMetadata)
                .where(
                    SqlConversationMetadata.workspace_id == current_workspace_id(),
                    member_filter,
                )
                .order_by(SqlConversationMetadata.id)
            )
            if self._supports_for_update:
                members_stmt = members_stmt.with_for_update()
            members = list(session.execute(members_stmt).scalars())
            if expected_member_ids is not None and {member.id for member in members} != set(
                expected_member_ids
            ):
                raise ProjectMembershipChangedError(project_id)
            revalidated_legacy_ids = (
                self._resolve_colocated_legacy_members(session, legacy_name, expected_profile_id)
                if legacy_labels_are_colocated
                else self._resolve_legacy_members(
                    resolve_legacy_session_ids,
                    legacy_name,
                    expected_profile_id,
                    project_id,
                )
            )
            if revalidated_legacy_ids != legacy_ids:
                raise LegacyProjectMembershipChangedError(project_id)
            if any(member.profile_id != expected_profile_id for member in members):
                raise ProjectSessionProfileMismatchError(project_id)

            encoded_config = _encode_config(config) if config is not None else row.config
            if workspace_relocation is not None:
                source_workspace, destination_workspace = workspace_relocation
                relocated_config = _decode_config(encoded_config)
                configured_workspace = relocated_config.get("workspace")
                if not isinstance(configured_workspace, str) or not configured_workspace:
                    raise ProjectWorkspaceRelocationError(project_id)
                relocated_config["workspace"] = _relocated_workspace(
                    configured_workspace,
                    source_workspace,
                    destination_workspace,
                )
                encoded_config = _encode_config(relocated_config)
                for member in members:
                    if member.workspace:
                        member.workspace = _relocated_workspace(
                            member.workspace,
                            source_workspace,
                            destination_workspace,
                        )
            if validate_workspace is not None:
                effective_config = _decode_config(encoded_config)
                workspace = effective_config.get("workspace")
                project_host_id = effective_config.get("host_id")
                if project_host_id is not None and not isinstance(project_host_id, str):
                    raise ProjectDestinationProfileChangedError(destination_profile_id or "")
                if isinstance(workspace, str) and workspace:
                    validate_workspace(workspace, project_host_id)
                for member in members:
                    if member.workspace:
                        validate_workspace(member.workspace, member.host_id)

            changed = False
            if row.name != effective_name:
                row.name = effective_name
                changed = True
            if (
                config is not None or workspace_relocation is not None
            ) and row.config != encoded_config:
                row.config = encoded_config
                changed = True
            if row.profile_id != destination_profile_id:
                row.profile_id = destination_profile_id
                row.profile_unassigned_slot = 1 if destination_profile_id is None else None
                changed = True
            for member in members:
                if member.profile_id != destination_profile_id:
                    member.profile_id = destination_profile_id
                    changed = True
                if member.project_id != project_id:
                    member.project_id = project_id
                    changed = True
            self._clear_legacy_labels(
                session,
                legacy_ids,
                labels_are_colocated=legacy_labels_are_colocated,
            )
            if not legacy_labels_are_colocated:
                cleanup_legacy_ids = legacy_ids
            if changed:
                row.updated_at = now_epoch()
            try:
                session.flush()
            except IntegrityError as error:
                _raise_name_conflict(effective_name, error)
            result = _to_entity(row)
        if cleanup_legacy_ids and clear_legacy_labels is not None:
            clear_legacy_labels(cleanup_legacy_ids)
        return result

    @staticmethod
    def _resolve_legacy_members(
        resolver: Callable[[str, str | None, str], Iterable[str]] | None,
        project_name: str,
        profile_id: str | None,
        project_id: str,
    ) -> tuple[str, ...]:
        """Return a deterministic legacy-membership snapshot."""
        if resolver is None:
            return ()
        return tuple(sorted(set(resolver(project_name, profile_id, project_id))))

    def _resolve_colocated_legacy_members(
        self,
        session: Session,
        project_name: str,
        profile_id: str | None,
    ) -> tuple[str, ...]:
        """Resolve legacy members inside the owning project transaction."""
        stmt = (
            select(SqlConversationMetadata.id)
            .join(
                SqlConversationLabel,
                and_(
                    SqlConversationLabel.workspace_id == SqlConversationMetadata.workspace_id,
                    SqlConversationLabel.conversation_id == SqlConversationMetadata.id,
                ),
            )
            .where(
                SqlConversationMetadata.workspace_id == current_workspace_id(),
                SqlConversationMetadata.profile_id == profile_id,
                SqlConversationMetadata.project_id.is_(None),
                SqlConversationLabel.key == PROJECT_LABEL_KEY,
                SqlConversationLabel.value == project_name,
            )
            .order_by(SqlConversationMetadata.id)
        )
        if self._supports_for_update:
            stmt = stmt.with_for_update()
        return tuple(session.execute(stmt).scalars())

    @staticmethod
    def _clear_legacy_labels(
        session: Session,
        session_ids: tuple[str, ...],
        *,
        labels_are_colocated: bool,
    ) -> None:
        """Clear adopted legacy labels in the owning storage transaction."""
        if not session_ids:
            return
        if labels_are_colocated:
            session.execute(
                delete(SqlConversationLabel).where(
                    SqlConversationLabel.workspace_id == current_workspace_id(),
                    SqlConversationLabel.conversation_id.in_(session_ids),
                    SqlConversationLabel.key == PROJECT_LABEL_KEY,
                )
            )

    def file_session(
        self,
        project_id: str,
        session_id: str,
        *,
        user_id: str | None,
        clear_legacy_label: bool = False,
        legacy_label_is_colocated: bool = False,
        clear_legacy_label_fallback: Callable[[], None] | None = None,
    ) -> bool:
        """File a session while holding the target project lock."""
        cleanup_legacy_label = False
        with (
            legacy_project_membership_guard(),
            self._session_immediate("file_session_in_project") as session,
        ):
            if clear_legacy_label and legacy_label_is_colocated:
                acquire_legacy_membership_db_lock(session)
            project_stmt = select(SqlProject).where(
                SqlProject.workspace_id == current_workspace_id(),
                SqlProject.id == project_id,
                SqlProject.user_id == user_id,
            )
            if self._supports_for_update:
                project_stmt = project_stmt.with_for_update()
            project = session.execute(project_stmt).scalar_one_or_none()
            if project is None:
                return False

            member_stmt = select(SqlConversationMetadata).where(
                SqlConversationMetadata.workspace_id == current_workspace_id(),
                SqlConversationMetadata.id == session_id,
            )
            if self._supports_for_update:
                member_stmt = member_stmt.with_for_update()
            member = session.execute(member_stmt).scalar_one_or_none()
            if member is None:
                return False
            if member.profile_id != project.profile_id:
                raise ProjectSessionProfileMismatchError(project_id)
            member.project_id = project_id
            if clear_legacy_label:
                self._clear_legacy_labels(
                    session,
                    (session_id,),
                    labels_are_colocated=legacy_label_is_colocated,
                )
                cleanup_legacy_label = not legacy_label_is_colocated
        if cleanup_legacy_label and clear_legacy_label_fallback is not None:
            clear_legacy_label_fallback()
        return True

    def unfile_session(
        self,
        session_id: str,
        *,
        clear_legacy_label: bool = False,
        legacy_label_is_colocated: bool = False,
        clear_legacy_label_fallback: Callable[[], None] | None = None,
    ) -> bool:
        """Unfile a session and clear its legacy label under one guard."""
        cleanup_legacy_label = False
        with (
            legacy_project_membership_guard(),
            self._session_immediate("unfile_session") as session,
        ):
            if clear_legacy_label and legacy_label_is_colocated:
                acquire_legacy_membership_db_lock(session)
            member = session.get(
                SqlConversationMetadata,
                (current_workspace_id(), session_id),
            )
            if member is None:
                return False
            member.project_id = None
            if clear_legacy_label:
                self._clear_legacy_labels(
                    session,
                    (session_id,),
                    labels_are_colocated=legacy_label_is_colocated,
                )
                cleanup_legacy_label = not legacy_label_is_colocated
        if cleanup_legacy_label and clear_legacy_label_fallback is not None:
            clear_legacy_label_fallback()
        return True
