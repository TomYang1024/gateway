import json

import httpx
import pytest
from conftest import Chunks
from llm_gateway_core.contracts import GatewayError, GatewayRequest, Message, Text
from llm_gateway_core.execution import ToolAssembler
from llm_gateway_infrastructure.providers import ClientFactory, decode_response, wire_payload

PROVIDERS = ["glm", "deepseek", "kimi"]


@pytest.mark.parametrize("auth_type,header", [("bearer", "authorization"), ("api_key", "x-api-key")])
async def test_authentication_is_independent_of_protocol(snapshot, upstream, monkeypatch, auth_type, header):
    monkeypatch.setenv("KIMI_API_KEY", "contract-test-key")
    connection = snapshot.providers["kimi"].model_copy(update={"auth_type": auth_type})
    factory = ClientFactory(httpx.MockTransport(upstream))
    try:
        adapter = factory.adapter(connection)
        assert header in adapter.client.headers
        assert adapter.client.headers["anthropic-version"] == "2023-06-01"
        assert ("x-api-key" if header == "authorization" else "authorization") not in adapter.client.headers
    finally:
        await factory.close()


async def test_upstream_machine_code_preserved_without_error_body(snapshot, basic, monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "contract-test-key")

    async def rejected(request):
        return httpx.Response(401, json={"error": {"code": "invalid_api_key", "message": "sensitive body"}})

    factory = ClientFactory(httpx.MockTransport(rejected))
    request = GatewayRequest.model_validate(basic)
    try:
        with pytest.raises(GatewayError) as error:
            await factory.adapter(snapshot.providers["glm"]).generate(
                snapshot.models["glm"], request, request.messages, {}
            )
        assert error.value.details["upstream_code"] == "invalid_api_key"
        assert "sensitive body" not in json.dumps(error.value.body())
    finally:
        await factory.close()


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_full_and_stream_contract(snapshot, provider, basic, tools, upstream, monkeypatch):
    for name in ("GLM", "DEEPSEEK", "KIMI"):
        monkeypatch.setenv(name + "_API_KEY", "test-key")
    factory = ClientFactory(httpx.MockTransport(upstream))
    adapter = factory.adapter(snapshot.providers[provider])
    req = GatewayRequest.model_validate({**basic, "tools": tools})
    model = snapshot.models[provider]
    try:
        response = await adapter.generate(model, req, req.messages, model.defaults)
        assert response.tool_calls[0].arguments == {"order_no": "R123"}
        assembler = ToolAssembler(req.tools)
        last = None
        async for item in adapter.stream(model, req, req.messages, model.defaults):
            if item["kind"] in {"tool_fragment", "tool_done"}:
                assembler.feed(item)
            if item["kind"] == "end":
                last = item["response"]
        assert assembler.calls()[0].arguments == response.tool_calls[0].arguments
        assert last.usage.input_tokens == 20
        assert last.provider_continuation == response.provider_continuation
        assert last.finish_reason == "tool_calls"
        assert upstream.requests[0][0].url.path.startswith("/v1/")
    finally:
        await factory.close()


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_text_utf8_arbitrary_tcp_chunks(snapshot, provider, basic, upstream, monkeypatch):
    monkeypatch.setenv(snapshot.providers[provider].secret_ref[4:], "secret")
    factory = ClientFactory(httpx.MockTransport(upstream))
    adapter = factory.adapter(snapshot.providers[provider])
    req = GatewayRequest.model_validate(basic)
    output = []
    try:
        async for item in adapter.stream(
            snapshot.models[provider], req, req.messages, {"max_output_tokens": 100}
        ):
            if item["kind"] == "text":
                output.append(item["text"])
        assert json.loads("".join(output)) == {"order_no": "R123"}
    finally:
        await factory.close()


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_no_completion_marker_is_failure(snapshot, provider, basic, upstream, monkeypatch):
    monkeypatch.setenv(snapshot.providers[provider].secret_ref[4:], "secret")

    async def broken(request, payload):
        return httpx.Response(200, stream=Chunks(b"event: ping\ndata: {}\n\n"))

    upstream.override = broken
    factory = ClientFactory(httpx.MockTransport(upstream))
    req = GatewayRequest.model_validate(basic)
    try:
        with pytest.raises(GatewayError) as exc:
            async for _ in factory.adapter(snapshot.providers[provider]).stream(
                snapshot.models[provider], req, req.messages, {}
            ):
                pass
        assert exc.value.code == "STREAM_INTERRUPTED"
    finally:
        await factory.close()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_wire_tool_result_and_opaque_roundtrip(snapshot, basic, tools, provider):
    from llm_gateway_api.mock_upstream import fixture

    req = GatewayRequest.model_validate({**basic, "tools": tools})
    connection, model = snapshot.providers[provider], snapshot.models[provider]
    payload = wire_payload(connection, model, req, req.messages, model.defaults, False)
    response = decode_response(connection.protocol, fixture(connection.protocol, payload))
    assistant = Message(role="assistant", content=[], tool_calls=response.tool_calls)
    result = Message(
        role="tool", tool_call_id="tc_1", name="lookup_order", content=[Text(text="ok")], is_error=True
    )
    second = wire_payload(
        connection,
        model,
        req,
        [*req.messages, assistant, result],
        model.defaults,
        False,
        [{"message_index": 1, "blocks": response.provider_continuation}],
    )
    wire = json.dumps(second)
    assert "opaque-mock" in wire
    assert "tc_1" in wire
    if provider == "kimi":
        assert "signed-mock" in wire
        assert second["messages"][-1]["content"][0]["is_error"] is True
