"""Lifecycle fencing for private-profile root changes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.entities import Conversation, PagedList
from omnigent.errors import OmnigentError
from omnigent.server.profile_protection import (
    ProfileProtectionChange,
    apply_profile_protection_change,
    configure_profile_protection,
    plan_profile_protection,
    plan_profile_protection_removal,
    read_protected_profiles,
)
from omnigent.server.routes._sessions.helpers import _stop_stale_host_launch
from omnigent.server.routes.profiles import _apply_and_fence, _invalidate_non_owning_environments


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_PROFILE_PROTECTION_PATH", str(tmp_path / "protection.json"))
    monkeypatch.setattr(
        "omnigent.server.profile_protection.hash_password",
        lambda _passcode: "test-passcode-hash",
    )

    async def _inline_to_thread(function: Any, /, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(
        "omnigent.server.routes.profiles.asyncio",
        SimpleNamespace(to_thread=_inline_to_thread, sleep=asyncio.sleep),
    )


def _conversation(
    session_id: str,
    *,
    profile_id: str | None,
    runner_id: str,
    workspace: Path | None,
) -> Conversation:
    return Conversation(
        id=session_id,
        created_at=1,
        updated_at=1,
        root_conversation_id=session_id,
        profile_id=profile_id,
        runner_id=runner_id,
        workspace=str(workspace) if workspace is not None else None,
    )


class _ConversationStore:
    def __init__(self, conversations: list[Conversation]) -> None:
        self._conversations = conversations

    def list_conversations(self, **_: Any) -> PagedList[Conversation]:
        return PagedList(data=self._conversations, has_more=False)


class _RunnerClient:
    def __init__(self, *, status_codes: tuple[int, ...] = (200, 200)) -> None:
        self.status_codes = iter(status_codes)
        self.posts: list[str] = []

    async def post(self, url: str, **_: Any) -> httpx.Response:
        self.posts.append(url)
        return httpx.Response(
            next(self.status_codes),
            request=httpx.Request("POST", f"http://runner.test{url}"),
        )


class _RunnerRouter:
    def __init__(self, clients: dict[str, _RunnerClient]) -> None:
        self._clients = clients

    def runner_is_online(self, runner_id: str) -> bool:
        return runner_id in self._clients

    def client_for_existing_conversation(self, session_id: str) -> Any:
        client = self._clients.get(f"runner-{session_id}")
        return SimpleNamespace(client=client) if client is not None else None


def _change(tmp_path: Path, operation: str) -> ProfileProtectionChange:
    old_root = tmp_path / "old"
    if operation != "add":
        configure_profile_protection(
            "private", user_id="owner", passcode="secret", protected_roots=[str(old_root)]
        )
    if operation == "remove":
        removal = plan_profile_protection_removal("private", user_id="owner")
        assert removal is not None
        return removal
    target = old_root if operation == "add" else tmp_path / "new"
    return plan_profile_protection(
        "private",
        user_id="owner",
        passcode="secret" if operation == "add" else None,
        protected_roots=[str(target)],
    )


@pytest.mark.asyncio
async def test_post_launch_generation_fence_stops_stale_runner(monkeypatch) -> None:
    """A protection mutation during launch triggers host-side termination."""
    stopped: list[tuple[str, str, str]] = []

    async def _stop(session_id, host_id, runner_id, _registry):
        stopped.append((session_id, host_id, runner_id))
        return True

    monkeypatch.setattr(
        "omnigent.server.profile_protection.profile_protection_generation",
        lambda: 6,
    )
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._stop_session_host_runner",
        _stop,
    )

    stale = await _stop_stale_host_launch(
        session_id="session-1",
        host_id="host-1",
        runner_id="runner-1",
        launch_generation=5,
        host_registry=object(),
    )

    assert stale is True
    assert stopped == [("session-1", "host-1", "runner-1")]


@pytest.mark.asyncio
async def test_post_launch_generation_fence_keeps_current_runner(monkeypatch) -> None:
    """An unchanged generation does not issue a stop command."""
    monkeypatch.setattr(
        "omnigent.server.profile_protection.profile_protection_generation",
        lambda: 5,
    )

    stale = await _stop_stale_host_launch(
        session_id="session-1",
        host_id="host-1",
        runner_id="runner-1",
        launch_generation=5,
        host_registry=object(),
    )

    assert stale is False


@pytest.mark.parametrize("operation", ["add", "change", "remove"])
@pytest.mark.asyncio
async def test_root_change_resets_only_live_non_owning_environments(
    tmp_path: Path,
    operation: str,
) -> None:
    change = _change(tmp_path, operation)
    before = read_protected_profiles()
    public_client = _RunnerClient()
    unknown_client = _RunnerClient()
    offline_client = _RunnerClient()
    store = _ConversationStore(
        [
            _conversation(
                "public",
                profile_id=None,
                runner_id="runner-public",
                workspace=tmp_path / "public",
            ),
            _conversation(
                "unknown",
                profile_id="private",
                runner_id="runner-unknown",
                workspace=None,
            ),
            _conversation(
                "offline",
                profile_id=None,
                runner_id="runner-offline",
                workspace=tmp_path / "offline",
            ),
        ]
    )
    router = _RunnerRouter({"runner-public": public_client, "runner-unknown": unknown_client})

    await _invalidate_non_owning_environments(change, store, router)  # type: ignore[arg-type]

    assert public_client.posts == [
        "/v1/sessions/public/events",
        "/v1/sessions/public/reset-state",
    ]
    assert unknown_client.posts == [
        "/v1/sessions/unknown/events",
        "/v1/sessions/unknown/reset-state",
    ]
    assert offline_client.posts == []
    assert read_protected_profiles() == before
    apply_profile_protection_change(change)


@pytest.mark.asyncio
async def test_failed_environment_reset_aborts_before_root_change(tmp_path: Path) -> None:
    change = _change(tmp_path, "add")
    before = read_protected_profiles()
    store = _ConversationStore(
        [
            _conversation(
                "public",
                profile_id=None,
                runner_id="runner-public",
                workspace=tmp_path / "public",
            )
        ]
    )
    router = _RunnerRouter({"runner-public": _RunnerClient(status_codes=(500,))})

    with pytest.raises(OmnigentError, match="were not changed"):
        await _invalidate_non_owning_environments(  # type: ignore[arg-type]
            change, store, router
        )

    assert read_protected_profiles() == before


@pytest.mark.asyncio
async def test_failed_environment_reset_after_stop_aborts_root_change(tmp_path: Path) -> None:
    change = _change(tmp_path, "add")
    before = read_protected_profiles()
    store = _ConversationStore(
        [
            _conversation(
                "public",
                profile_id=None,
                runner_id="runner-public",
                workspace=tmp_path / "public",
            )
        ]
    )
    client = _RunnerClient(status_codes=(200, 500))
    router = _RunnerRouter({"runner-public": client})

    with pytest.raises(OmnigentError, match="were not changed"):
        await _invalidate_non_owning_environments(  # type: ignore[arg-type]
            change, store, router
        )

    assert client.posts == [
        "/v1/sessions/public/events",
        "/v1/sessions/public/reset-state",
    ]
    assert read_protected_profiles() == before


@pytest.mark.asyncio
async def test_generation_fence_failure_reverts_applied_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-apply fence error restores the prior registry generation."""
    change = _change(tmp_path, "add")
    before = read_protected_profiles()
    calls = 0

    async def fail_then_pass(_change: ProfileProtectionChange) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OmnigentError("stale runner", code="runner_unavailable")

    monkeypatch.setattr(
        "omnigent.server.routes.profiles._await_runner_generation_fence",
        fail_then_pass,
    )

    with pytest.raises(OmnigentError, match="stale runner"):
        await _apply_and_fence(change, required_unlock_token=None)

    assert calls == 2
    assert read_protected_profiles() == before
