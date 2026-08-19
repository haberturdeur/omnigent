"""Enforce one default profile per workspace owner.

Revision ID: pd1e2f3a4b5c
Revises: pc1d2e3f4a5b
Create Date: 2026-08-20 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "pd1e2f3a4b5c"
down_revision: str | None = "pc1d2e3f4a5b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("profiles") as batch_op:
        batch_op.add_column(sa.Column("owner_is_anonymous", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("owner_scope", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("default_slot", sa.SmallInteger(), nullable=True))

    bind = op.get_bind()
    profiles = sa.table(
        "profiles",
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("id", Uuid16()),
        sa.column("user_id", sa.String(128)),
        sa.column("owner_is_anonymous", sa.Boolean()),
        sa.column("owner_scope", sa.String(128)),
        sa.column("is_default", sa.Boolean()),
        sa.column("default_slot", sa.SmallInteger()),
        sa.column("created_at", sa.Integer()),
    )
    bind.execute(
        profiles.update().values(
            owner_is_anonymous=profiles.c.user_id.is_(None),
            owner_scope=sa.func.coalesce(profiles.c.user_id, ""),
            default_slot=sa.case((profiles.c.is_default.is_(True), 1), else_=None),
        )
    )

    rows = bind.execute(
        sa.select(
            profiles.c.workspace_id,
            profiles.c.id,
            profiles.c.user_id,
            profiles.c.is_default,
        ).order_by(
            profiles.c.workspace_id,
            profiles.c.owner_scope,
            profiles.c.created_at,
            profiles.c.id,
        )
    ).all()
    owner_rows: dict[tuple[int, str | None], list[tuple[object, bool]]] = {}
    for workspace_id, profile_id, user_id, is_default in rows:
        owner = (workspace_id, user_id)
        owner_rows.setdefault(owner, []).append((profile_id, bool(is_default)))

    for (workspace_id, _user_id), candidates in owner_rows.items():
        existing_defaults = [profile_id for profile_id, is_default in candidates if is_default]
        canonical_id = existing_defaults[0] if existing_defaults else candidates[0][0]
        for profile_id, _is_default in candidates:
            canonical = profile_id == canonical_id
            bind.execute(
                profiles.update()
                .where(
                    profiles.c.workspace_id == workspace_id,
                    profiles.c.id == profile_id,
                )
                .values(is_default=canonical, default_slot=1 if canonical else None)
            )

    sqlite = bind.dialect.name == "sqlite"
    with op.batch_alter_table("profiles", recreate="always" if sqlite else "auto") as batch_op:
        batch_op.alter_column("owner_is_anonymous", existing_type=sa.Boolean(), nullable=False)
        batch_op.alter_column("owner_scope", existing_type=sa.String(128), nullable=False)
        batch_op.create_check_constraint(
            "ck_profiles_owner_scope",
            "(user_id IS NULL AND owner_is_anonymous = true AND owner_scope = '') OR "
            "(user_id IS NOT NULL AND owner_is_anonymous = false AND owner_scope = user_id)",
        )
        batch_op.create_check_constraint(
            "ck_profiles_default_slot",
            "(is_default = true AND default_slot = 1) OR "
            "(is_default = false AND default_slot IS NULL)",
        )
        batch_op.create_unique_constraint(
            "uq_profiles_workspace_owner_default",
            ["workspace_id", "owner_is_anonymous", "owner_scope", "default_slot"],
        )


def downgrade() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    with op.batch_alter_table("profiles", recreate="always" if sqlite else "auto") as batch_op:
        batch_op.drop_constraint("uq_profiles_workspace_owner_default", type_="unique")
        batch_op.drop_constraint("ck_profiles_default_slot", type_="check")
        batch_op.drop_constraint("ck_profiles_owner_scope", type_="check")
        batch_op.drop_column("default_slot")
        batch_op.drop_column("owner_scope")
        batch_op.drop_column("owner_is_anonymous")
