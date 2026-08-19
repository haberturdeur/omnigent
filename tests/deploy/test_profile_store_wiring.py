"""Deployment entry points expose profile-backed routes."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "relative_path",
    [
        "deploy/docker/entrypoint.py",
        "deploy/databricks/src/app.py",
        "scripts/dump_openapi.py",
    ],
)
def test_app_construction_wires_sqlalchemy_profile_store(relative_path: str) -> None:
    """Every production/spec app passes a SQL profile store to create_app."""
    tree = ast.parse((_REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    constructors = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "SqlAlchemyProfileStore"
    ]
    create_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "create_app"
    ]

    assert constructors, f"{relative_path} does not construct SqlAlchemyProfileStore"
    assert any(
        any(keyword.arg == "profile_store" for keyword in call.keywords) for call in create_calls
    ), f"{relative_path} does not pass profile_store to create_app"
