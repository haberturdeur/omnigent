"""Enforce profile-name uniqueness within an owner scope.

Revision ID: pg1b2c3d4e5f
Revises: pf1a2b3c4d5e
Create Date: 2026-08-20 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "pg1b2c3d4e5f"
down_revision: str | None = "pf1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MAX_PROFILE_NAME_LENGTH = 100


def _id_hex(value: object) -> str:
    if isinstance(value, str):
        return value.replace("-", "").lower()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    raise TypeError(f"unsupported profile id value: {type(value).__name__}")


def _reconciled_name(original: str, profile_id: object, used_names: set[str]) -> str:
    profile_id_hex = _id_hex(profile_id)
    attempt = 1
    while True:
        discriminator = profile_id_hex if attempt == 1 else f"{profile_id_hex}-{attempt}"
        suffix = f" (duplicate {discriminator})"
        candidate = f"{original[: _MAX_PROFILE_NAME_LENGTH - len(suffix)]}{suffix}"
        if candidate not in used_names:
            return candidate
        attempt += 1


def upgrade() -> None:
    bind = op.get_bind()
    profiles = sa.table(
        "profiles",
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("id", Uuid16()),
        sa.column("name", sa.String(100)),
        sa.column("owner_is_anonymous", sa.Boolean()),
        sa.column("owner_scope", sa.String(128)),
        sa.column("created_at", sa.Integer()),
    )
    rows = bind.execute(
        sa.select(
            profiles.c.workspace_id,
            profiles.c.id,
            profiles.c.name,
            profiles.c.owner_is_anonymous,
            profiles.c.owner_scope,
            profiles.c.created_at,
        ).order_by(profiles.c.created_at, profiles.c.id)
    ).all()
    used_by_scope: dict[tuple[object, object, object], set[str]] = {}
    for workspace_id, _profile_id, name, anonymous, owner_scope, _created_at in rows:
        used_by_scope.setdefault((workspace_id, anonymous, owner_scope), set()).add(name)

    seen: set[tuple[object, object, object, str]] = set()
    for workspace_id, profile_id, name, anonymous, owner_scope, _created_at in rows:
        scope = (workspace_id, anonymous, owner_scope)
        name_key = (*scope, name)
        if name_key not in seen:
            seen.add(name_key)
            continue
        reconciled = _reconciled_name(name, profile_id, used_by_scope[scope])
        bind.execute(
            profiles.update()
            .where(
                profiles.c.workspace_id == workspace_id,
                profiles.c.id == profile_id,
            )
            .values(name=reconciled)
        )
        used_by_scope[scope].add(reconciled)
        seen.add((*scope, reconciled))

    sqlite = bind.dialect.name == "sqlite"
    with op.batch_alter_table("profiles", recreate="always" if sqlite else "auto") as batch_op:
        batch_op.create_unique_constraint(
            "uq_profiles_workspace_owner_name",
            ["workspace_id", "owner_is_anonymous", "owner_scope", "name"],
        )


def downgrade() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    with op.batch_alter_table("profiles", recreate="always" if sqlite else "auto") as batch_op:
        batch_op.drop_constraint("uq_profiles_workspace_owner_name", type_="unique")
