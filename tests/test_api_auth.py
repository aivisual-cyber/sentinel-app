"""API token middleware: enforced when SENTINEL_API_TOKEN is set, /api/health stays open."""

import pytest
from aiohttp import web

import api_server


async def _ok(request):
    return web.json_response({"ok": True})


@pytest.fixture
def app_factory(monkeypatch):
    def make(token):
        monkeypatch.setattr(api_server, "API_TOKEN", token)
        app = web.Application(middlewares=[api_server.auth_middleware])
        app.router.add_get("/api/health", _ok)
        app.router.add_get("/api/dashboard", _ok)
        return app
    return make


@pytest.mark.asyncio
async def test_token_required(aiohttp_client, app_factory):
    client = await aiohttp_client(app_factory("tok123"))
    assert (await client.get("/api/health")).status == 200
    assert (await client.get("/api/dashboard")).status == 401
    assert (await client.get("/api/dashboard", headers={"Authorization": "Bearer wrong"})).status == 401
    assert (await client.get("/api/dashboard", headers={"Authorization": "Bearer tok123"})).status == 200


@pytest.mark.asyncio
async def test_no_token_configured_is_open(aiohttp_client, app_factory):
    client = await aiohttp_client(app_factory(""))
    assert (await client.get("/api/dashboard")).status == 200
