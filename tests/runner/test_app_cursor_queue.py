"""Native queue controls never inject a second user prompt."""

import pytest

from omnigent.inner.cursor_protocol import transport
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["cursor-native", "openai-agents"])
async def test_native_queue_controls(monkeypatch, harness):
    calls = []
    queue = {
        "revision": "native-1",
        "messages": [{"index": 0, "text": "follow-up"}],
        "can_steer": True,
    }

    def control(directory, body, **kwargs):
        calls.append(body)
        if body["action"] == "get_queue":
            return queue
        if body.get("revision") != "native-1":
            raise RuntimeError("Cursor queue changed")
        return {"status": "submitted"}

    monkeypatch.setattr(transport, "send_control", control)
    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )

    async def resolver(agent_id, session_id=None):
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),
        spec_resolver=resolver,
        server_client=NullServerClient(),
    )
    async with _runner_client(app) as client:
        response = await client.post(
            "/v1/sessions", json={"session_id": "queue-session", "agent_id": "agent"}
        )
        assert response.status_code == 201, response.text
        response = await client.get("/v1/sessions/queue-session/cursor-queue")
        if harness != "cursor-native":
            assert response.status_code == 404
            response = await client.post(
                "/v1/sessions/queue-session/cursor-queue", json={"action": "steer_queue"}
            )
            assert response.status_code == 404
            assert calls == []
            return
        assert response.json() == queue
        assert response.headers["cache-control"] == "no-store"
        response = await client.post(
            "/v1/sessions/queue-session/cursor-queue",
            json={"action": "prompt", "content": "duplicate"},
        )
        assert response.status_code == 400
        action = {"action": "steer_queue", "revision": "native-1", "index": 0}
        response = await client.post(
            "/v1/sessions/queue-session/cursor-queue",
            json={**action, "content": "must not be forwarded"},
        )
        assert response.status_code == 200
        assert calls == [{"action": "get_queue"}, action]
        response = await client.post(
            "/v1/sessions/queue-session/cursor-queue", json={**action, "revision": "stale"}
        )
        assert response.status_code == 409
        assert response.json()["error"] == "Cursor queue changed"
