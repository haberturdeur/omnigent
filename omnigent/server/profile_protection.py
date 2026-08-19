"""Private-profile locks and filesystem isolation registry."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no multi-process flock.
    fcntl = None  # type: ignore[assignment]

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.admin_list import resolve_data_dir
from omnigent.server.passwords import InvalidPasswordError, hash_password, verify_password
from omnigent.stores.profile_protection_registry import ProfileProtectionRegistryStore

PROFILE_UNLOCK_HEADER = "X-Omnigent-Profile-Unlock"
_REGISTRY_FILE = "profile_protection.json"
_RUNNER_LEASE_FILE = "profile_runner_generation_leases.json"
_REGISTRY_PATH_ENV = "OMNIGENT_PROFILE_PROTECTION_PATH"
_UNLOCK_TTL_SECONDS = 12 * 60 * 60
_MAX_ACTIVE_UNLOCK_TOKENS = 8
_UNLOCK_ATTEMPT_LIMIT = 10
_UNLOCK_ATTEMPT_WINDOW_SECONDS = 60.0
RUNNER_GENERATION_LEASE_TTL_SECONDS = 10.0
_lock = threading.RLock()
_unlock_attempt_lock = threading.Lock()
_unlock_attempts: dict[tuple[str, str | None, str], deque[float]] = {}
_file_membership_writes: dict[str, tuple[str, ...]] = {}
_file_pending_profiles: set[str] = set()
_database_registry: ProfileProtectionRegistryStore | None = None
_declared_protected_profile_ids: Callable[[], frozenset[str]] | None = None
_active_registry_payload: ContextVar[dict[str, Any] | None] = ContextVar(
    "active_profile_protection_registry",
    default=None,
)


def _record_unlock_attempt(profile_id: str, user_id: str | None) -> None:
    """Bound expensive passcode verification per registry/profile owner."""
    if _uses_database_registry():
        now = time.time()
        cutoff = now - _UNLOCK_ATTEMPT_WINDOW_SECONDS
        key = f"{user_id or ''}\0{profile_id}"
        with _registry_write_lock():
            payload = _registry_payload()
            raw = payload.get("unlock_attempts", [])
            if not isinstance(raw, list):
                raise OmnigentError(
                    "Private-profile unlock throttling state is malformed.",
                    code=ErrorCode.INTERNAL_ERROR,
                )
            retained = [
                item
                for item in raw
                if isinstance(item, dict)
                and isinstance(item.get("key"), str)
                and isinstance(item.get("at"), (int, float))
                and float(item["at"]) > cutoff
            ]
            attempts = [item for item in retained if item["key"] == key]
            if len(attempts) >= _UNLOCK_ATTEMPT_LIMIT:
                raise OmnigentError(
                    "Too many unlock attempts; wait a minute and try again.",
                    code=ErrorCode.RATE_LIMITED,
                )
            retained.append({"key": key, "at": now})
            payload["unlock_attempts"] = retained
        return
    now = time.monotonic()
    key = (str(resolve_profile_protection_path()), user_id, profile_id)
    cutoff = now - _UNLOCK_ATTEMPT_WINDOW_SECONDS
    with _unlock_attempt_lock:
        attempts = _unlock_attempts.setdefault(key, deque())
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if len(attempts) >= _UNLOCK_ATTEMPT_LIMIT:
            raise OmnigentError(
                "Too many unlock attempts; wait a minute and try again.",
                code=ErrorCode.RATE_LIMITED,
            )
        attempts.append(now)


@dataclass(frozen=True)
class ProtectedProfile:
    """One private profile's server-side protection state."""

    profile_id: str
    user_id: str | None
    host_id: str | None
    passcode_hash: str
    protected_roots: tuple[Path, ...]
    unlock_token_hashes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileProtectionChange:
    """Compare-and-swap plan for one protection-registry mutation."""

    before: tuple[ProtectedProfile, ...]
    after: tuple[ProtectedProfile, ...]
    profile_id: str
    user_id: str | None
    changed_roots: tuple[Path, ...]

    @property
    def configured(self) -> ProtectedProfile | None:
        """Return the profile protection produced by this change, if any."""
        return next(
            (item for item in self.after if item.profile_id == self.profile_id),
            None,
        )


