import asyncio
import json

import httpx
import pytest
from llm_gateway_api.app import sse
from llm_gateway_core.contracts import GatewayRequest
from llm_gateway_core.security import authenticate
from test_gateway import ADMIN, publish

pytestmark = pytest.mark.integration


async def test_emergency_disable_existing_plan(client, basic, upstream):
    gateway = client.app.state.gateway
    version, snapshot = gateway.control.get()
    prepared = await gateway.prepare(
        GatewayRequest.model_validate(basic), authenticate(snapshot, version, "dev-app-key")
    )
    version = await publish(client, lambda s: s["providers"]["glm"].update(enabled=False))
    result = await gateway.executor.generate(prepared)
    assert result.actual_provider == "deepseek"
    assert len(upstream.requests) == 1
    assert prepared.attempts[0]["safety_check_version"] == version
    assert prepared.context.config_version < version


async def test_circuit_filter_before_fixed_plan(client, basic, upstream):
    gateway = client.app.state.gateway
    for _ in range(20):
        await gateway.circuits.record("glm", "glm", False)
    result = await client.post("/v1/generate", json=basic)
    assert result.status_code == 200
    assert result.json()["actual_provider"] == "deepseek"
    row = await gateway.db.call(result.json()["call_id"], ["demo"])
    assert row["route_decisions"][0]["rejections"][0]["reason"] == "UPSTREAM_UNAVAILABLE"


async def test_bounded_backpressure_cancels_upstream(client_factory, basic):
    async with client_factory(stream_buffer_size=1, backpressure_timeout_s=0.01) as client:
        gateway = client.app.state.gateway
        version, snapshot = gateway.control.get()
        p = await gateway.prepare(
            GatewayRequest.model_validate(basic), authenticate(snapshot, version, "dev-app-key"), True
        )
        iterator = sse(gateway, p, client.app.state.settings, client.app.state.inflight)
        assert "event: delta" in await anext(iterator)
        await asyncio.sleep(0.06)
        await iterator.aclose()
        assert p.terminal == "cancelled"
        row = await gateway.db.call(p.context.call_id, ["demo"])
        assert row["status"] == "cancelled"
        assert row["attempts"][0]["cost"]["complete"] is False
        assert await gateway.quotas.redis.zcard(gateway.quotas.key("app:demo", "leases")) == 0


async def test_model_repair_once_is_a_visible_attempt(client, basic, upstream):
    await publish(client, lambda s: s["routes"]["assistant"].update(allow_model_repair=True))

    async def first_bad(request, payload):
        if len(upstream.requests) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "broken json"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 2},
                },
            )

    upstream.override = first_bad
    response = await client.post(
        "/v1/generate",
        json={
            **basic,
            "requirements": {"structured_output": "json_schema"},
            "output_schema": {"type": "object"},
            "execution": {"model_repair": True},
        },
    )
    assert response.status_code == 200, response.text
    assert len(upstream.requests) == 2
    assert upstream.requests[-1][1]["model"] == "mock-glm"
    assert "Do not invent" in json.dumps(upstream.requests[-1][1])
    call = (await client.get(f"/v1/calls/{response.json()['call_id']}")).json()
    assert len(call["attempts"]) == 2
    assert call["payload"]["model_repair_attempted"]


async def test_cost_budget_rejects_before_network(client, basic, upstream):
    response = await client.post("/v1/generate", json={**basic, "execution": {"max_cost": "0"}})
    assert response.json()["error"]["code"] == "COST_BUDGET_EXCEEDED"
    assert upstream.requests == []


async def test_key_rotation_and_readonly_credential(client, basic):
    version = client.app.state.gateway.control.version
    issued = await client.post(
        "/admin/v1/keys",
        headers=ADMIN,
        json={"base_version": version, "application_id": "demo", "reason": "rotation"},
    )
    assert issued.status_code == 200
    new = issued.json()
    headers = {"Authorization": "Bearer " + new["key"]}
    assert (await client.get("/v1/models", headers=headers)).status_code == 200
    assert (await client.get("/v1/models")).status_code == 200
    revoked = await client.post(
        "/admin/v1/keys/demo",
        headers=ADMIN,
        json={"base_version": new["version"], "enabled": False, "reason": "retire old key"},
    )
    assert revoked.status_code == 200
    assert (await client.get("/v1/models")).status_code == 401
    assert (await client.get("/v1/models", headers=headers)).status_code == 200
    policy = (await client.get("/admin/v1/policies/current", headers=ADMIN)).json()
    assert new["key"] not in json.dumps(policy)


async def test_shared_loop_round_limit_prevents_reference_replay(client, basic, tools, upstream):
    payload = {**basic, "tools": tools, "execution": {"max_tool_rounds": 2}}
    first = (await client.post("/v1/generate", json=payload)).json()
    second_payload = {
        **payload,
        "continuation": first["continuation"],
        "messages": [
            *basic["messages"],
            {"role": "assistant", "content": [], "tool_calls": first["tool_calls"]},
            {"role": "tool", "tool_call_id": "tc_1", "content": [{"type": "text", "text": "ok"}]},
        ],
    }
    second = await client.post("/v1/generate", json=second_payload)
    assert second.status_code == 200
    replay = await client.post("/v1/generate", json=second_payload)
    assert replay.json()["error"]["code"] == "TOOL_LOOP_LIMIT_EXCEEDED"
    assert len(upstream.requests) == 2


async def test_prompt_auth_and_variables_fail_before_upstream(client, upstream):
    bad = await client.post(
        "/v1/generate",
        json={
            "logical_model": "order_extraction",
            "prompt": {"name": "extract_order", "version": "1.0.0", "variables": {"order_text": 123}},
        },
    )
    assert bad.json()["error"]["code"] == "PROMPT_VARIABLE_INVALID"
    bad = await client.post(
        "/v1/generate",
        json={
            "logical_model": "assistant",
            "prompt": {"name": "extract_order", "version": "1.0.0", "variables": {"order_text": "R123"}},
        },
    )
    assert bad.json()["error"]["code"] == "ACCESS_DENIED"
    assert upstream.requests == []


async def test_live_ready_metrics_and_sdk_error(client, basic):
    assert (await client.get("/health/live")).status_code == 200
    assert (await client.get("/health/ready")).status_code == 200
    assert (await client.get("/metrics")).status_code == 401
    metrics = await client.get("/metrics", headers=ADMIN)
    assert metrics.status_code == 200
    assert "gateway_calls_total" in metrics.text
    from llm_gateway_sdk import GatewayClient, GatewaySDKError

    async with GatewayClient(
        "http://gateway", "bad-key", transport=httpx.ASGITransport(app=client.app)
    ) as sdk:
        with pytest.raises(GatewaySDKError) as exc:
            await sdk.generate(basic)
        assert exc.value.code == "AUTHENTICATION_FAILED"
