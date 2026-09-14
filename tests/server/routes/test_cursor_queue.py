"""Authenticated session API proxies native queue reads and steering."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from omnigent.server.routes.sessions import routes_resources


@pytest.mark.asyncio
async def test_native_queue_proxy_preserves_snapshot_and_stale_action_errors(monkeypatch):
    async def inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(routes_resources, "asyncio", SimpleNamespace(to_thread=inline))
    store = Mock(
        get_conversation=Mock(return_value=SimpleNamespace(id="session", runner_id="runner"))
    )
    requests = []
    queue = {
        "revision": "live",
        "messages": [{"index": 0, "text": "native follow-up"}],
        "can_steer": True,
    }

    def respond(request):
        requests.append(request)
        return (
            httpx.Response(200, json=queue)
            if request.method == "GET"
            else httpx.Response(409, json={"error": "Cursor queue changed"})
        )

    async with httpx.AsyncClient(
        base_url="http://runner", transport=httpx.MockTransport(respond)
    ) as runner:
        monkeypatch.setattr(
            routes_resources,
            "_get_runner_client_for_resource_access",
            AsyncMock(return_value=runner),
        )
        app = FastAPI()
        router = APIRouter(prefix="/v1")
        routes_resources.register_resources_routes(
            router, conversation_store=store, agent_store=Mock()
        )
        app.include_router(router)
        async with httpx.AsyncClient(
            base_url="http://server", transport=httpx.ASGITransport(app=app)
        ) as client:
            response = await client.get("/v1/sessions/session/cursor-queue")
            assert response.json() == queue
            assert response.headers["cache-control"] == "no-store"
            action = {"action": "steer_queue", "revision": "stale", "index": 0}
            response = await client.post("/v1/sessions/session/cursor-queue", json=action)
            assert response.status_code == 409
            assert response.json() == {"error": "Cursor queue changed"}
    assert all(request.url.path == "/v1/sessions/session/cursor-queue" for request in requests)
    assert json.loads(requests[1].content) == action
