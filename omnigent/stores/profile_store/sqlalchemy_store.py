"""SQLAlchemy-backed profile store."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import asc, desc, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlConversationMetadata,
    SqlProfile,
    SqlProject,
    SqlSessionPermission,
    current_workspace_id,
)
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker, now_epoch
from omnigent.entities import Profile
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER
from omnigent.stores.profile_store import ProfileStore

_JSON_MAX_SERIALIZED_LEN = 64 * 1024
_NAME_CONSTRAINT = "uq_profiles_workspace_owner_name"


def _encode(value: dict[str, Any] | None) -> str | None:
    if not value:
        return None
    raw = json.dumps(value, separators=(",", ":"))
    if len(raw) > _JSON_MAX_SERIALIZED_LEN:
        raise OmnigentError("profile settings are too large", code=ErrorCode.INVALID_INPUT)
    return raw


def _decode(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    decoded = json.loads(value)
    return decoded if isinstance(decoded, dict) else {}


def _to_entity(row: SqlProfile) -> Profile:
    return Profile(
        id=row.id,
        name=row.name,
        user_id=row.user_id,
        icon=row.icon,
        color=row.color,
        is_default=row.is_default,
        config=_decode(row.config),
        protection=_decode(row.protection),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _owner_identity(user_id: str | None) -> tuple[bool, str]:
    return user_id is None, "" if user_id is None else user_id


def _find_default(session: Session, *, user_id: str | None) -> SqlProfile | None:
    return session.execute(
        select(SqlProfile)
        .where(
            SqlProfile.workspace_id == current_workspace_id(),
            SqlProfile.user_id == user_id,
            SqlProfile.is_default.is_(True),
        )
        .order_by(asc(SqlProfile.created_at), asc(SqlProfile.id))
        .limit(1)
    ).scalar_one_or_none()


def _is_name_conflict(error: IntegrityError) -> bool:
    """Recognize only the portable profile-name uniqueness invariant."""
    original = error.orig
    diagnostic = getattr(original, "diag", None)
    if getattr(diagnostic, "constraint_name", None) == _NAME_CONSTRAINT:
        return True
    message = str(original).lower()
    return "unique constraint failed" in message and all(
        column in message
        for column in (
            "profiles.workspace_id",
            "profiles.owner_is_anonymous",
            "profiles.owner_scope",
            "profiles.name",
        )
    )


def _raise_name_conflict(name: str, error: IntegrityError) -> None:
    if not _is_name_conflict(error):
        raise error
    raise OmnigentError(
        f"A profile named {name!r} already exists",
        code=ErrorCode.ALREADY_EXISTS,
    ) from error


class SqlAlchemyProfileStore(ProfileStore):
    """Persist profiles and adopt legacy rows into the default profile."""

    def __init__(self, storage_location: str) -> None:
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.profile_store",
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.profile_store",
            immediate=True,
        )
        self._supports_for_update = self._engine.dialect.name != "sqlite"

    def _name_taken(
        self,
        session: Session,
        *,
        user_id: str | None,
        name: str,
        exclude_id: str | None = None,
    ) -> bool:
        stmt = select(SqlProfile.id).where(
            SqlProfile.workspace_id == current_workspace_id(),
            SqlProfile.user_id == user_id,
            SqlProfile.name == name,
        )
        if exclude_id is not None:
            stmt = stmt.where(SqlProfile.id != exclude_id)
        return session.execute(stmt).first() is not None

    def _adopt_legacy_rows(
        self,
        session: Session,
        *,
        user_id: str | None,
        profile_id: str,
    ) -> None:
        session.execute(
            update(SqlProject)
            .where(
                SqlProject.workspace_id == current_workspace_id(),
                SqlProject.user_id == user_id,
                SqlProject.profile_id.is_(None),
            )
            .values(profile_id=profile_id, profile_unassigned_slot=None)
        )
        metadata = update(SqlConversationMetadata).where(
            SqlConversationMetadata.workspace_id == current_workspace_id(),
            SqlConversationMetadata.profile_id.is_(None),
        )
        if user_id is not None:
            owned_ids = select(SqlSessionPermission.conversation_id).where(
                SqlSessionPermission.workspace_id == current_workspace_id(),
                SqlSessionPermission.user_id == user_id,
                SqlSessionPermission.level >= LEVEL_OWNER,
            )
            metadata = metadata.where(SqlConversationMetadata.id.in_(owned_ids))
        session.execute(metadata.values(profile_id=profile_id))

    def ensure_default(self, *, user_id: str | None) -> Profile:
        with self._session("resolve_default_profile") as session:
            row = _find_default(session, user_id=user_id)
            if row is not None:
                self._adopt_legacy_rows(session, user_id=user_id, profile_id=row.id)
                return _to_entity(row)

        try:
            with self._session("establish_default_profile") as session:
                owner_is_anonymous, owner_scope = _owner_identity(user_id)
                row = SqlProfile(
                    id=uuid.uuid4().hex,
                    name="Personal",
                    user_id=user_id,
                    owner_is_anonymous=owner_is_anonymous,
                    owner_scope=owner_scope,
                    is_default=True,
                    default_slot=1,
                    created_at=now_epoch(),
                )
                session.add(row)
                session.flush()
                self._adopt_legacy_rows(session, user_id=user_id, profile_id=row.id)
                return _to_entity(row)
        except IntegrityError as collision:
            # A concurrent caller established the same owner's default.
            with self._session("recover_concurrent_default_profile") as session:
                row = _find_default(session, user_id=user_id)
                if row is None:
                    raise collision
                self._adopt_legacy_rows(session, user_id=user_id, profile_id=row.id)
                return _to_entity(row)

    def create(
        self,
        profile_id: str,
        name: str,
        user_id: str | None,
        *,
        icon: str | None = None,
        color: str | None = None,
        config: dict[str, Any] | None = None,
        protection: dict[str, Any] | None = None,
    ) -> Profile:
        with self._session("create_profile") as session:
            if self._name_taken(session, user_id=user_id, name=name):
                raise OmnigentError(
                    f"A profile named {name!r} already exists",
                    code=ErrorCode.ALREADY_EXISTS,
                )
            owner_is_anonymous, owner_scope = _owner_identity(user_id)
            row = SqlProfile(
                id=profile_id,
                name=name,
                user_id=user_id,
                owner_is_anonymous=owner_is_anonymous,
                owner_scope=owner_scope,
                icon=icon,
                color=color,
                is_default=False,
                default_slot=None,
                config=_encode(config),
                protection=_encode(protection),
                created_at=now_epoch(),
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError as error:
                _raise_name_conflict(name, error)
            return _to_entity(row)

    def get(self, profile_id: str, *, user_id: str | None) -> Profile | None:
        with self._session("resolve_owned_profile") as session:
            row = session.get(SqlProfile, (current_workspace_id(), profile_id))
            return None if row is None or row.user_id != user_id else _to_entity(row)

    def list(self, *, user_id: str | None) -> list[Profile]:
        self.ensure_default(user_id=user_id)
        with self._session("list_profiles") as session:
            rows = session.execute(
                select(SqlProfile)
                .where(
                    SqlProfile.workspace_id == current_workspace_id(),
                    SqlProfile.user_id == user_id,
                )
                .order_by(
                    desc(SqlProfile.is_default),
                    asc(SqlProfile.created_at),
                    asc(SqlProfile.id),
                )
            ).scalars()
            return [_to_entity(row) for row in rows]

    def get_protection(self, profile_id: str) -> dict[str, Any]:
        with self._session("read_profile_protection_policy") as session:
            row = session.get(SqlProfile, (current_workspace_id(), profile_id))
            return {} if row is None else _decode(row.protection)

    def list_protected_profile_ids(self) -> frozenset[str]:
        """Return profiles whose database policy declares a lock."""
        with self._session("list_protected_profile_ids") as session:
            rows = session.execute(
                select(SqlProfile.id, SqlProfile.protection).where(
                    SqlProfile.workspace_id == current_workspace_id()
                )
            ).all()
        return frozenset(
            profile_id
            for profile_id, raw_protection in rows
            if _decode(raw_protection).get("lock") in {"passcode", "device"}
        )

    def update(
        self,
        profile_id: str,
        *,
        user_id: str | None,
        name: str | None = None,
        icon: str | None = None,
        color: str | None = None,
        config: dict[str, Any] | None = None,
        protection: dict[str, Any] | None = None,
    ) -> Profile | None:
        with self._session("update_profile") as session:
            row = session.get(SqlProfile, (current_workspace_id(), profile_id))
            if row is None or row.user_id != user_id:
                return None
            changed = False
            if name is not None and name != row.name:
                if self._name_taken(session, user_id=user_id, name=name, exclude_id=profile_id):
                    raise OmnigentError(
                        f"A profile named {name!r} already exists",
                        code=ErrorCode.ALREADY_EXISTS,
                    )
                row.name = name
                changed = True
            for attr, value in (("icon", icon), ("color", color)):
                if value is not None and getattr(row, attr) != value:
                    setattr(row, attr, value)
                    changed = True
            for attr, value in (("config", config), ("protection", protection)):
                if value is not None:
                    encoded = _encode(value)
                    if getattr(row, attr) != encoded:
                        setattr(row, attr, encoded)
                        changed = True
            if changed:
                row.updated_at = now_epoch()
            try:
                session.flush()
            except IntegrityError as error:
                _raise_name_conflict(name or row.name, error)
            return _to_entity(row)

    def delete(self, profile_id: str, *, user_id: str | None) -> bool:
        with self._session_immediate("delete_profile") as session:
            stmt = select(SqlProfile).where(
                SqlProfile.workspace_id == current_workspace_id(),
                SqlProfile.id == profile_id,
                SqlProfile.user_id == user_id,
            )
            if self._supports_for_update:
                stmt = stmt.with_for_update()
            row = session.execute(stmt).scalar_one_or_none()
            if row is None:
                return False
            if row.is_default:
                raise OmnigentError(
                    "The default profile cannot be deleted",
                    code=ErrorCode.CONFLICT,
                )
            has_projects = session.execute(
                select(SqlProject.id)
                .where(
                    SqlProject.workspace_id == current_workspace_id(),
                    SqlProject.profile_id == profile_id,
                )
                .limit(1)
            ).first()
            has_sessions = session.execute(
                select(SqlConversationMetadata.id)
                .where(
                    SqlConversationMetadata.workspace_id == current_workspace_id(),
                    SqlConversationMetadata.profile_id == profile_id,
                )
                .limit(1)
            ).first()
            if has_projects or has_sessions:
                raise OmnigentError(
                    "Move this profile's content before deleting it",
                    code=ErrorCode.CONFLICT,
                )
            session.delete(row)
            return True

    def restore(self, profile: Profile) -> None:
        """Restore an exact profile snapshot after a coordinated delete fails."""
        owner_is_anonymous, owner_scope = _owner_identity(profile.user_id)
        with self._session_immediate("restore_profile") as session:
            if session.get(SqlProfile, (current_workspace_id(), profile.id)) is not None:
                return
            session.add(
                SqlProfile(
                    id=profile.id,
                    name=profile.name,
                    user_id=profile.user_id,
                    owner_is_anonymous=owner_is_anonymous,
                    owner_scope=owner_scope,
                    icon=profile.icon,
                    color=profile.color,
                    is_default=profile.is_default,
                    default_slot=1 if profile.is_default else None,
                    config=_encode(profile.config),
                    protection=_encode(profile.protection),
                    created_at=profile.created_at,
                    updated_at=profile.updated_at,
                )
            )
