"""Feature verification over the real API, PostgreSQL and Redis with a simulated upstream.

Run with `make verify`. Each section maps to one delivery requirement.
"""

import asyncio
import json

import httpx
import pytest
from llm_gateway_core.config import RoutePolicy
from llm_gateway_core.contracts import GatewayRequest
from pydantic import ValidationError

ADMIN = {"Authorization": "Bearer dev-admin-key"}
MODELS = ["glm", "deepseek", "kimi"]
ENDPOINTS = {"glm": "chat/completions", "deepseek": "responses", "kimi": "messages"}
MESSAGES = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
ORDER_SCHEMA = {
    "type": "object",
    "properties": {"order_no": {"type": "string"}},
    "required": ["order_no"],
    "additionalProperties": False,
}
ERROR_FIELDS = {"code", "message", "call_id", "retryable", "details"}


async def publish(client, mutate):
    current = (await client.get("/admin/v1/policies/current", headers=ADMIN)).json()
    mutate(current["snapshot"])
    response = await client.post(
        "/admin/v1/policies",
        headers=ADMIN,
        json={"base_version": current["version"], "snapshot": current["snapshot"], "reason": "verification"},
    )
    assert response.status_code == 200, response.text


def sse_events(response):
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]


# 1. Unified abstraction


@pytest.mark.parametrize("model", MODELS)
async def test_model_field_routes_to_its_adapter(client, upstream, model):
    response = await client.post("/v1/generate", json={"model": model, "messages": MESSAGES})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["actual_provider"] == model
    assert body["content"]
    request, _ = upstream.requests[0]
    assert request.url.path.endswith(ENDPOINTS[model])
    assert any("test-only-secret" in value for value in request.headers.values())


def test_model_is_an_alias_of_logical_model():
    assert GatewayRequest.model_validate({"model": "glm", "messages": MESSAGES}).logical_model == "glm"


# 2. Streaming


@pytest.mark.parametrize("model", MODELS)
async def test_stream_true_returns_sse_chunks(client, model):
    response = await client.post("/v1/generate", json={"model": model, "messages": MESSAGES, "stream": True})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = sse_events(response)
    assert sum(e["type"] == "delta" for e in events) > 1
    assert events[-1]["type"] == "terminal"
    assert events[-1]["data"]["status"] == "completed"


# 3. Structured output


