"""Project store — persists first-class, owner-private projects.

A project is a user-defined container that groups sessions and exists
independently of its members (see ``designs/PROJECTS_PRD.md``). This store owns
the ``projects`` table. Session→project membership lives on the conversation's
metadata row (``project_id``) and is managed by the conversation store, not
here.

Projects have no ACL of their own (PRD §9): every method is scoped by
``user_id`` so a caller only ever sees and mutates their own projects.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from threading import RLock
from typing import Any

from omnigent.entities import Project


class ProjectProfileChangedError(RuntimeError):
    """The project changed profiles while an operation was being prepared."""


class ProjectDestinationProfileChangedError(RuntimeError):
    """The destination profile disappeared before the project move committed."""


class ProjectSessionProfileMismatchError(RuntimeError):
    """A project member and its project belong to different profiles."""


class ProjectWorkspaceRelocationError(RuntimeError):
    """A project member workspace cannot be relocated with its project root."""


class LegacyProjectMembershipChangedError(RuntimeError):
    """Legacy label membership changed while a project move was prepared."""


class ProjectMembershipChangedError(RuntimeError):
    """The project's session membership changed during a move."""


_legacy_project_membership_lock = RLock()


@contextmanager
def legacy_project_membership_guard() -> Iterator[None]:
    """Serialize legacy project-label writes with project adoption."""
    with _legacy_project_membership_lock:
        yield


class ProjectStore(ABC):
    """
    Abstract base for project persistence.

    Manages the lifecycle of projects (CRUD). All reads and writes are scoped
    by ``user_id`` because projects are owner-private.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the project store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create(
        self,
        project_id: str,
        name: str,
        user_id: str | None,
        config: dict[str, Any] | None = None,
        profile_id: str | None = None,
    ) -> Project:
        """
        Insert a new, empty project.

        :param project_id: Pre-generated unique project id (a UUID string).
        :param name: Human-readable project name. Trimmed, non-empty, unique
            among the owner's projects in this profile.
        :param user_id: Owning user, or ``None`` in single-user mode.
        :param config: Optional default session settings (opaque JSON object);
            ``None`` or empty stores no defaults.
        :returns: The newly created :class:`Project`.
        :raises OmnigentError: ``ALREADY_EXISTS`` if the owner already has a
            project with this name in the same profile.
        """
        ...

    @abstractmethod
    def get(self, project_id: str, *, user_id: str | None) -> Project | None:
        """
        Return an owned project by id, or ``None`` if not found.

        :param project_id: Opaque project identifier.
        :param user_id: The requesting owner; a project owned by someone
            else is treated as not found.
        :returns: The :class:`Project` if found and owned, else ``None``.
        """
        ...

    @abstractmethod
    def list(self, *, user_id: str | None, profile_id: str | None = None) -> list[Project]:
        """
        List the owner's projects ordered by ``created_at ASC, id ASC``.

        :param user_id: The owner whose projects to return.
        :returns: List of :class:`Project` instances.
        """
        ...

    @abstractmethod
    def update(
        self,
        project_id: str,
        *,
        user_id: str | None,
        name: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> Project | None:
        """
        Update mutable fields of an owned project.

        ``None`` leaves a field unchanged. Returns ``None`` if the project does
        not exist or is not owned by ``user_id``.

        :param project_id: Opaque project identifier.
        :param user_id: The requesting owner.
        :param name: New name, or ``None`` to leave unchanged. Trimmed,
            non-empty, unique among the owner's projects in this profile.
        :param config: New config object to replace the stored one, or ``None``
            to leave it unchanged. An empty dict clears the stored defaults.
        :returns: The updated :class:`Project`, or ``None`` if not found.
        :raises OmnigentError: ``ALREADY_EXISTS`` if the new name collides with
            another project in the same owner/profile scope.
        """
        ...

    @abstractmethod
    def update_with_sessions(
        self,
        project_id: str,
        *,
        user_id: str | None,
        expected_profile_id: str | None,
        destination_profile_id: str | None,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        resolve_legacy_session_ids: (
            Callable[[str, str | None, str], Iterable[str]] | None
        ) = None,
        hold_legacy_membership_lock: (Callable[[], AbstractContextManager[None]] | None) = None,
        legacy_project_name: str | None = None,
        legacy_labels_are_colocated: bool = False,
        clear_legacy_labels: Callable[[tuple[str, ...]], None] | None = None,
        validate_workspace: Callable[[str, str | None], None] | None = None,
        workspace_relocation: tuple[str, str] | None = None,
        expected_member_ids: tuple[str, ...] | None = None,
    ) -> Project | None:
        """Atomically update a project and move all of its member sessions."""
        ...

    @abstractmethod
    def file_session(
        self,
        project_id: str,
        session_id: str,
        *,
        user_id: str | None,
        clear_legacy_label: bool = False,
        legacy_label_is_colocated: bool = False,
        clear_legacy_label_fallback: Callable[[], None] | None = None,
    ) -> bool:
        """Atomically file a same-profile session into an owned project."""
        ...

    @abstractmethod
    def unfile_session(
        self,
        session_id: str,
        *,
        clear_legacy_label: bool = False,
        legacy_label_is_colocated: bool = False,
        clear_legacy_label_fallback: Callable[[], None] | None = None,
    ) -> bool:
        """Atomically unfile a session and optionally clear its legacy label."""
        ...

    @abstractmethod
    def delete(self, project_id: str, *, user_id: str | None) -> bool:
        """
        Delete an owned project. Idempotent.

        Deleting a project does not delete its member sessions; unfiling them
        (clearing ``project_id``) is the caller's responsibility.

        :param project_id: Opaque project identifier.
        :param user_id: The requesting owner.
        :returns: ``True`` if removed; ``False`` if not found / not owned.
        """
        ...
