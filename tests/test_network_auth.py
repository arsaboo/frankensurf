"""serve and frankensurf-mcp --http: an API key before either listens beyond this machine."""
import asyncio

import httpx
import pytest

from frankensurf import firecrawl_compat
from frankensurf.firecrawl_compat import authorized, network_guard


def test_network_guard_needs_a_key_off_loopback():
    network_guard("127.0.0.1", None)
    network_guard("localhost", None)
    network_guard("::1", None)
    with pytest.raises(ValueError, match="set an API key"):
        network_guard("0.0.0.0", None)
    with pytest.raises(ValueError):
        network_guard("192.168.1.20", None)
    network_guard("0.0.0.0", "k")
    network_guard("0.0.0.0", None, allow_unauthenticated=True)


def test_authorized_is_a_bearer_check():
    assert authorized({}, None)
    assert authorized({"authorization": "Bearer secret"}, "secret")
    assert not authorized({"authorization": "Bearer nope"}, "secret")
    assert not authorized({}, "secret")


def test_serve_answers_401_without_the_key_but_health_stays_open(monkeypatch):
    class FakeRuntime:
        def __init__(self, state_dir=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def read(self, url, policy_overrides=None):
            return {"url": url, "text": "Example page " * 20, "title": "E",
                    "receipt": {"status": "observed", "trace_id": "t", "attempts": [{"provider": "http"}]}}

    monkeypatch.setattr(firecrawl_compat, "Runtime", FakeRuntime)

    async def scenario():
        started = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(firecrawl_compat.serve(None, "127.0.0.1", 0, ready=started.set_result,
                                                          api_key="secret"))
        server = await started
        base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        async with httpx.AsyncClient() as client:
            health = await client.get(base + "/health")
            refused = await client.post(base + "/v1/scrape", json={"url": "https://example.com"})
            wrong = await client.post(base + "/v1/scrape", json={"url": "https://example.com"},
                                      headers={"Authorization": "Bearer nope"})
            ok = await client.post(base + "/v1/scrape", json={"url": "https://example.com"},
                                   headers={"Authorization": "Bearer secret"})
        task.cancel()
        return health, refused, wrong, ok

    health, refused, wrong, ok = asyncio.run(scenario())
    assert health.status_code == 200
    assert refused.status_code == 401 and wrong.status_code == 401
    assert ok.status_code == 200 and ok.json()["success"] is True


def test_serve_refuses_to_open_on_the_network_without_a_key():
    with pytest.raises(ValueError, match="set an API key"):
        asyncio.run(firecrawl_compat.serve(None, "0.0.0.0", 0))


def test_mcp_http_wrapper_checks_the_key():
    pytest.importorskip("mcp")
    from frankensurf.mcp_server import _with_key
    reached = []

    async def app(scope, receive, send):
        reached.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def call(headers):
        sent = []

        async def send(message):
            sent.append(message)
        await _with_key(app, "secret")({"type": "http", "path": "/mcp", "headers": headers}, None, send)
        return sent[0]["status"]
    assert asyncio.run(call([])) == 401
    assert asyncio.run(call([(b"authorization", b"Bearer secret")])) == 200 and reached == ["/mcp"]
