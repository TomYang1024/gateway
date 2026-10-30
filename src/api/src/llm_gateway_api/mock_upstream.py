"""Deterministic three-protocol upstream for local development, never a provider substitute."""

import json

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()


@app.get("/health")
async def health():
    return {"status": "mock"}


def fixture(protocol, payload):
    is_tool_result = (
        any(m.get("role") == "tool" for m in payload.get("messages", []))
        or any(i.get("type") == "function_call_output" for i in payload.get("input", []))
        or any(
            b.get("type") == "tool_result"
            for m in payload.get("messages", [])
            for b in m.get("content", [])
            if isinstance(b, dict)
        )
    )
    tools = payload.get("tools") or []
    tool = tools[0].get("function", tools[0]) if tools else None
    call_tool = tool and not is_tool_result and payload.get("tool_choice") not in ("none", {"type": "none"})
    content = "工具结果已收到。" if is_tool_result else '{"order_no":"R123"}'
    args = json.dumps({"order_no": "R123"})
    if protocol == "openai_chat":
        message = {"role": "assistant", "content": "" if call_tool else content}
        if call_tool:
            message.update(
                tool_calls=[
                    {"id": "tc_1", "type": "function", "function": {"name": tool["name"], "arguments": args}}
                ],
                reasoning_content="opaque-mock-reasoning",
            )
        return {
            "id": "mock-chat",
            "choices": [
                {"index": 0, "message": message, "finish_reason": "tool_calls" if call_tool else "stop"}
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
        }
    if protocol == "openai_responses":
        output = (
            [
                {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque-mock", "summary": []},
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "tc_1",
                    "name": tool["name"],
                    "arguments": args,
                },
            ]
            if call_tool
            else [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content}],
                }
            ]
        )
        return {
            "id": "mock-responses",
            "status": "completed",
            "output": output,
            "usage": {"input_tokens": 20, "output_tokens": 10},
        }
    blocks = (
        [
            {"type": "thinking", "thinking": "opaque-mock", "signature": "signed-mock"},
            {"type": "tool_use", "id": "tc_1", "name": tool["name"], "input": json.loads(args)},
        ]
        if call_tool
        else [{"type": "text", "text": content}]
    )
    return {
        "id": "mock-messages",
        "content": blocks,
        "stop_reason": "tool_use" if call_tool else "end_turn",
        "usage": {"input_tokens": 20, "output_tokens": 10},
    }


def stream_fixture(protocol, body):
    if protocol == "openai_chat":
        message = body["choices"][0]["message"]
        if message.get("reasoning_content"):
            yield "message", {"choices": [{"delta": {"reasoning_content": message["reasoning_content"]}}]}
        for char in message["content"]:
            yield "message", {"choices": [{"delta": {"content": char}}]}
        for index, tool in enumerate(message.get("tool_calls", [])):
            yield (
                "message",
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": index,
                                        "id": tool["id"],
                                        "function": {"name": tool["function"]["name"], "arguments": ""},
                                    }
                                ]
                            }
                        }
                    ]
                },
            )
            for char in tool["function"]["arguments"]:
                yield (
                    "message",
                    {
                        "choices": [
                            {"delta": {"tool_calls": [{"index": index, "function": {"arguments": char}}]}}
                        ]
                    },
                )
        yield (
            "message",
            {
                "choices": [{"delta": {}, "finish_reason": body["choices"][0]["finish_reason"]}],
                "usage": body["usage"],
            },
        )
    elif protocol == "openai_responses":
        for i, item in enumerate(body["output"]):
            yield (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": i,
                    "item": {**item, "arguments": ""} if item["type"] == "function_call" else item,
                },
            )
            if item["type"] == "function_call":
                for char in item["arguments"]:
                    yield (
                        "response.function_call_arguments.delta",
                        {"type": "response.function_call_arguments.delta", "output_index": i, "delta": char},
                    )
                yield (
                    "response.function_call_arguments.done",
                    {"type": "response.function_call_arguments.done", "output_index": i},
                )
            elif item["type"] == "message":
                for block in item["content"]:
                    for char in block["text"]:
                        yield (
                            "response.output_text.delta",
                            {"type": "response.output_text.delta", "delta": char},
                        )
        yield "response.completed", {"type": "response.completed", "response": body}
    else:
        yield (
            "message_start",
            {"type": "message_start", "message": {"id": body["id"], "usage": {"input_tokens": 20}}},
        )
        for i, block in enumerate(body["content"]):
            start = (
                {**block, "text": ""}
                if block["type"] == "text"
                else {**block, "input": {}}
                if block["type"] == "tool_use"
                else block
            )
            yield "content_block_start", {"type": "content_block_start", "index": i, "content_block": start}
            if block["type"] in ("text", "tool_use"):
                source = block.get("text") if block["type"] == "text" else json.dumps(block["input"])
                for char in source:
                    delta = (
                        {"type": "text_delta", "text": char}
                        if block["type"] == "text"
                        else {"type": "input_json_delta", "partial_json": char}
                    )
                    yield "content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta}
            yield "content_block_stop", {"type": "content_block_stop", "index": i}
        yield (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": body["stop_reason"]},
                "usage": {"output_tokens": 10},
            },
        )
        yield "message_stop", {"type": "message_stop"}


@app.post("/v1/{endpoint:path}")
async def generate(endpoint: str, request: Request):
    protocol = {
        "chat/completions": "openai_chat",
        "responses": "openai_responses",
        "messages": "anthropic_messages",
    }[endpoint]
    payload = await request.json()
    body = fixture(protocol, payload)
    if not payload.get("stream"):
        return body

    async def events():
        for event, item in stream_fixture(protocol, body):
            yield f"event: {event}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
