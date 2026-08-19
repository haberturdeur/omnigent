"""Migration coverage for owner/profile-scoped project-name uniqueness."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from omnigent.db.utils import (
    _build_alembic_config,
    clear_engine_cache,
    get_or_create_engine,
)

_BEFORE_INVARIANT = "pd1e2f3a4b5c"


@pytest.fixture
def db_engine(tmp_path: Path) -> Engine:
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'projects.db'}")
    try:
        yield engine
    finally:
        engine.dispose()
        clear_engine_cache()


def _migrate(engine: Engine, revision: str) -> None:
    config = _build_alembic_config(str(engine.url))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        if revision == _BEFORE_INVARIANT:
            command.downgrade(config, revision)
        else:
            command.upgrade(config, revision)


def _project_values(
    project_id: str,
    name: str,
    *,
    user_id: str | None,
    profile_id: str | None,
    created_at: int,
) -> dict[str, object]:
    return {
        "workspace_id": 0,
        "id": bytes.fromhex(project_id),
        "name": name,
        "user_id": user_id,
        "profile_id": bytes.fromhex(profile_id) if profile_id else None,
        "created_at": created_at,
    }


def _invariant_values(
    project_id: str,
    name: str,
    *,
    user_id: str | None,
    profile_id: str | None,
    created_at: int,
) -> dict[str, object]:
    return {
        **_project_values(
            project_id,
            name,
            user_id=user_id,
            profile_id=profile_id,
            created_at=created_at,
        ),
        "owner_is_anonymous": user_id is None,
        "owner_scope": user_id or "",
        "profile_unassigned_slot": 1 if profile_id is None else None,
    }


def test_upgrade_reconciles_duplicates_without_losing_project_membership(
    db_engine: Engine,
) -> None:
    _migrate(db_engine, _BEFORE_INVARIANT)
    projects = sa.Table("projects", sa.MetaData(), autoload_with=db_engine)
    metadata = sa.Table(
        "omnigent_conversation_metadata",
        sa.MetaData(),
        autoload_with=db_engine,
    )
    profile_a = "aa" * 16
    profile_b = "bb" * 16
    duplicate_id = "20" * 16
    colliding_generated_name = f"Alpha (duplicate {duplicate_id})"
    rows = [
        _project_values("10" * 16, "Alpha", user_id=None, profile_id=profile_a, created_at=100),
        _project_values(duplicate_id, "Alpha", user_id=None, profile_id=profile_a, created_at=200),
        _project_values(
            "30" * 16,
            colliding_generated_name,
            user_id=None,
            profile_id=profile_a,
            created_at=50,
        ),
        _project_values("40" * 16, "Alpha", user_id=None, profile_id=profile_b, created_at=100),
        _project_values(
            "50" * 16,
            "Alpha",
            user_id="alice@example.com",
            profile_id=None,
            created_at=100,
        ),
        _project_values(
            "60" * 16,
            "Alpha",
            user_id="alice@example.com",
            profile_id=None,
            created_at=200,
        ),
    ]
    with db_engine.begin() as connection:
        connection.execute(projects.insert(), rows)
        connection.execute(
            metadata.insert().values(
                workspace_id=0,
                id=bytes.fromhex("90" * 16),
                project_id=bytes.fromhex(duplicate_id),
            )
        )

    _migrate(db_engine, "head")

    migrated_projects = sa.Table("projects", sa.MetaData(), autoload_with=db_engine)
    migrated_metadata = sa.Table(
        "omnigent_conversation_metadata",
        sa.MetaData(),
        autoload_with=db_engine,
    )
    with db_engine.connect() as connection:
        names = {
            bytes(project_id).hex(): name
            for project_id, name in connection.execute(
                sa.select(migrated_projects.c.id, migrated_projects.c.name)
            )
        }
        member_project_id = connection.execute(
            sa.select(migrated_metadata.c.project_id).where(
                migrated_metadata.c.id == bytes.fromhex("90" * 16)
            )
        ).scalar_one()

    assert names["10" * 16] == "Alpha"
    assert names[duplicate_id] == f"Alpha (duplicate {duplicate_id}-2)"
    assert names["30" * 16] == colliding_generated_name
    assert names["40" * 16] == "Alpha"
    assert names["50" * 16] == "Alpha"
    assert names["60" * 16] == f"Alpha (duplicate {'60' * 16})"
    assert bytes(member_project_id).hex() == duplicate_id
    assert len(names) == len(rows)


@pytest.mark.parametrize(
    ("user_id", "profile_id"),
    [
        (None, "aa" * 16),
        (None, None),
        ("alice@example.com", "aa" * 16),
        ("alice@example.com", None),
    ],
)
def test_upgrade_enforces_uniqueness_for_each_nullable_scope(
    db_engine: Engine,
    user_id: str | None,
    profile_id: str | None,
) -> None:
    projects = sa.Table("projects", sa.MetaData(), autoload_with=db_engine)
    with db_engine.begin() as connection:
        connection.execute(
            projects.insert().values(
                **_invariant_values(
                    "70" * 16,
                    "Unique",
                    user_id=user_id,
                    profile_id=profile_id,
                    created_at=100,
                )
            )
        )

    with pytest.raises(IntegrityError), db_engine.begin() as connection:
        connection.execute(
            projects.insert().values(
                **_invariant_values(
                    "80" * 16,
                    "Unique",
                    user_id=user_id,
                    profile_id=profile_id,
                    created_at=200,
                )
            )
        )


def test_upgrade_allows_same_name_in_a_different_scope(db_engine: Engine) -> None:
    projects = sa.Table("projects", sa.MetaData(), autoload_with=db_engine)
    with db_engine.begin() as connection:
        connection.execute(
            projects.insert(),
            [
                _invariant_values(
                    "91" * 16,
                    "Shared",
                    user_id=None,
                    profile_id=None,
                    created_at=100,
                ),
                _invariant_values(
                    "92" * 16,
                    "Shared",
                    user_id=None,
                    profile_id="aa" * 16,
                    created_at=100,
                ),
                _invariant_values(
                    "93" * 16,
                    "Shared",
                    user_id="alice@example.com",
                    profile_id=None,
                    created_at=100,
                ),
            ],
        )


def test_constraints_reject_denormalized_scope_columns(db_engine: Engine) -> None:
    projects = sa.Table("projects", sa.MetaData(), autoload_with=db_engine)
    values = _invariant_values(
        "94" * 16,
        "Invalid",
        user_id=None,
        profile_id=None,
        created_at=100,
    )
    values["owner_scope"] = "someone@example.com"
    with pytest.raises(IntegrityError), db_engine.begin() as connection:
        connection.execute(projects.insert().values(**values))

    values = _invariant_values(
        "95" * 16,
        "Invalid",
        user_id=None,
        profile_id="aa" * 16,
        created_at=100,
    )
    values["profile_unassigned_slot"] = 1
    with pytest.raises(IntegrityError), db_engine.begin() as connection:
        connection.execute(projects.insert().values(**values))


def test_downgrade_removes_invariant_columns_and_constraints(db_engine: Engine) -> None:
    projects = sa.Table("projects", sa.MetaData(), autoload_with=db_engine)
    with db_engine.begin() as connection:
        connection.execute(
            projects.insert().values(
                **_invariant_values(
                    "a0" * 16,
                    "Alpha",
                    user_id=None,
                    profile_id=None,
                    created_at=100,
                )
            )
        )

    _migrate(db_engine, _BEFORE_INVARIANT)

    inspector = sa.inspect(db_engine)
    assert {column["name"] for column in inspector.get_columns("projects")}.isdisjoint(
        {"owner_is_anonymous", "owner_scope", "profile_unassigned_slot"}
    )
    constraint_names = {
        constraint["name"] for constraint in inspector.get_unique_constraints("projects")
    }
    assert "uq_projects_workspace_owner_profile_name" not in constraint_names
    assert "uq_projects_workspace_owner_unprofiled_name" not in constraint_names

    downgraded = sa.Table("projects", sa.MetaData(), autoload_with=db_engine)
    with db_engine.begin() as connection:
        connection.execute(
            downgraded.insert().values(
                **_project_values(
                    "b0" * 16,
                    "Alpha",
                    user_id=None,
                    profile_id=None,
                    created_at=200,
                )
            )
        )

    _migrate(db_engine, "head")
    upgraded = sa.Table("projects", sa.MetaData(), autoload_with=db_engine)
    with db_engine.connect() as connection:
        rows = connection.execute(
            sa.select(upgraded.c.name, upgraded.c.owner_scope).order_by(upgraded.c.created_at)
        ).all()
    assert rows == [("Alpha", ""), (f"Alpha (duplicate {'b0' * 16})", "")]
