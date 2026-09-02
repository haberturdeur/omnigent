"""Runner forwarding of a vendor-proposed session title.

Some vendors name a conversation from its opening turns (Copilot's
``SESSION_TITLE_CHANGED``), which the executor adapter emits as the
runner-internal ``session.title_suggested`` event. The runner applies it
through ``/auto-title`` — deliberately, since that endpoint's seed-only
semantics replace the deterministic first-message title but never a name the
user chose. A vendor rename must also never break the turn that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from omnigent.runner import app as runner_app_mod


@dataclass
class _Post:
    url: str
    body: dict[str, Any]


class _RecordingServerClient:
    """Records POSTs and returns queued real responses (so raise_for_status runs)."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.calls: list[_Post] = []

    async def post(self, url: str, *, json: dict[str, Any]) -> httpx.Response:
        self.calls.append(_Post(url=url, body=json))
        assert self._responses, f"unexpected POST #{len(self.calls)} (no response queued)"
        return self._responses.pop(0)


class _RaisingServerClient:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def post(self, url: str, *, json: dict[str, Any]) -> httpx.Response:
        raise self._exc


def _resp(status: int, url: str, body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", f"http://test{url}"), json=body)


@pytest.mark.asyncio
async def test_vendor_title_goes_through_the_seed_only_auto_title_route() -> None:
    """The rename must not be able to clobber a user-chosen session name."""
    url = "/v1/sessions/conv_abc/auto-title"
    client = _RecordingServerClient([_resp(200, url, {"renamed": True})])
    await runner_app_mod._apply_vendor_session_title(
        client,  # type: ignore[arg-type]
        conversation_id="conv_abc",
        title="auth-refactor",
    )
    assert len(client.calls) == 1
    assert client.calls[0].url == url
    assert client.calls[0].body == {"title": "auth-refactor"}


@pytest.mark.asyncio
async def test_a_declined_rename_is_not_an_error() -> None:
    """auto-title answering "no" (a human already named it) is a normal outcome."""
    url = "/v1/sessions/conv_abc/auto-title"
    client = _RecordingServerClient(
        [_resp(200, url, {"renamed": False, "reason": "title_not_seed"})]
    )
    await runner_app_mod._apply_vendor_session_title(
        client,  # type: ignore[arg-type]
        conversation_id="conv_abc",
        title="auth-refactor",
    )
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_a_failed_rename_never_raises_into_the_turn() -> None:
    """A title is cosmetic; losing it must not fail the work that produced it."""
    client = _RaisingServerClient(httpx.ConnectError("server gone"))
    # Must not raise.
    await runner_app_mod._apply_vendor_session_title(
        client,  # type: ignore[arg-type]
        conversation_id="conv_abc",
        title="auth-refactor",
    )


@pytest.mark.asyncio
async def test_an_http_error_response_never_raises_into_the_turn() -> None:
    url = "/v1/sessions/conv_abc/auto-title"
    client = _RecordingServerClient([_resp(500, url, {"error": "boom"})])
    await runner_app_mod._apply_vendor_session_title(
        client,  # type: ignore[arg-type]
        conversation_id="conv_abc",
        title="auth-refactor",
    )
    assert len(client.calls) == 1
