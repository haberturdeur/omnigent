"""The agent-driven todo list: schema plus the runner's publish path.

Vendors differ on whether their plan is readable (the native TUIs expose a task
list; the Copilot SDK only announces that its plan file changed), so the todo
panel is fed by a tool any harness can call rather than by scraping a vendor.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.runner.tool_dispatch import _publish_session_todos_via_rest
from omnigent.tools.builtins.todos import TODO_STATUSES, SysTodoWriteTool


class _RecordingClient:
    def __init__(self, status: int = 200) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._status = status

    async def post(self, url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        self.calls.append((url, json))
        return httpx.Response(
            self._status, request=httpx.Request("POST", f"http://test{url}"), json={}
        )


class _RaisingClient:
    async def post(self, url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        raise httpx.ConnectError("server gone")


def test_schema_matches_the_panel_statuses() -> None:
    schema = SysTodoWriteTool().get_schema()
    props = schema["function"]["parameters"]["properties"]["todos"]["items"]["properties"]
    assert props["status"]["enum"] == list(TODO_STATUSES)
    assert schema["function"]["parameters"]["required"] == ["todos"]


@pytest.mark.asyncio
async def test_publishes_the_same_event_a_native_forwarder_sends() -> None:
    client = _RecordingClient()
    out = await _publish_session_todos_via_rest(
        {
            "todos": [
                {"content": "Add the migration", "status": "in_progress"},
                {"content": "Backfill rows", "status": "pending", "activeForm": "Backfilling"},
            ]
        },
        "conv_abc",
        client,  # type: ignore[arg-type]
    )
    assert json.loads(out) == {"ok": True, "count": 2}
    url, body = client.calls[0]
    assert url == "/v1/sessions/conv_abc/events"
    assert body["type"] == "external_session_todos"
    assert body["data"]["todos"] == [
        # activeForm defaults to content so a row never renders blank.
        {
            "content": "Add the migration",
            "status": "in_progress",
            "activeForm": "Add the migration",
        },
        {"content": "Backfill rows", "status": "pending", "activeForm": "Backfilling"},
    ]


@pytest.mark.asyncio
async def test_bad_input_is_a_tool_result_not_an_exception() -> None:
    client = _RecordingClient()
    for args in (
        {},
        {"todos": "nope"},
        {"todos": [{"status": "pending"}]},
        {"todos": [{"content": "  ", "status": "pending"}]},
        {"todos": [{"content": "x", "status": "done"}]},
        {"todos": ["not-an-object"]},
    ):
        out = json.loads(await _publish_session_todos_via_rest(args, "conv_abc", client))  # type: ignore[arg-type]
        assert "error" in out, args
    # Nothing malformed reached the server.
    assert client.calls == []


@pytest.mark.asyncio
async def test_transport_and_http_failures_never_raise() -> None:
    out = json.loads(
        await _publish_session_todos_via_rest(
            {"todos": [{"content": "x", "status": "pending"}]},
            "conv_abc",
            _RaisingClient(),  # type: ignore[arg-type]
        )
    )
    assert "error" in out
    out = json.loads(
        await _publish_session_todos_via_rest(
            {"todos": [{"content": "x", "status": "pending"}]},
            "conv_abc",
            _RecordingClient(status=500),  # type: ignore[arg-type]
        )
    )
    assert "error" in out


@pytest.mark.asyncio
async def test_missing_session_or_server_is_reported() -> None:
    args = {"todos": [{"content": "x", "status": "pending"}]}
    assert "error" in json.loads(await _publish_session_todos_via_rest(args, "conv", None))
    assert "error" in json.loads(
        await _publish_session_todos_via_rest(args, None, _RecordingClient())  # type: ignore[arg-type]
    )
