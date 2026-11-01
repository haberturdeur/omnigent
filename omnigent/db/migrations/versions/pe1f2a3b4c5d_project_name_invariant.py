"""Enforce project-name uniqueness within an owner profile.

Revision ID: pe1f2a3b4c5d
Revises: pd1e2f3a4b5c
Create Date: 2026-08-20 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "pe1f2a3b4c5d"
down_revision: str | None = "pd1e2f3a4b5c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MAX_PROJECT_NAME_LENGTH = 256


def _id_hex(value: object) -> str:
    if isinstance(value, str):
        return value.replace("-", "").lower()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    raise TypeError(f"unsupported project id value: {type(value).__name__}")


def _reconciled_name(original: str, project_id: object, used_names: set[str]) -> str:
    id_hex = _id_hex(project_id)
    attempt = 1
    while True:
        discriminator = id_hex if attempt == 1 else f"{id_hex}-{attempt}"
        suffix = f" (duplicate {discriminator})"
        candidate = f"{original[: _MAX_PROJECT_NAME_LENGTH - len(suffix)]}{suffix}"
        if candidate not in used_names:
            return candidate
        attempt += 1


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("owner_is_anonymous", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("owner_scope", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("profile_unassigned_slot", sa.SmallInteger(), nullable=True))

    bind = op.get_bind()
    projects = sa.table(
        "projects",
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("id", Uuid16()),
        sa.column("name", sa.String(256)),
        sa.column("user_id", sa.String(128)),
        sa.column("owner_is_anonymous", sa.Boolean()),
        sa.column("owner_scope", sa.String(128)),
        sa.column("profile_id", Uuid16()),
        sa.column("profile_unassigned_slot", sa.SmallInteger()),
        sa.column("created_at", sa.Integer()),
    )
    bind.execute(
        projects.update().values(
            owner_is_anonymous=projects.c.user_id.is_(None),
            owner_scope=sa.func.coalesce(projects.c.user_id, ""),
            profile_unassigned_slot=sa.case(
                (projects.c.profile_id.is_(None), 1),
                else_=None,
            ),
        )
    )

    rows = bind.execute(
        sa.select(
            projects.c.workspace_id,
            projects.c.id,
            projects.c.name,
            projects.c.user_id,
            projects.c.profile_id,
            projects.c.created_at,
        ).order_by(projects.c.created_at, projects.c.id)
    ).all()
    used_by_scope: dict[tuple[object, object, object], set[str]] = {}
    for workspace_id, _project_id, name, user_id, profile_id, _created_at in rows:
        used_by_scope.setdefault((workspace_id, user_id, profile_id), set()).add(name)

    seen: set[tuple[object, object, object, str]] = set()
    for workspace_id, project_id, name, user_id, profile_id, _created_at in rows:
        scope = (workspace_id, user_id, profile_id)
        name_key = (*scope, name)
        if name_key not in seen:
            seen.add(name_key)
            continue
        reconciled = _reconciled_name(name, project_id, used_by_scope[scope])
        bind.execute(
            projects.update()
            .where(
                projects.c.workspace_id == workspace_id,
                projects.c.id == project_id,
            )
            .values(name=reconciled)
        )
        used_by_scope[scope].add(reconciled)
        seen.add((*scope, reconciled))

    sqlite = bind.dialect.name == "sqlite"
    with op.batch_alter_table("projects", recreate="always" if sqlite else "auto") as batch_op:
        batch_op.alter_column("owner_is_anonymous", existing_type=sa.Boolean(), nullable=False)
        batch_op.alter_column("owner_scope", existing_type=sa.String(128), nullable=False)
        batch_op.create_check_constraint(
            "ck_projects_owner_scope",
            "(user_id IS NULL AND owner_is_anonymous = true AND owner_scope = '') OR "
            "(user_id IS NOT NULL AND owner_is_anonymous = false AND owner_scope = user_id)",
        )
        batch_op.create_check_constraint(
            "ck_projects_profile_slot",
            "(profile_id IS NULL AND profile_unassigned_slot = 1) OR "
            "(profile_id IS NOT NULL AND profile_unassigned_slot IS NULL)",
        )
        batch_op.create_unique_constraint(
            "uq_projects_workspace_owner_profile_name",
            ["workspace_id", "owner_is_anonymous", "owner_scope", "profile_id", "name"],
        )
        batch_op.create_unique_constraint(
            "uq_projects_workspace_owner_unprofiled_name",
            [
                "workspace_id",
                "owner_is_anonymous",
                "owner_scope",
                "profile_unassigned_slot",
                "name",
            ],
        )


def downgrade() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    with op.batch_alter_table("projects", recreate="always" if sqlite else "auto") as batch_op:
        batch_op.drop_constraint(
            "uq_projects_workspace_owner_unprofiled_name",
            type_="unique",
        )
        batch_op.drop_constraint("uq_projects_workspace_owner_profile_name", type_="unique")
        batch_op.drop_constraint("ck_projects_profile_slot", type_="check")
        batch_op.drop_constraint("ck_projects_owner_scope", type_="check")
        batch_op.drop_column("profile_unassigned_slot")
        batch_op.drop_column("owner_scope")
        batch_op.drop_column("owner_is_anonymous")
