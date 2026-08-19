"""Shared persistence for private-profile isolation state."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from omnigent.db.db_models import (
    SqlProfileProtectionRegistry,
    current_workspace_id,
)
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker, now_epoch
from omnigent.errors import ErrorCode, OmnigentError


def empty_profile_protection_payload() -> dict[str, Any]:
    """Return a fresh empty registry payload."""
    return {
        "generation": 0,
        "profiles": [],
        "leases": [],
        "unlock_attempts": [],
        "membership_writes": [],
        "pending_profiles": [],
    }


def legacy_profile_protection_checksum(payload: dict[str, Any]) -> str:
    """Fingerprint durable legacy protection state, excluding runner leases."""
    durable = {
        "generation": payload.get("generation", 0),
        "profiles": payload.get("profiles", []),
    }
    return hashlib.sha256(_encode(durable).encode()).hexdigest()


def _decode(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise OmnigentError(
            "Private-profile protection settings are malformed.",
            code=ErrorCode.INTERNAL_ERROR,
        ) from exc
    if not isinstance(value, dict):
        raise OmnigentError(
            "Private-profile protection settings are malformed.",
            code=ErrorCode.INTERNAL_ERROR,
        )
    return value


def _encode(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


class ProfileProtectionRegistryStore:
    """Serialize registry mutations through one database row per workspace."""

    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        self._supports_for_update = engine.dialect.name != "sqlite"
        self._session = make_named_managed_session_maker(
            engine,
            query_name_prefix="omnigent.profile_protection_registry",
        )
        self._session_immediate = make_named_managed_session_maker(
            engine,
            query_name_prefix="omnigent.profile_protection_registry",
            immediate=True,
        )
        self._ensure_row()

    def _ensure_row(self) -> None:
        try:
            with self._session_immediate("ensure_registry") as session:
                row = session.get(
                    SqlProfileProtectionRegistry,
                    current_workspace_id(),
                )
                if row is None:
                    session.add(
                        SqlProfileProtectionRegistry(
                            workspace_id=current_workspace_id(),
                            payload=_encode(empty_profile_protection_payload()),
                            updated_at=now_epoch(),
                        )
                    )
        except IntegrityError:
            # Another worker established the singleton concurrently.
            return

    def read(self) -> dict[str, Any]:
        """Read a detached registry snapshot."""
        with self._session("read_registry") as session:
            row = session.get(SqlProfileProtectionRegistry, current_workspace_id())
            if row is None:
                return empty_profile_protection_payload()
            return deepcopy(_decode(row.payload))

    @contextmanager
    def locked(self) -> Iterator[dict[str, Any]]:
        """Yield a mutable payload under a cross-process transaction lock."""
        with self._session_immediate("mutate_registry") as session:
            statement = select(SqlProfileProtectionRegistry).where(
                SqlProfileProtectionRegistry.workspace_id == current_workspace_id()
            )
            if self._supports_for_update:
                statement = statement.with_for_update()
            row = session.execute(statement).scalar_one_or_none()
            if row is None:
                row = SqlProfileProtectionRegistry(
                    workspace_id=current_workspace_id(),
                    payload=_encode(empty_profile_protection_payload()),
                    updated_at=now_epoch(),
                )
                session.add(row)
            payload = deepcopy(_decode(row.payload))
            yield payload
            encoded = _encode(payload)
            if encoded != row.payload:
                row.payload = encoded
                row.updated_at = now_epoch()

    def seed_if_empty(self, payload: dict[str, Any]) -> None:
        """Import one legacy payload and reject divergent replica files."""
        checksum = legacy_profile_protection_checksum(payload)
        with self.locked() as current:
            current_profiles = current.get("profiles", [])
            current_generation = current.get("generation", 0)
            current_has_state = bool(current_profiles or current_generation)
            if current_has_state:
                established = current.get("legacy_checksum")
                if established is None:
                    established = legacy_profile_protection_checksum(current)
                if established != checksum:
                    raise OmnigentError(
                        "Local private-profile settings disagree with the shared registry.",
                        code=ErrorCode.CONFLICT,
                    )
                return
            current.clear()
            current.update(deepcopy(payload))
            current.setdefault("leases", [])
            current.setdefault("unlock_attempts", [])
            current.setdefault("membership_writes", [])
            current.setdefault("pending_profiles", [])
            current["legacy_checksum"] = checksum
