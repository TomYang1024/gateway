"""Opt-in paid acceptance, never enabled by normal test runs or repository defaults."""

import json
import os
from decimal import Decimal

import pytest
from llm_gateway_sdk import GatewayClient, assistant_message, tool_result

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("GATEWAY_LIVE_AUTHORIZED") != "1",
        reason="Real credentials, approved models and paid test budget required",
    ),
]


def parameters(vendor):
    routes = json.loads(os.environ["GATEWAY_LIVE_ROUTES"])
    total = Decimal(os.environ["GATEWAY_LIVE_TOTAL_COST"])
    assert total.is_finite() and total > 0
    return {
        "logical_model": routes[vendor],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Reply with a short greeting."}]}],
        "generation": {"max_output_tokens": 256},
        "execution": {"max_total_attempts": 1, "max_cost": format(total / 9, "f")},
    }


@pytest.mark.parametrize("vendor", ["glm", "deepseek", "kimi"])
async def test_real_text(vendor):
    async with GatewayClient(os.environ["GATEWAY_LIVE_URL"], os.environ["GATEWAY_LIVE_KEY"]) as sdk:
        response = await sdk.generate(parameters(vendor))
        assert response.content
        assert response.usage.usage_source == "provider"


@pytest.mark.parametrize("vendor", ["glm", "deepseek", "kimi"])
async def test_real_two_round_synthetic_tool(vendor):
    payload = parameters(vendor)
    payload.update(
        tools=[
            {
                "name": "lookup_order",
                "parameters": {
                    "type": "object",
                    "properties": {"order_no": {"type": "string"}},
                    "required": ["order_no"],
                    "additionalProperties": False,
                },
            }
        ],
        tool_choice="required",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Call lookup_order with order_no R123. Then summarize the supplied result.",
                    }
                ],
            }
        ],
    )
    async with GatewayClient(os.environ["GATEWAY_LIVE_URL"], os.environ["GATEWAY_LIVE_KEY"]) as sdk:
        first = await sdk.generate(payload)
        assert first.tool_calls and first.continuation
        history = assistant_message(first)
        # This synthetic result performs no business tool call and has no external side effect.
        second = await sdk.generate(
            {
                **payload,
                "tool_choice": "auto",
                "continuation": first.continuation.model_dump(),
                "messages": [
                    *payload["messages"],
                    history.model_dump(mode="json"),
                    *[
                        tool_result(call, '{"synthetic_status":"shipped"}').model_dump(mode="json")
                        for call in first.tool_calls
                    ],
                ],
            }
        )
        assert second.content
        assert second.actual_provider == first.actual_provider
        assert second.actual_model == first.actual_model
