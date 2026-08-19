"""Migration coverage for owner-scoped profile-name uniqueness."""

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

_BEFORE_INVARIANT = "pf1a2b3c4d5e"


@pytest.fixture
def db_engine(tmp_path: Path) -> Engine:
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'profile-names.db'}")
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


def _profile_values(
    profile_id: str,
    name: str,
    *,
    user_id: str | None,
    created_at: int,
    is_default: bool = False,
) -> dict[str, object]:
    return {
        "workspace_id": 0,
        "id": bytes.fromhex(profile_id),
        "name": name,
        "user_id": user_id,
        "owner_is_anonymous": user_id is None,
        "owner_scope": user_id or "",
        "is_default": is_default,
        "default_slot": 1 if is_default else None,
        "created_at": created_at,
    }


def test_upgrade_reconciles_duplicate_names_deterministically_without_data_loss(
    db_engine: Engine,
) -> None:
    _migrate(db_engine, _BEFORE_INVARIANT)
    profiles = sa.Table("profiles", sa.MetaData(), autoload_with=db_engine)
    duplicate_id = "20" * 16
    generated_collision = f"Work (duplicate {duplicate_id})"
    rows = [
        _profile_values("10" * 16, "Work", user_id=None, created_at=100, is_default=True),
        _profile_values(duplicate_id, "Work", user_id=None, created_at=200),
        _profile_values("30" * 16, generated_collision, user_id=None, created_at=50),
        _profile_values("40" * 16, "Work", user_id="alice@example.com", created_at=100),
    ]
    with db_engine.begin() as connection:
        connection.execute(profiles.insert(), rows)

    _migrate(db_engine, "head")

    migrated = sa.Table("profiles", sa.MetaData(), autoload_with=db_engine)
    with db_engine.connect() as connection:
        names = {
            bytes(profile_id).hex(): name
            for profile_id, name in connection.execute(sa.select(migrated.c.id, migrated.c.name))
        }

    assert names == {
        "10" * 16: "Work",
        duplicate_id: f"Work (duplicate {duplicate_id}-2)",
        "30" * 16: generated_collision,
        "40" * 16: "Work",
    }


@pytest.mark.parametrize("user_id", [None, "alice@example.com"])
def test_upgrade_enforces_name_uniqueness_for_each_owner(
    db_engine: Engine,
    user_id: str | None,
) -> None:
    _migrate(db_engine, _BEFORE_INVARIANT)
    _migrate(db_engine, "head")
    profiles = sa.Table("profiles", sa.MetaData(), autoload_with=db_engine)
    with db_engine.begin() as connection:
        connection.execute(
            profiles.insert().values(
                **_profile_values("50" * 16, "Unique", user_id=user_id, created_at=100)
            )
        )

    with pytest.raises(IntegrityError), db_engine.begin() as connection:
        connection.execute(
            profiles.insert().values(
                **_profile_values("60" * 16, "Unique", user_id=user_id, created_at=200)
            )
        )


def test_downgrade_removes_only_profile_name_constraint(db_engine: Engine) -> None:
    inspector = sa.inspect(db_engine)
    constraints = {
        constraint["name"] for constraint in inspector.get_unique_constraints("profiles")
    }
    assert "uq_profiles_workspace_owner_name" in constraints

    _migrate(db_engine, _BEFORE_INVARIANT)

    constraints = {
        constraint["name"]
        for constraint in sa.inspect(db_engine).get_unique_constraints("profiles")
    }
    assert "uq_profiles_workspace_owner_name" not in constraints
    assert "uq_profiles_workspace_owner_default" in constraints
