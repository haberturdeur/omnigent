"""Database locking for legacy project-label membership."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

_ADVISORY_LOCK_ID = 0x4F4D4E49


def acquire_legacy_membership_db_lock(session: Session) -> None:
    """Serialize legacy membership changes across PostgreSQL processes."""
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _ADVISORY_LOCK_ID},
        )