@pytest.mark.parametrize("model", MODELS)
async def test_response_format_returns_valid_json(client, model):
    response = await client.post(
        "/v1/generate",
        json={
            "model": model,
            "messages": MESSAGES,
            "response_format": {"type": "json_schema", "json_schema": {"schema": ORDER_SCHEMA}},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["structured_data"] == {"order_no": "R123"}


async def test_response_format_rejects_output_violating_the_schema(client):
    schema = {**ORDER_SCHEMA, "required": ["order_no", "missing"]}
    schema["properties"] = {**schema["properties"], "missing": {"type": "string"}}
    response = await client.post(
        "/v1/generate",
        json={
            "model": "glm",
            "messages": MESSAGES,
            "response_format": {"type": "json_schema", "json_schema": {"schema": schema}},
        },
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "OUTPUT_VALIDATION_FAILED"


def test_response_format_requires_a_schema():
    with pytest.raises(ValidationError):
        GatewayRequest.model_validate(
            {"model": "glm", "messages": MESSAGES, "response_format": {"type": "json_schema"}}
        )


# 4. Prompt templates


async def test_prompt_reference_renders_variables_and_records_version(client, upstream):
    response = await client.post(
        "/v1/generate",
        json={
            "model": "order_extraction",
            "prompt": {"name": "extract_order", "version": "1.0.0", "variables": {"order_text": "订单R123"}},
        },
    )
    assert response.status_code == 200, response.text
    assert "订单R123" in json.dumps(upstream.requests[0][1], ensure_ascii=False)
    call = (await client.get(f"/v1/calls/{response.json()['call_id']}")).json()
    assert call["payload"]["prompt_version"] == "1.0.0"
    assert call["payload"]["prompt_hash"]


async def test_unknown_prompt_version_is_rejected(client, upstream):
    response = await client.post(
        "/v1/generate",
        json={
            "model": "order_extraction",
            "prompt": {"name": "extract_order", "version": "9.9.9", "variables": {"order_text": "x"}},
        },
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROMPT_NOT_FOUND"
    assert upstream.requests == []


# 5. Observability


async def test_call_records_token_categories_and_latency(client):
    response = await client.post("/v1/generate", json={"model": "glm", "messages": MESSAGES})
    call = (await client.get(f"/v1/calls/{response.json()['call_id']}")).json()
    usage = call["payload"]["usage"]
    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] > 0
    assert {"cached_input_tokens", "cache_write_tokens", "reasoning_tokens"} <= usage.keys()
    assert call["payload"]["total_ms"] > 0
    assert call["attempts"][0]["total_ms"] > 0


@pytest.mark.parametrize("model", MODELS)
async def test_stream_records_first_token_latency(client, model):
    response = await client.post("/v1/generate", json={"model": model, "messages": MESSAGES, "stream": True})
    call_id = sse_events(response)[-1]["call_id"]
    attempt = (await client.get(f"/v1/calls/{call_id}")).json()["attempts"][0]
    assert attempt["ttft_ms"] is not None
    assert attempt["first_content_ms"] is not None


async def test_metrics_expose_calls_and_latency(client):
    await client.post("/v1/generate", json={"model": "glm", "messages": MESSAGES})
    metrics = (await client.get("/metrics", headers=ADMIN)).text
    assert "gateway_calls_total" in metrics
    assert "gateway_call_seconds" in metrics


# 6. Resilience


async def test_errors_share_one_envelope(client, upstream):
    unauthenticated = await client.post(
        "/v1/generate", headers={"Authorization": "Bearer wrong"}, json={"model": "glm", "messages": MESSAGES}
    )
    invalid = await client.post("/v1/generate", json={"model": "glm"})

    async def rejected(request, payload):
        return httpx.Response(401)

    upstream.override = rejected
    upstream_failure = await client.post("/v1/generate", json={"model": "glm", "messages": MESSAGES})
    for response, status, code in (
        (unauthenticated, 401, "AUTHENTICATION_FAILED"),
        (invalid, 422, "REQUEST_INVALID"),
        (upstream_failure, 502, "UPSTREAM_AUTH_FAILED"),
    ):
        assert response.status_code == status
        assert response.json()["error"].keys() == ERROR_FIELDS
        assert response.json()["error"]["code"] == code


@pytest.fixture
def backoff_delays(monkeypatch):
    delays, real_sleep = [], asyncio.sleep

    async def spy(delay, *args):
        delays.append(delay)
        await real_sleep(delay, *args)

    monkeypatch.setattr("llm_gateway_core.execution.asyncio.sleep", spy)
    monkeypatch.setattr("llm_gateway_core.execution.random.uniform", lambda low, high: 1.0)
    return lambda: [d for d in delays if 0 < d < 0.5]


async def test_exponential_backoff_stops_after_three_retries(client, upstream, backoff_delays):
    await publish(client, lambda s: s["routes"]["glm"].update(initial_delay_s=0.01))

    async def unavailable(request, payload):
        return httpx.Response(503)

    upstream.override = unavailable
    response = await client.post("/v1/generate", json={"model": "glm", "messages": MESSAGES})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"
    assert len(upstream.requests) == 4
    assert backoff_delays() == [0.01, 0.02, 0.04]


async def test_retry_recovers_after_transient_failures(client, upstream):
    await publish(client, lambda s: s["routes"]["glm"].update(initial_delay_s=0))
    failures = iter([httpx.Response(503), httpx.Response(429)])

    async def transient(request, payload):
        return next(failures, None)

    upstream.override = transient
    response = await client.post("/v1/generate", json={"model": "glm", "messages": MESSAGES})
    assert response.status_code == 200, response.text
    call = (await client.get(f"/v1/calls/{response.json()['call_id']}")).json()
    assert len(call["attempts"]) == 3


def test_policy_cannot_exceed_three_retries():
    with pytest.raises(ValidationError):
        RoutePolicy.model_validate(
            {"candidates": [{"provider": "glm", "model": "glm"}], "max_retries_per_candidate": 4}
        )


async def test_rate_limit_is_independent_per_model(client, upstream):
    await publish(client, lambda s: s["models"]["glm"].update(rpm=2))
    payload = {"model": "glm", "messages": MESSAGES}
    for _ in range(2):
        assert (await client.post("/v1/generate", json=payload)).status_code == 200
    limited = await client.post("/v1/generate", json=payload)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "GATEWAY_RATE_LIMITED"
    assert len(upstream.requests) == 2
    other = await client.post("/v1/generate", json={"model": "deepseek", "messages": MESSAGES})
    assert other.status_code == 200, other.text


async def test_stream_is_rate_limited_before_commit(client):
    await publish(client, lambda s: s["models"]["glm"].update(rpm=1))
    payload = {"model": "glm", "messages": MESSAGES}
    assert (await client.post("/v1/generate", json=payload)).status_code == 200
    limited = await client.post("/v1/generate", json={**payload, "stream": True})
    assert sse_events(limited)[-1]["data"]["status"] == "failed"
    assert sse_events(limited)[-1]["data"]["error"]["code"] == "GATEWAY_RATE_LIMITED"
