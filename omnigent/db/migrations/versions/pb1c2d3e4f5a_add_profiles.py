"""Add switchable profiles and profile membership.

Revision ID: pb1c2d3e4f5a
Revises: pa1b2c3d4e5f
Create Date: 2026-08-19 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "pb1c2d3e4f5a"
down_revision: str | None = "pa1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "profiles",
        sa.Column("workspace_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("icon", sa.String(64), nullable=True),
        sa.Column("color", sa.String(32), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        # CompressedText stores bytes (BYTEA/BLOB), not database text.
        sa.Column("config", sa.LargeBinary(), nullable=True),
        sa.Column("protection", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_profiles_user_id", "profiles", ["workspace_id", "user_id", "created_at", "id"]
    )
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("profile_id", Uuid16(), nullable=True))
    op.drop_index("ix_projects_user_id", table_name="projects")
    op.create_index(
        "ix_projects_user_id",
        "projects",
        ["workspace_id", "user_id", "profile_id", "created_at", "id"],
    )
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.add_column(sa.Column("profile_id", Uuid16(), nullable=True))
    op.create_index(
        "ix_conversation_metadata_profile_id",
        "omnigent_conversation_metadata",
        ["workspace_id", "profile_id", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_metadata_profile_id",
        table_name="omnigent_conversation_metadata",
    )
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("profile_id")
    op.drop_index("ix_projects_user_id", table_name="projects")
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("profile_id")
    op.create_index(
        "ix_projects_user_id",
        "projects",
        ["workspace_id", "user_id", "created_at", "id"],
    )
    op.drop_index("ix_profiles_user_id", table_name="profiles")
    op.drop_table("profiles")