@dataclass(frozen=True)
class RunnerGenerationLease:
    """One connected runner's durable protection-generation lease."""

    lease_id: str
    runner_id: str
    generation: int
    expires_at: float


def resolve_profile_protection_path() -> Path:
    """Return the registry path shared by the server and local runners."""
    explicit = os.environ.get(_REGISTRY_PATH_ENV, "").strip()
    return Path(explicit) if explicit else resolve_data_dir() / _REGISTRY_FILE


def _runner_lease_path() -> Path:
    """Return the cross-worker runner-generation lease path."""
    registry = resolve_profile_protection_path()
    return registry.with_name(_RUNNER_LEASE_FILE)


def _file_registry_payload() -> dict[str, Any]:
    """Read the legacy local registry and lease files."""
    path = resolve_profile_protection_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {"generation": 0, "profiles": []}
    except (OSError, ValueError, TypeError) as exc:
        raise OmnigentError(
            "Private-profile protection settings are unreadable.",
            code=ErrorCode.INTERNAL_ERROR,
        ) from exc
    if not isinstance(raw, dict):
        raise OmnigentError(
            "Private-profile protection settings are malformed.",
            code=ErrorCode.INTERNAL_ERROR,
        )
    try:
        leases_raw = json.loads(_runner_lease_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        leases_raw = {"leases": []}
    except (OSError, ValueError, TypeError) as exc:
        raise OmnigentError(
            "Runner protection leases are unreadable.",
            code=ErrorCode.INTERNAL_ERROR,
        ) from exc
    if not isinstance(leases_raw, dict):
        raise OmnigentError(
            "Runner protection leases are malformed.",
            code=ErrorCode.INTERNAL_ERROR,
        )
    return {
        "generation": raw.get("generation", 0),
        "profiles": raw.get("profiles", []),
        "leases": leases_raw.get("leases", []),
    }


def _uses_database_registry() -> bool:
    """Keep explicit registry paths as the isolated test/operator override."""
    return _database_registry is not None and not os.environ.get(_REGISTRY_PATH_ENV, "").strip()


def configure_profile_protection_registry(
    storage_location: str,
    *,
    declared_protected_profile_ids: Callable[[], frozenset[str]] | None = None,
) -> None:
    """Use shared database state for server-side protection coordination."""
    global _database_registry, _declared_protected_profile_ids
    legacy = _file_registry_payload()
    store = ProfileProtectionRegistryStore(storage_location)
    if legacy["profiles"] or legacy["leases"] or legacy["generation"]:
        store.seed_if_empty(legacy)
    _database_registry = store
    _declared_protected_profile_ids = declared_protected_profile_ids


def _registry_payload() -> dict[str, Any]:
    active = _active_registry_payload.get()
    if active is not None:
        return active
    if _uses_database_registry():
        assert _database_registry is not None
        return _database_registry.read()
    return _file_registry_payload()


def _read_protected_profiles() -> tuple[ProtectedProfile, ...]:
    """Read the registry while the caller holds any required write lock."""
    raw = _registry_payload()
    entries = raw.get("profiles") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise OmnigentError(
            "Private-profile protection settings are malformed.",
            code=ErrorCode.INTERNAL_ERROR,
        )
    profiles: list[ProtectedProfile] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise OmnigentError(
                "Private-profile protection settings are malformed.",
                code=ErrorCode.INTERNAL_ERROR,
            )
        profile_id = entry.get("profile_id")
        user_id = entry.get("user_id")
        host_id = entry.get("host_id")
        passcode_hash = entry.get("passcode_hash")
        roots = entry.get("protected_roots")
        unlock_token_hashes = entry.get("unlock_token_hashes", [])
        if (
            not isinstance(profile_id, str)
            or not profile_id
            or (user_id is not None and not isinstance(user_id, str))
            or (host_id is not None and (not isinstance(host_id, str) or not host_id))
            or not isinstance(passcode_hash, str)
            or not isinstance(roots, list)
            or not roots
            or not all(isinstance(root, str) for root in roots)
            or not isinstance(unlock_token_hashes, list)
            or not all(isinstance(value, str) for value in unlock_token_hashes)
        ):
            raise OmnigentError(
                "Private-profile protection settings are malformed.",
                code=ErrorCode.INTERNAL_ERROR,
            )
        profiles.append(
            ProtectedProfile(
                profile_id=profile_id,
                user_id=user_id,
                host_id=host_id,
                passcode_hash=passcode_hash,
                protected_roots=tuple(Path(root) for root in roots),
                unlock_token_hashes=tuple(unlock_token_hashes),
            )
        )
    return tuple(profiles)


def profile_protection_generation() -> int:
    """Return the monotonic generation for isolation-topology changes."""
    raw = _registry_payload()
    generation = raw.get("generation", 0)
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise OmnigentError(
            "Private-profile protection settings are malformed.",
            code=ErrorCode.INTERNAL_ERROR,
        )
    return generation


def begin_profile_membership_write(profile_ids: tuple[str, ...]) -> str | None:
    """Register a cross-replica membership write unless protection is changing."""
    ids = tuple(sorted(set(profile_ids)))
    if not ids:
        return None
    operation_id = secrets.token_urlsafe(18)
    if not _uses_database_registry():
        with _lock:
            if set(ids).intersection(_file_pending_profiles):
                raise OmnigentError(
                    "Profile protection is changing; retry the request.",
                    code=ErrorCode.CONFLICT,
                )
            _file_membership_writes[operation_id] = ids
        return operation_id
    with _registry_write_lock():
        payload = _registry_payload()
        pending = payload.get("pending_profiles", [])
        writes = payload.get("membership_writes", [])
        if not isinstance(pending, list) or not isinstance(writes, list):
            raise OmnigentError(
                "Private-profile lifecycle state is malformed.",
                code=ErrorCode.INTERNAL_ERROR,
            )
        if set(ids).intersection(item for item in pending if isinstance(item, str)):
            raise OmnigentError(
                "Profile protection is changing; retry the request.",
                code=ErrorCode.CONFLICT,
            )
        writes.append({"operation_id": operation_id, "profile_ids": list(ids)})
        payload["membership_writes"] = writes
    return operation_id


def finish_profile_membership_write(operation_id: str | None) -> None:
    """Release a cross-replica membership-write registration."""
    if operation_id is None:
        return
    if not _uses_database_registry():
        with _lock:
            _file_membership_writes.pop(operation_id, None)
        return
    with _registry_write_lock():
        payload = _registry_payload()
        writes = payload.get("membership_writes", [])
        if not isinstance(writes, list):
            raise OmnigentError(
                "Private-profile lifecycle state is malformed.",
                code=ErrorCode.INTERNAL_ERROR,
            )
        payload["membership_writes"] = [
            item
            for item in writes
            if not isinstance(item, dict) or item.get("operation_id") != operation_id
        ]


@contextlib.contextmanager
def profile_membership_write(profile_ids: tuple[str, ...]):
    """Guard one membership mutation against protection transitions."""
    operation_id = begin_profile_membership_write(profile_ids)
    try:
        yield
    finally:
        finish_profile_membership_write(operation_id)


def begin_profile_protection_transition(profile_id: str) -> None:
    """Block new profile membership writes across every server replica."""
    if not _uses_database_registry():
        with _lock:
            if profile_id in _file_pending_profiles:
                raise OmnigentError(
                    "Profile protection is already changing; retry the request.",
                    code=ErrorCode.CONFLICT,
                )
            if any(profile_id in ids for ids in _file_membership_writes.values()):
                raise OmnigentError(
                    "Profile membership is changing; retry the request.",
                    code=ErrorCode.CONFLICT,
                )
            _file_pending_profiles.add(profile_id)
        return
    with _registry_write_lock():
        payload = _registry_payload()
        pending = payload.get("pending_profiles", [])
        writes = payload.get("membership_writes", [])
        if not isinstance(pending, list) or not isinstance(writes, list):
            raise OmnigentError(
                "Private-profile lifecycle state is malformed.",
                code=ErrorCode.INTERNAL_ERROR,
            )
        if profile_id in pending:
            raise OmnigentError(
                "Profile protection is already changing; retry the request.",
                code=ErrorCode.CONFLICT,
            )
        active = any(
            isinstance(item, dict) and profile_id in item.get("profile_ids", []) for item in writes
        )
        if active:
            raise OmnigentError(
                "Profile membership is changing; retry the request.",
                code=ErrorCode.CONFLICT,
            )
        payload["pending_profiles"] = [*pending, profile_id]


def finish_profile_protection_transition(profile_id: str) -> None:
    """Allow membership writes after a coordinated protection transition."""
    if not _uses_database_registry():
        with _lock:
            _file_pending_profiles.discard(profile_id)
        return
    with _registry_write_lock():
        payload = _registry_payload()
        pending = payload.get("pending_profiles", [])
        if not isinstance(pending, list):
            raise OmnigentError(
                "Private-profile lifecycle state is malformed.",
                code=ErrorCode.INTERNAL_ERROR,
            )
        payload["pending_profiles"] = [item for item in pending if item != profile_id]


def read_protected_profiles() -> tuple[ProtectedProfile, ...]:
    """Read the isolation registry, failing closed when it is malformed."""
    return _read_protected_profiles()


@contextlib.contextmanager
def _registry_write_lock():
    """Serialize registry read-modify-write cycles across server workers."""
    if _uses_database_registry():
        assert _database_registry is not None
        with _lock, _database_registry.locked() as payload:
            token = _active_registry_payload.set(payload)
            try:
                yield
            finally:
                _active_registry_payload.reset(token)
        return
    path = resolve_profile_protection_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with _lock, lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _canonicalize_roots(
    values: list[str],
    existing: tuple[ProtectedProfile, ...],
    *,
    host_id: str | None,
) -> tuple[Path, ...]:
    """Canonicalize roots and reject unsafe or cross-profile overlap."""
    home = Path.home().resolve() if host_id is None else None
    roots: list[Path] = []
    occupied = [
        root
        for profile in existing
        if profile.host_id == host_id
        for root in profile.protected_roots
    ]

    def validate_root(resolved: Path) -> None:
        if resolved == Path(resolved.anchor) or resolved == home:
            raise OmnigentError(
                "The filesystem root and home directory cannot be protected roots.",
                code=ErrorCode.INVALID_INPUT,
            )
        for other in [*occupied, *roots]:
            if (
                resolved == other
                or resolved.is_relative_to(other)
                or other.is_relative_to(resolved)
            ):
                raise OmnigentError(
                    "A protected root overlaps another private profile.",
                    code=ErrorCode.INVALID_INPUT,
                )

    for value in values:
        if "\x00" in value:
            raise OmnigentError(
                "Protected roots must not contain NUL bytes.",
                code=ErrorCode.INVALID_INPUT,
            )
        candidate = Path(value).expanduser() if host_id is None else Path(value)
        if not candidate.is_absolute():
            raise OmnigentError(
                "Protected roots must be absolute paths.", code=ErrorCode.INVALID_INPUT
            )
        if host_id is None:
            try:
                unresolved = candidate.resolve(strict=False)
            except OSError as exc:
                raise OmnigentError(
                    f"Protected root is invalid: {value}", code=ErrorCode.INVALID_INPUT
                ) from exc
        else:
            unresolved = Path(os.path.normpath(value))
        validate_root(unresolved)
        if host_id is None:
            try:
                unresolved.mkdir(parents=True, exist_ok=True)
                resolved = unresolved.resolve(strict=True)
            except OSError as exc:
                raise OmnigentError(
                    f"Could not create protected root: {value}", code=ErrorCode.INVALID_INPUT
                ) from exc
            if not resolved.is_dir():
                raise OmnigentError(
                    f"Protected root is not a directory: {value}",
                    code=ErrorCode.INVALID_INPUT,
                )
        else:
            resolved = unresolved
        validate_root(resolved)
        roots.append(resolved)
    if not roots:
        raise OmnigentError(
            "A private profile needs at least one protected root.", code=ErrorCode.INVALID_INPUT
        )
    return tuple(sorted(roots, key=str))


def _write_profiles(
    profiles: tuple[ProtectedProfile, ...], *, increment_generation: bool = False
) -> None:
    generation = profile_protection_generation()
    entries = [
        {
            "profile_id": profile.profile_id,
            "user_id": profile.user_id,
            "host_id": profile.host_id,
            "passcode_hash": profile.passcode_hash,
            "protected_roots": [str(root) for root in profile.protected_roots],
            "unlock_token_hashes": list(profile.unlock_token_hashes),
        }
        for profile in sorted(profiles, key=lambda item: item.profile_id)
    ]
    if _uses_database_registry():
        payload = _active_registry_payload.get()
        if payload is None:
            raise RuntimeError("database registry writes require the registry lock")
        payload["generation"] = generation + 1 if increment_generation else generation
        payload["profiles"] = entries
        return
    path = resolve_profile_protection_path()
    payload = {
        "generation": generation + 1 if increment_generation else generation,
        "profiles": entries,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _read_runner_generation_leases(*, now: float) -> tuple[RunnerGenerationLease, ...]:
    """Read durable runner leases while the protection registry lock is held."""
    _ = now
    if _uses_database_registry():
        entries = _registry_payload().get("leases", [])
    else:
        path = _runner_lease_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ()
        except (OSError, ValueError, TypeError) as exc:
            raise OmnigentError(
                "Runner protection leases are unreadable.",
                code=ErrorCode.INTERNAL_ERROR,
            ) from exc
        entries = raw.get("leases") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise OmnigentError(
            "Runner protection leases are malformed.",
            code=ErrorCode.INTERNAL_ERROR,
        )
    leases: list[RunnerGenerationLease] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise OmnigentError(
                "Runner protection leases are malformed.",
                code=ErrorCode.INTERNAL_ERROR,
            )
        lease_id = entry.get("lease_id")
        runner_id = entry.get("runner_id")
        generation = entry.get("generation")
        expires_at = entry.get("expires_at")
        if (
            not isinstance(lease_id, str)
            or not lease_id
            or not isinstance(runner_id, str)
            or not runner_id
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 0
            or not isinstance(expires_at, (int, float))
            or isinstance(expires_at, bool)
        ):
            raise OmnigentError(
                "Runner protection leases are malformed.",
                code=ErrorCode.INTERNAL_ERROR,
            )
        leases.append(
            RunnerGenerationLease(
                lease_id=lease_id,
                runner_id=runner_id,
                generation=generation,
                expires_at=float(expires_at),
            )
        )
    return tuple(leases)


def _write_runner_generation_leases(leases: tuple[RunnerGenerationLease, ...]) -> None:
    """Atomically persist runner leases while the registry lock is held."""
    entries = [
        {
            "lease_id": lease.lease_id,
            "runner_id": lease.runner_id,
            "generation": lease.generation,
            "expires_at": lease.expires_at,
        }
        for lease in sorted(leases, key=lambda item: item.lease_id)
    ]
    if _uses_database_registry():
        payload = _active_registry_payload.get()
        if payload is None:
            raise RuntimeError("database lease writes require the registry lock")
        payload["leases"] = entries
        return
    path = _runner_lease_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"leases": entries}
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def register_runner_generation_lease(
    runner_id: str,
    generation: int,
    lease_id: str,
) -> bool:
    """Atomically register a tunnel only if its generation is current."""
    now = time.time()
    with _registry_write_lock():
        if profile_protection_generation() != generation:
            return False
        leases = tuple(
            lease
            for lease in _read_runner_generation_leases(now=now)
            if lease.lease_id != lease_id
        )
        _write_runner_generation_leases(
            (
                *leases,
                RunnerGenerationLease(
                    lease_id=lease_id,
                    runner_id=runner_id,
                    generation=generation,
                    expires_at=now + RUNNER_GENERATION_LEASE_TTL_SECONDS,
                ),
            )
        )
        return True


def refresh_runner_generation_lease(lease_id: str, generation: int) -> bool:
    """Refresh a live lease, failing when protection generation changed."""
    now = time.time()
    with _registry_write_lock():
        leases = _read_runner_generation_leases(now=now)
        target = next(
            (
                lease
                for lease in leases
                if lease.lease_id == lease_id and lease.generation == generation
            ),
            None,
        )
        if target is None or profile_protection_generation() != generation:
            return False
        replacement = RunnerGenerationLease(
            lease_id=target.lease_id,
            runner_id=target.runner_id,
            generation=target.generation,
            expires_at=now + RUNNER_GENERATION_LEASE_TTL_SECONDS,
        )
        _write_runner_generation_leases(
            tuple(replacement if lease.lease_id == lease_id else lease for lease in leases)
        )
        return True


def unregister_runner_generation_lease(lease_id: str) -> None:
    """Remove one tunnel lease without disturbing a replacement connection."""
    now = time.time()
    with _registry_write_lock():
        leases = _read_runner_generation_leases(now=now)
        remaining = tuple(lease for lease in leases if lease.lease_id != lease_id)
        if remaining != leases:
            _write_runner_generation_leases(remaining)


def stale_runner_generation_leases(generation: int) -> tuple[RunnerGenerationLease, ...]:
    """Return live cross-worker leases that predate ``generation``."""
    now = time.time()
    with _registry_write_lock():
        leases = _read_runner_generation_leases(now=now)
        return tuple(lease for lease in leases if lease.generation != generation)


def plan_profile_protection(
    profile_id: str,
    *,
    user_id: str | None,
    host_id: str | None = None,
    passcode: str | None,
    protected_roots: list[str],
) -> ProfileProtectionChange:
    """Prepare a protection change without making its roots effective."""
    with _registry_write_lock():
        current = _read_protected_profiles()
        previous = next((item for item in current if item.profile_id == profile_id), None)
        if passcode == "" or (passcode is None and previous is None):
            raise OmnigentError("Passcode must not be empty.", code=ErrorCode.INVALID_INPUT)
        others = tuple(item for item in current if item.profile_id != profile_id)
        if previous is not None and previous.host_id != host_id:
            raise OmnigentError(
                "Disable private-profile protection before changing its host.",
                code=ErrorCode.CONFLICT,
            )
        roots = _canonicalize_roots(protected_roots, others, host_id=host_id)
        if passcode is not None:
            passcode_hash = hash_password(passcode)
        else:
            assert previous is not None
            passcode_hash = previous.passcode_hash
        configured = ProtectedProfile(
            profile_id=profile_id,
            user_id=user_id,
            host_id=host_id,
            passcode_hash=passcode_hash,
            protected_roots=roots,
            unlock_token_hashes=(),
        )
        previous_roots = frozenset(previous.protected_roots if previous else ())
        configured_roots = frozenset(configured.protected_roots)
        return ProfileProtectionChange(
            before=current,
            after=(*others, configured),
            profile_id=profile_id,
            user_id=user_id,
            changed_roots=tuple(sorted(previous_roots ^ configured_roots, key=str)),
        )


def plan_profile_protection_removal(
    profile_id: str, *, user_id: str | None
) -> ProfileProtectionChange | None:
    """Prepare removal without changing the active isolation registry."""
    with _registry_write_lock():
        current = _read_protected_profiles()
        target = next((item for item in current if item.profile_id == profile_id), None)
        if target is None or target.user_id != user_id:
            return None
        return ProfileProtectionChange(
            before=current,
            after=tuple(item for item in current if item.profile_id != profile_id),
            profile_id=profile_id,
            user_id=user_id,
            changed_roots=tuple(sorted(target.protected_roots, key=str)),
        )


def apply_profile_protection_change(
    change: ProfileProtectionChange,
    *,
    required_unlock_token: str | None = None,
) -> ProtectedProfile | None:
    """Apply a prepared change if the registry has not changed meanwhile."""
    with _registry_write_lock():
        current = _read_protected_profiles()
        if required_unlock_token is not None:
            protected = next(
                (
                    profile
                    for profile in current
                    if profile.profile_id == change.profile_id
                    and profile.user_id == change.user_id
                ),
                None,
            )
            if protected is None or not _unlock_token_matches(
                protected, required_unlock_token, now=int(time.time())
            ):
                raise OmnigentError(
                    "Profile unlock is no longer valid.",
                    code=ErrorCode.UNAUTHORIZED,
                )
        if current != change.before:
            raise OmnigentError(
                "Private-profile protection changed concurrently; retry the request.",
                code=ErrorCode.CONFLICT,
            )
        _write_profiles(change.after, increment_generation=bool(change.changed_roots))
    return change.configured


def revert_profile_protection_change(change: ProfileProtectionChange) -> None:
    """Conditionally compensate a change whose durable DB update failed."""
    with _registry_write_lock():
        if _read_protected_profiles() != change.after:
            raise OmnigentError(
                "Private-profile protection changed concurrently; retry the request.",
                code=ErrorCode.CONFLICT,
            )
        _write_profiles(change.before, increment_generation=bool(change.changed_roots))


def configure_profile_protection(
    profile_id: str,
    *,
    user_id: str | None,
    host_id: str | None = None,
    passcode: str | None,
    protected_roots: list[str],
) -> ProtectedProfile:
    """Create or replace protection without external lifecycle coordination."""
    change = plan_profile_protection(
        profile_id,
        user_id=user_id,
        host_id=host_id,
        passcode=passcode,
        protected_roots=protected_roots,
    )
    configured = apply_profile_protection_change(change)
    assert configured is not None
    return configured


def remove_profile_protection(profile_id: str, *, user_id: str | None) -> bool:
    """Remove protection without external lifecycle coordination."""
    change = plan_profile_protection_removal(profile_id, user_id=user_id)
    if change is None:
        return False
    apply_profile_protection_change(change)
    return True


def restore_profile_protection(profile: ProtectedProfile) -> None:
    """Restore a trusted snapshot after a cross-store update fails."""
    with _registry_write_lock():
        current = _read_protected_profiles()
        others = tuple(item for item in current if item.profile_id != profile.profile_id)
        _write_profiles((*others, profile), increment_generation=True)


def get_profile_protection(profile_id: str, *, user_id: str | None) -> ProtectedProfile | None:
    """Return an owned profile's server-side protection settings."""
    return next(
        (
            profile
            for profile in read_protected_profiles()
            if profile.profile_id == profile_id and profile.user_id == user_id
        ),
        None,
    )


def get_profile_protection_by_id(profile_id: str) -> ProtectedProfile | None:
    """Return protection state by profile identity, regardless of the viewer."""
    return next(
        (profile for profile in read_protected_profiles() if profile.profile_id == profile_id),
        None,
    )


def protected_profile_ids(*, user_id: str | None) -> frozenset[str]:
    """Return private profile IDs owned by one user."""
    return frozenset(
        profile.profile_id for profile in read_protected_profiles() if profile.user_id == user_id
    )


def profile_is_accessible(profile_id: str, token: str | None) -> bool:
    """Return whether a profile is public or its owner-bound token is valid."""
    profile = get_profile_protection_by_id(profile_id)
    declared = (
        _declared_protected_profile_ids is not None
        and profile_id in _declared_protected_profile_ids()
    )
    if profile is None:
        return not declared
    return validate_profile_unlock(
        token,
        profile_id=profile.profile_id,
        user_id=profile.user_id,
    )


def _normalize_workspace(workspace: str | Path, *, host_id: str | None) -> Path:
    """Normalize local paths physically and remote host paths lexically."""
    if host_id is None:
        return Path(workspace).expanduser().resolve(strict=False)
    return Path(os.path.normpath(str(workspace)))


def workspace_belongs_to_profile(
    profile_id: str,
    workspace: str | Path,
    *,
    host_id: str | None = None,
) -> bool:
    """Return whether a workspace is inside this private profile's roots."""
    resolved = _normalize_workspace(workspace, host_id=host_id)
    profile = next(
        (item for item in read_protected_profiles() if item.profile_id == profile_id), None
    )
    return (
        profile is not None
        and profile.host_id == host_id
        and any(
            resolved == root or resolved.is_relative_to(root) for root in profile.protected_roots
        )
    )


def protected_profile_for_workspace(
    workspace: str | Path,
    *,
    host_id: str | None = None,
) -> str | None:
    """Return the private profile owning a workspace, if any."""
    resolved = _normalize_workspace(workspace, host_id=host_id)
    return next(
        (
            profile.profile_id
            for profile in read_protected_profiles()
            if profile.host_id == host_id
            if any(
                resolved == root or resolved.is_relative_to(root)
                for root in profile.protected_roots
            )
        ),
        None,
    )


def mint_profile_unlock(profile_id: str, passcode: str, *, user_id: str | None) -> str:
    """Verify a passcode and return a shared-registry, profile-scoped bearer."""
    _record_unlock_attempt(profile_id, user_id)
    profile = get_profile_protection(profile_id, user_id=user_id)
    if profile is None:
        raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
    try:
        verify_password(passcode, profile.passcode_hash)
    except InvalidPasswordError as exc:
        raise OmnigentError("Incorrect passcode.", code=ErrorCode.UNAUTHORIZED) from exc
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = int(time.time())
    token_record = f"{token_hash}.{now + _UNLOCK_TTL_SECONDS}"
    with _registry_write_lock():
        current = _read_protected_profiles()
        target = next(
            (
                item
                for item in current
                if item.profile_id == profile_id and item.user_id == user_id
            ),
            None,
        )
        if target is None:
            raise OmnigentError("Profile not found", code=ErrorCode.NOT_FOUND)
        if not hmac.compare_digest(target.passcode_hash, profile.passcode_hash):
            raise OmnigentError(
                "The profile passcode changed; try unlocking again.",
                code=ErrorCode.UNAUTHORIZED,
            )
        live_records = [
            value for value in target.unlock_token_hashes if _unlock_record_live(value, now=now)
        ][-_MAX_ACTIVE_UNLOCK_TOKENS + 1 :]
        replacement = ProtectedProfile(
            profile_id=target.profile_id,
            user_id=target.user_id,
            host_id=target.host_id,
            passcode_hash=target.passcode_hash,
            protected_roots=target.protected_roots,
            unlock_token_hashes=(
                *live_records,
                token_record,
            ),
        )
        _write_profiles(tuple(replacement if item is target else item for item in current))
    return token


def validate_profile_unlock(token: str | None, *, profile_id: str, user_id: str | None) -> bool:
    """Return whether a bearer is live for this profile and owner."""
    if not token:
        return False
    profile = get_profile_protection(profile_id, user_id=user_id)
    if profile is None:
        return False
    return _unlock_token_matches(profile, token, now=int(time.time()))


def _unlock_token_matches(profile: ProtectedProfile, token: str, *, now: int) -> bool:
    """Validate one bearer against an already-loaded protected profile."""
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return any(
        _unlock_record_live(value, now=now)
        and hmac.compare_digest(token_hash, value.partition(".")[0])
        for value in profile.unlock_token_hashes
    )


def revoke_profile_unlock(token: str | None, *, profile_id: str, user_id: str | None) -> None:
    """Revoke a matching profile-scoped bearer."""
    if not token:
        return
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with _registry_write_lock():
        current = _read_protected_profiles()
        target = next(
            (
                item
                for item in current
                if item.profile_id == profile_id and item.user_id == user_id
            ),
            None,
        )
        if target is None or not any(
            hmac.compare_digest(token_hash, value.partition(".")[0])
            for value in target.unlock_token_hashes
        ):
            return
        replacement = ProtectedProfile(
            profile_id=target.profile_id,
            user_id=target.user_id,
            host_id=target.host_id,
            passcode_hash=target.passcode_hash,
            protected_roots=target.protected_roots,
            unlock_token_hashes=tuple(
                value
                for value in target.unlock_token_hashes
                if not hmac.compare_digest(token_hash, value.partition(".")[0])
            ),
        )
        _write_profiles(tuple(replacement if item is target else item for item in current))


def _unlock_record_live(value: str, *, now: int) -> bool:
    """Return whether a persisted token-hash record has not expired."""
    _, separator, expires = value.partition(".")
    if not separator:
        return False
    try:
        return int(expires) > now
    except ValueError:
        return False


def isolation_masks_for_workspace(
    workspace: str | Path,
    *,
    host_id: str | None = None,
    profiles: tuple[ProtectedProfile, ...] | None = None,
) -> tuple[Path, ...]:
    """Return private roots hidden from an environment at ``workspace``."""
    resolved = _normalize_workspace(workspace, host_id=host_id)
    all_profiles = read_protected_profiles() if profiles is None else profiles
    effective_profiles = tuple(profile for profile in all_profiles if profile.host_id == host_id)
    owner_id = next(
        (
            profile.profile_id
            for profile in effective_profiles
            if any(
                resolved == root or resolved.is_relative_to(root)
                for root in profile.protected_roots
            )
        ),
        None,
    )
    return tuple(
        root
        for profile in effective_profiles
        if profile.profile_id != owner_id
        for root in profile.protected_roots
    )


def profile_isolation_snapshot(
    workspace: str | Path,
    *,
    host_id: str | None = None,
) -> tuple[int, tuple[Path, ...]]:
    """Atomically snapshot the generation and masks for a runner launch."""
    with _registry_write_lock():
        profiles = _read_protected_profiles()
        generation = profile_protection_generation()
        return generation, isolation_masks_for_workspace(
            workspace,
            host_id=host_id,
            profiles=profiles,
        )
