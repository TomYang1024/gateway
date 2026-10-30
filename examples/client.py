"""Local mock gateway examples. The synthetic tool below has no business side effect."""

import asyncio

from llm_gateway_sdk import GatewayClient, StreamRound, tool_result
from pydantic import BaseModel


class Order(BaseModel):
    order_no: str


async def main():
    async with GatewayClient("http://127.0.0.1:8000", "dev-app-key") as gateway:
        order = await gateway.generate(
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
        print(order.call_id, order.structured_data)
        request = {
            "logical_model": "assistant",
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
        collected = StreamRound()
        async for event in gateway.stream(request):
            collected.consume(event)
        if collected.finish_reason == "tool_calls":
            assistant = collected.assistant
            second = await gateway.generate(
                {
                    **request,
                    "continuation": collected.continuation,
                    "messages": [
                        *request["messages"],
                        assistant.model_dump(mode="json"),
                        *[
                            tool_result(call, '{"synthetic_status":"shipped"}').model_dump(mode="json")
                            for call in assistant.tool_calls
                        ],
                    ],
                }
            )
            print(second.call_id, second.content)


if __name__ == "__main__":
    asyncio.run(main())
