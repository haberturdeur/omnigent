"""Runtime approval reads through the authenticated HTTP session route."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from omnigent.inner.cursor_protocol.forwarder import read_pending_decisions
from omnigent.server.routes.sessions import routes_resources


@pytest.mark.asyncio
async def test_http_reads_live_mode_and_overrides_stale_snapshot(monkeypatch):
    async def inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(routes_resources.asyncio, "to_thread", inline)
    conversation = SimpleNamespace(id="session", runner_id="runner")
    store = Mock(get_conversation=Mock(return_value=conversation))
    mode = "unrestricted"
    requests = []

    def runner_response(request):
        requests.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "approval_mode": mode,
                "run_everything": mode == "unrestricted",
                "source": "cursor-runtime",
            },
        )

    async with httpx.AsyncClient(
        base_url="http://runner", transport=httpx.MockTransport(runner_response)
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
            stale = {"runEverything": False, "pending": [{"id": "tool", "kind": "tool"}]}
            pending = await read_pending_decisions(client, "session", stale, "epoch")
            assert next(iter(pending.values()))["auto_accept"] is True
            mode = "allowlist"
            pending = await read_pending_decisions(
                client, "session", {**stale, "runEverything": True}, "epoch"
            )
            assert next(iter(pending.values()))["auto_accept"] is False
            response = await client.get("/v1/sessions/session/cursor-approval-config")
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["approval_mode"] == "allowlist"
    assert requests == ["/v1/sessions/session/cursor-approval-config"] * 3


@pytest.mark.asyncio
async def test_unavailable_runtime_does_not_fall_back_to_cached_run_everything():
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(lambda request: httpx.Response(503))
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await read_pending_decisions(
                client,
                "session",
                {
                    "runEverything": True,
                    "pending": [{"id": "tool", "kind": "tool"}],
                },
                "epoch",
            )
