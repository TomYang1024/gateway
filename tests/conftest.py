import json
import os
from contextlib import asynccontextmanager

import httpx
import pytest
from asgi_lifespan import LifespanManager
from cryptography.fernet import Fernet
from llm_gateway_api.app import create_app
from llm_gateway_api.development import development_snapshot
from llm_gateway_api.mock_upstream import fixture, stream_fixture
from llm_gateway_infrastructure.settings import Settings
from llm_gateway_infrastructure.storage import Database, metadata
from redis.asyncio import Redis


class Chunks(httpx.AsyncByteStream):
    def __init__(self, body, size=1, error=None):
        self.body, self.size, self.error, self.closed = body, size, error, False

    async def __aiter__(self):
        for i in range(0, len(self.body), self.size):
            yield self.body[i : i + self.size]
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


def sse_bytes(events):
    return "".join(
        f"event: {event}\r\ndata: {json.dumps(b, ensure_ascii=False)}\r\n\r\n" for event, b in events
    ).encode()


class Upstream:
    def __init__(self):
        self.requests, self.override = [], None

    async def __call__(self, request):
        payload = json.loads(request.content)
        self.requests.append((request, payload))
        if self.override:
            response = await self.override(request, payload)
            if response is not None:
                return response
        protocol = (
            "openai_chat"
            if request.url.path.endswith("chat/completions")
            else "openai_responses"
            if request.url.path.endswith("responses")
            else "anthropic_messages"
        )
        body = fixture(protocol, payload)
        if payload.get("stream"):
            return httpx.Response(
                200,
                stream=Chunks(sse_bytes(stream_fixture(protocol, body))),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=body)


@pytest.fixture
def upstream():
    return Upstream()


@pytest.fixture
def snapshot():
    return development_snapshot("http://upstream/v1-prefix".removesuffix("/v1-prefix"))


@pytest.fixture
def basic():
    return {
        "logical_model": "assistant",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
    }


@pytest.fixture
def tools():
    return [
        {
            "name": "lookup_order",
            "version": "1",
            "parameters": {
                "type": "object",
                "properties": {"order_no": {"type": "string"}},
                "required": ["order_no"],
                "additionalProperties": False,
            },
        }
    ]


@pytest.fixture
async def infrastructure():
    url = os.getenv(
        "GATEWAY_TEST_DATABASE_URL", "postgresql+asyncpg://gateway:gateway@localhost:55432/gateway_test"
    )
    redis_url = os.getenv("GATEWAY_TEST_REDIS_URL", "redis://localhost:56379/15")
    if not url.rsplit("/", 1)[-1].endswith("_test") or not redis_url.endswith("/15"):
        pytest.fail("Tests require an isolated *_test database and Redis DB 15")
    db = Database(url)
    redis = Redis.from_url(redis_url)
    try:
        await redis.ping()
        async with db.engine.begin() as c:
            await c.run_sync(metadata.create_all)
            for table in reversed(metadata.sorted_tables):
                await c.execute(table.delete())
        await redis.flushdb()
        yield db, redis, url, redis_url
    finally:
        await redis.aclose()
        await db.engine.dispose()


@pytest.fixture
async def client_factory(infrastructure, tmp_path, monkeypatch, upstream, snapshot):
    db, redis, url, redis_url = infrastructure
    for key in ("GLM_API_KEY", "DEEPSEEK_API_KEY", "KIMI_API_KEY"):
        monkeypatch.setenv(key, "test-only-secret")
    await db.bootstrap(snapshot)
    apps = []

    @asynccontextmanager
    async def factory(**overrides):
        settings = Settings(
            database_url=url,
            redis_url=redis_url,
            spool_dir=str(tmp_path / f"spool-{len(apps)}"),
            continuation_key=Fernet.generate_key().decode(),
            policy_poll_s=30,
            **overrides,
        )
        app = create_app(settings, transport=httpx.MockTransport(upstream))
        apps.append(app)
        async with LifespanManager(app):
            assert app.state.ready
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://gateway",
                headers={"Authorization": "Bearer dev-app-key"},
            ) as client:
                client.app = app
                yield client

    return factory


@pytest.fixture
async def client(client_factory):
    async with client_factory() as client:
        yield client
