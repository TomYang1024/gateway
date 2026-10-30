"""Exercise the local Compose mock stack over actual HTTP/TCP. No real supplier calls."""

import asyncio
import json
from pathlib import Path

import httpx
from llm_gateway_sdk import GatewayClient, StreamRound, assistant_message, tool_result


async def main():
    results = []
    async with httpx.AsyncClient() as http:
        ready = await http.get("http://127.0.0.1:8000/health/ready")
        ready.raise_for_status()
    async with GatewayClient("http://127.0.0.1:8000", "dev-app-key") as sdk:
        for vendor in ("glm", "deepseek", "kimi"):
            for streaming in (False, True):
                payload = {
                    "logical_model": f"test_{vendor}",
                    "messages": [{"role": "user", "content": [{"type": "text", "text": "查询订单R123"}]}],
                    "tools": [
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
                }
                if streaming:
                    collected = StreamRound()
                    async for event in sdk.stream(payload):
                        collected.consume(event)
                    assert collected.status == "completed"
                    assistant, continuation = collected.assistant, collected.continuation
                else:
                    first = await sdk.generate(payload)
                    assert first.actual_model.startswith("mock-")
                    assistant, continuation = assistant_message(first), first.continuation.model_dump()
                result = await sdk.generate(
                    {
                        **payload,
                        "continuation": continuation,
                        "messages": [
                            *payload["messages"],
                            assistant.model_dump(mode="json"),
                            *[
                                tool_result(call, "synthetic shipped").model_dump(mode="json")
                                for call in assistant.tool_calls
                            ],
                        ],
                    }
                )
                assert result.content == "工具结果已收到。"
                row = await sdk.call(result.call_id)
                assert row["status"] == "completed"
                assert len(row["attempts"]) == 1
                results.append(
                    {
                        "vendor": vendor,
                        "streaming": streaming,
                        "call_id": result.call_id,
                        "actual_model": result.actual_model,
                        "status": "passed",
                    }
                )
    await asyncio.to_thread(write_results, results)
    print(f"Container HTTP smoke: {len(results)} protocol/mode two-round cases passed")


def write_results(results):
    Path("test-results").mkdir(exist_ok=True)
    Path("test-results/container-smoke.json").write_text(json.dumps(results, indent=2))


asyncio.run(main())
