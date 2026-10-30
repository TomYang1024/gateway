import asyncio
import json
import time

import httpx
import pytest
from conftest import Chunks, sse_bytes
from llm_gateway_core.config import secret_hash
from llm_gateway_core.contracts import GatewayRequest
from llm_gateway_core.security import authenticate
from llm_gateway_infrastructure.storage import events
from llm_gateway_sdk import GatewayClient, StreamRound, assistant_message, tool_result
from pydantic import BaseModel
from sqlalchemy import select

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer dev-admin-key"}


def parse_events(response):
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]


async def publish(client, mutate):
    result = (await client.get("/admin/v1/policies/current", headers=ADMIN)).json()
    mutate(result["snapshot"])
    response = await client.post(
        "/admin/v1/policies",
        headers=ADMIN,
        json={
            "base_version": result["version"],
            "snapshot": result["snapshot"],
            "reason": "integration test",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["version"]


@pytest.mark.parametrize("provider", ["glm", "deepseek", "kimi"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_two_round_tool_loop(client, basic, tools, provider, streaming, upstream):
    payload = {**basic, "logical_model": f"test_{provider}", "tools": tools, "context": {"run_id": "run-1"}}
    async with GatewayClient(
        "http://gateway", "dev-app-key", transport=httpx.ASGITransport(app=client.app)
    ) as sdk:
        if streaming:
            collected = StreamRound()
            event_list = []
            async for event in sdk.stream(payload):
                collected.consume(event)
                event_list.append(event)
            assert [e.sequence for e in event_list] == list(range(1, len(event_list) + 1))
            assert sum(e.type == "terminal" for e in event_list) == 1
            assert collected.status == "completed", event_list[-1]
            assistant, continuation = collected.assistant, collected.continuation
        else:
            first = await sdk.generate(payload)
            assert first.finish_reason == "tool_calls"
            assistant, continuation = assistant_message(first), first.continuation.model_dump()
        result = tool_result(assistant.tool_calls[0], "已发货")
        second = await sdk.generate(
            {
                **payload,
                "continuation": continuation,
                "messages": [
                    *payload["messages"],
                    assistant.model_dump(mode="json"),
                    result.model_dump(mode="json"),
                ],
            }
        )
        assert second.content == "工具结果已收到。"
        assert second.continuation is None
    assert len(upstream.requests) == 2
    wire = json.dumps(upstream.requests[-1][1], ensure_ascii=False)
    assert "opaque-mock" in wire
    call = await client.get(f"/v1/calls/{second.call_id}")
    assert call.status_code == 200
    assert call.json()["payload"]["api_key_id"] == "demo"
    assert call.json()["attempts"][0]["safety_check_version"] >= 1
    assert "工具结果" not in json.dumps(call.json(), ensure_ascii=False)


async def test_prompt_structured_and_pydantic_sdk(client):
    class Order(BaseModel):
        order_no: str

    async with GatewayClient(
        "http://gateway", "dev-app-key", transport=httpx.ASGITransport(app=client.app)
    ) as sdk:
        response = await sdk.generate(
            {
                "logical_model": "order_extraction",
                "prompt": {
                    "name": "extract_order",
                    "version": "1.0.0",
                    "variables": {"order_text": "订单R123"},
                },
            },
            output_type=Order,
        )
        assert isinstance(response.structured_data, Order)
        assert response.structured_data.order_no == "R123"
        call = await sdk.call(response.call_id)
        assert call["payload"]["prompt_hash"]
        assert call["payload"]["prompt_version"] == "1.0.0"


async def test_auth_context_and_preflight(client, basic, upstream):
    assert (
        await client.post("/v1/generate", json=basic, headers={"Authorization": "Bearer bad"})
    ).status_code == 401
    assert (await client.get("/admin/v1/policies/current")).status_code == 401
    assert (
        await client.post("/v1/generate", json={**basic, "context": {"team_id": "evil"}})
    ).status_code == 422
    stream = await client.post(
        "/v1/stream",
        json={
            **basic,
            "requirements": {"structured_output": "json_schema"},
            "output_schema": {"type": "object"},
        },
    )
    assert stream.status_code == 400
    assert stream.json()["error"]["code"] == "FEATURE_NOT_SUPPORTED"
    assert upstream.requests == []


async def test_retry_fallback_and_unknown_cost(client, basic, upstream):
    await publish(
        client, lambda s: s["routes"]["assistant"].update(max_retries_per_candidate=1, initial_delay_s=0)
    )

    async def fail_glm(request, payload):
        if payload["model"] == "mock-glm":
            return httpx.Response(503)

    upstream.override = fail_glm
    response = await client.post("/v1/generate", json=basic)
    assert response.status_code == 200, response.text
    assert response.json()["actual_provider"] == "deepseek"
    assert response.json()["cost"]["amount"] is None
    assert response.json()["cost"]["complete"] is False
    call = (await client.get(f"/v1/calls/{response.json()['call_id']}")).json()
    assert len(call["attempts"]) == 3
    assert all(a["api_key_id"] == "demo" for a in call["attempts"])
    assert len({a["attempt_id"] for a in call["attempts"]}) == 3


async def test_post_commit_failure_never_retries(client, basic, upstream):
    async def partial(request, payload):
        return httpx.Response(
            200,
            stream=Chunks(
                sse_bytes([("message", {"choices": [{"delta": {"content": "partial"}}]})]),
                error=httpx.ReadError("secret-body"),
            ),
        )

    upstream.override = partial
    response = await client.post("/v1/stream", json=basic)
    assert response.status_code == 200
    event_list = parse_events(response)
    assert [e["type"] for e in event_list] == ["delta", "terminal"]
    assert event_list[-1]["data"]["status"] == "failed"
    assert event_list[-1]["data"]["partial"] is True
    assert len(upstream.requests) == 1
    assert "secret-body" not in response.text


async def test_tool_start_commits_before_arguments(client, basic, tools, upstream):
    async def partial(request, payload):
        return httpx.Response(
            200,
            stream=Chunks(
                sse_bytes(
                    [
                        (
                            "response.output_item.added",
                            {
                                "type": "response.output_item.added",
                                "output_index": 0,
                                "item": {"type": "function_call", "call_id": "tc_1", "name": "lookup_order"},
                            },
                        )
                    ]
                )
            ),
        )

    upstream.override = partial
    response = await client.post(
        "/v1/stream", json={**basic, "logical_model": "test_deepseek", "tools": tools}
    )
    event_list = parse_events(response)
    assert event_list[0]["type"] == "tool_call_start"
    assert event_list[-1]["data"]["status"] == "failed"
    assert len(upstream.requests) == 1


async def test_stream_precommit_fallback(client, basic, upstream):
    await publish(client, lambda s: s["routes"]["assistant"].update(max_retries_per_candidate=0))

    async def fail(request, payload):
        if payload["model"] == "mock-glm":
            return httpx.Response(503)

    upstream.override = fail
    response = await client.post("/v1/stream", json=basic)
    event_list = parse_events(response)
    assert event_list[-1]["data"]["status"] == "completed", response.text
    assert event_list[-1]["data"]["actual_provider"] == "deepseek"
    assert len(upstream.requests) == 2


async def test_deadline_attempt_limit_and_retry_after(client, basic, upstream):
    async def rate_limit(request, payload):
        return httpx.Response(429, headers={"Retry-After": "10"})

    upstream.override = rate_limit
    response = await client.post("/v1/generate", json={**basic, "execution": {"total_timeout_s": 0.1}})
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "CALL_DEADLINE_EXCEEDED"
    assert len(upstream.requests) == 1


async def test_attempt_budget(client, basic, upstream):
    async def broken(request, payload):
        return httpx.Response(503)

    upstream.override = broken
    response = await client.post("/v1/generate", json={**basic, "execution": {"max_total_attempts": 1}})
    assert response.json()["error"]["code"] == "ATTEMPT_BUDGET_EXCEEDED"
    assert len(upstream.requests) == 1


async def test_invalid_json_no_fallback(client, basic, upstream):
    async def broken(request, payload):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"order_no":1}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 2},
            },
        )

    upstream.override = broken
    response = await client.post(
        "/v1/generate",
        json={
            **basic,
            "requirements": {"structured_output": "json_schema"},
            "output_schema": {
                "type": "object",
                "properties": {"order_no": {"type": "string"}},
                "required": ["order_no"],
            },
        },
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "OUTPUT_VALIDATION_FAILED"
    assert len(upstream.requests) == 1


async def test_continuation_cannot_fallback_or_omit_results(client, basic, tools, upstream):
    first = (await client.post("/v1/generate", json={**basic, "tools": tools})).json()
    assistant = {"role": "assistant", "content": [], "tool_calls": first["tool_calls"]}
    payload = {
        **basic,
        "tools": tools,
        "messages": [*basic["messages"], assistant],
        "continuation": first["continuation"],
    }
    missing = await client.post("/v1/generate", json=payload)
    assert missing.json()["error"]["code"] == "TOOL_RESULT_INVALID"
    payload["messages"].append(
        {"role": "tool", "tool_call_id": "tc_1", "content": [{"type": "text", "text": "ok"}]}
    )
    await publish(client, lambda s: s["routes"]["assistant"].update(max_retries_per_candidate=0))

    async def broken(request, body):
        return httpx.Response(503)

    upstream.override = broken
    response = await client.post("/v1/generate", json=payload)
    assert response.status_code == 502
    assert len(upstream.requests) == 2
    assert all(body["model"] == "mock-glm" for _, body in upstream.requests)


async def test_isolation_reports_keys_and_calls(client, basic):
    result = (await client.post("/v1/generate", json={**basic, "context": {"business_ref": "R123"}})).json()

    def mutate(s):
        s["teams"]["other"] = {}
        s["applications"]["other"] = {"team_id": "other", "use_cases": ["assistant"]}
        s["keys"]["other"] = {"application_id": "other", "digest": secret_hash("other-key")}
        s["admins"]["reader"] = {
            "actor": "reporter",
            "digest": secret_hash("reader-key"),
            "role": "team_report_reader",
            "teams": ["other"],
            "expires_at": time.time() + 600,
        }

    await publish(client, mutate)
    other = {"Authorization": "Bearer other-key"}
    assert (await client.get(f"/v1/calls/{result['call_id']}", headers=other)).status_code == 403
    assert (await client.get("/v1/calls?business_ref=R123", headers=other)).json()["calls"] == []
    assert (await client.get("/v1/reports/usage", headers=other)).json()["rows"] == []
    reader = {"Authorization": "Bearer reader-key"}
    assert (await client.get("/admin/v1/reports/usage?team_id=demo", headers=reader)).status_code == 403
    assert (await client.get("/admin/v1/reports/usage", headers=reader)).json()["rows"] == []
    assert (await client.get("/admin/v1/policies/current", headers=reader)).status_code == 403
    assert (await client.get("/v1/reports/usage")).json()["rows"][0]["calls"] == 1


async def test_hot_disable_and_key_revocation_before_attempt(client, basic, upstream):
    gateway = client.app.state.gateway
    version, snapshot = gateway.control.get()
    who = authenticate(snapshot, version, "dev-app-key")
    prepared = await gateway.prepare(GatewayRequest.model_validate(basic), who)
    await publish(client, lambda s: s["keys"]["demo"].update(enabled=False))
    with pytest.raises(Exception) as exc:
        await gateway.executor.generate(prepared)
    assert exc.value.code == "ACCESS_DENIED"
    assert upstream.requests == []
    assert (await client.post("/v1/generate", json=basic)).status_code == 401


async def test_policy_cas_rollback_and_two_instance_convergence(client, client_factory):
    initial = (await client.get("/admin/v1/policies/current", headers=ADMIN)).json()
    async with client_factory() as second:
        new = await publish(client, lambda s: s["providers"]["glm"].update(enabled=False))
        await second.app.state.gateway.control.refresh()
        assert second.app.state.gateway.control.version == new
        stale = await client.post(
            "/admin/v1/policies",
            headers=ADMIN,
            json={"base_version": initial["version"], "snapshot": initial["snapshot"], "reason": "stale"},
        )
        assert stale.status_code == 409
        rollback = await client.post(
            "/admin/v1/policies/rollback",
            headers=ADMIN,
            json={"base_version": new, "target_version": initial["version"], "reason": "restore"},
        )
        assert rollback.status_code == 200
        assert rollback.json()["version"] > new
        assert client.app.state.gateway.control.get()[1].providers["glm"].enabled


async def test_cancel_closes_attempt_and_releases_lease(client, basic, upstream):
    entered = asyncio.Event()

    async def stall(request, payload):
        entered.set()
        await asyncio.sleep(10)

    upstream.override = stall
    gateway = client.app.state.gateway
    version, snapshot = gateway.control.get()
    who = authenticate(snapshot, version, "dev-app-key")
    prepared = await gateway.prepare(GatewayRequest.model_validate(basic), who)
    task = asyncio.create_task(gateway.executor.generate(prepared))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert prepared.terminal == "cancelled"
    key = gateway.quotas.key("app:demo", "leases")
    assert await gateway.quotas.redis.zcard(key) == 0
    async with gateway.db.engine.connect() as c:
        terminal = (await c.execute(select(events).where(events.c.call_id == prepared.context.call_id))).all()
    assert len(terminal) == 1
    row = await gateway.db.call(prepared.context.call_id, ["demo"])
    assert row["attempts"][0]["status"] == "cancelled"


async def test_manual_prompt_refresh_job(client):
    response = await client.post("/admin/v1/prompts/refresh", headers=ADMIN)
    assert response.status_code == 202
    ident = response.json()["job_id"]
    for _ in range(20):
        job = await client.get(f"/admin/v1/prompts/refresh/{ident}", headers=ADMIN)
        if job.json()["status"] != "pending":
            break
        await asyncio.sleep(0.01)
    assert job.json()["status"] == "completed"
