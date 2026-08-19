"""Persistence interface for owner-private profiles."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from omnigent.entities import Profile


class ProfileStore(ABC):
    """Manage profile lifecycle and the per-owner default profile."""

    def __init__(self, storage_location: str) -> None:
        self.storage_location = storage_location

    @abstractmethod
    def ensure_default(self, *, user_id: str | None) -> Profile: ...

    @abstractmethod
    def create(
        self,
        profile_id: str,
        name: str,
        user_id: str | None,
        *,
        icon: str | None = None,
        color: str | None = None,
        config: dict[str, Any] | None = None,
        protection: dict[str, Any] | None = None,
    ) -> Profile: ...

    @abstractmethod
    def get(self, profile_id: str, *, user_id: str | None) -> Profile | None: ...

    @abstractmethod
    def list(self, *, user_id: str | None) -> list[Profile]: ...

    @abstractmethod
    def get_protection(self, profile_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def list_protected_profile_ids(self) -> frozenset[str]: ...

    @abstractmethod
    def update(
        self,
        profile_id: str,
        *,
        user_id: str | None,
        name: str | None = None,
        icon: str | None = None,
        color: str | None = None,
        config: dict[str, Any] | None = None,
        protection: dict[str, Any] | None = None,
    ) -> Profile | None: ...

    @abstractmethod
    def delete(self, profile_id: str, *, user_id: str | None) -> bool: ...

    def restore(self, profile: Profile) -> None:
        """Restore a deleted snapshot when supported by the backend."""
        raise NotImplementedError
