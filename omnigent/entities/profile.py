"""User profile entity."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Profile:
    """A switchable owner-private context for projects and sessions."""

    id: str
    name: str
    user_id: str | None
    created_at: int
    updated_at: int | None = None
    icon: str | None = None
    color: str | None = None
    is_default: bool = False
    config: dict[str, Any] = field(default_factory=dict)
    protection: dict[str, Any] = field(default_factory=dict)
