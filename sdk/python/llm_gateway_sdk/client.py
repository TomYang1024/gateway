from __future__ import annotations

import httpx
from llm_gateway_core.contracts import GatewayRequest, GatewayResponse, Message, StreamEvent, Text, ToolCall
from pydantic import BaseModel


class GatewaySDKError(Exception):
    def __init__(self, error, status):
        self.code, self.call_id = error.get("code"), error.get("call_id")
        self.status, self.details = status, error.get("details", {})
        super().__init__(error.get("message", self.code))


class GatewayClient:
    def __init__(self, base_url, api_key, *, timeout=610, transport=None):
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
            trust_env=False,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.http.aclose()

    def payload(self, request):
        return request.model_dump(mode="json") if isinstance(request, BaseModel) else dict(request)

    async def check(self, response):
        if response.is_error:
            await response.aread()
            raise GatewaySDKError(
                response.json().get("error", {"code": "INTERNAL_ERROR"}), response.status_code
            )

    async def generate(self, request: GatewayRequest | dict, *, output_type: type[BaseModel] | None = None):
        payload = self.payload(request)
        if output_type:
            payload["output_schema"] = output_type.model_json_schema()
            payload["requirements"] = {**payload.get("requirements", {}), "structured_output": "json_schema"}
        response = await self.http.post("v1/generate", json=payload)
        await self.check(response)
        result = GatewayResponse.model_validate_json(response.content)
        if output_type:
            result.structured_data = output_type.model_validate(result.structured_data, strict=True)
        return result

    async def stream(self, request: GatewayRequest | dict):
        async with self.http.stream("POST", "v1/stream", json=self.payload(request)) as response:
            await self.check(response)
            data = []
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    yield StreamEvent.model_validate_json("\n".join(data))
                    data = []

    async def models(self):
        response = await self.http.get("v1/models")
        await self.check(response)
        return response.json()

    async def call(self, call_id):
        response = await self.http.get(f"v1/calls/{call_id}")
        await self.check(response)
        return response.json()


def assistant_message(response):
    return Message(
        role="assistant",
        content=[Text(text=response.content)] if response.content else [],
        tool_calls=response.tool_calls,
    )


def tool_result(tool_call, output: str, *, is_error=False):
    return Message(
        role="tool",
        tool_call_id=tool_call.id,
        name=tool_call.name,
        content=[Text(text=output)],
        is_error=is_error,
    )


class StreamRound:
    """Collect only validated tool ends; applications execute tools after completed/tool_calls."""

    def __init__(self):
        self.content, self.tools, self.arguments = "", {}, {}
        self.continuation, self.status, self.finish_reason = None, None, None

    def consume(self, event):
        data = event.data
        if event.type == "delta":
            self.content += data["text"]
        elif event.type == "tool_call_start":
            self.tools[data["tool_call_id"]] = {"name": data["name"], "index": data["index"]}
            self.arguments[data["tool_call_id"]] = ""
        elif event.type == "tool_call_delta":
            self.arguments[data["tool_call_id"]] += data["arguments_delta"]
        elif event.type == "tool_call_end":
            self.tools[data["tool_call_id"]]["call"] = ToolCall(
                id=data["tool_call_id"],
                name=self.tools[data["tool_call_id"]]["name"],
                arguments=data["parsed_arguments"],
                raw_arguments=self.arguments[data["tool_call_id"]],
            )
        elif event.type == "terminal":
            self.status, self.finish_reason = data["status"], data.get("finish_reason")
            self.continuation = data.get("continuation")
        return event

    @property
    def assistant(self):
        if self.status != "completed":
            raise ValueError("Stream did not complete; tools cannot be executed")
        return Message(
            role="assistant",
            content=[Text(text=self.content)] if self.content else [],
            tool_calls=[t["call"] for t in sorted(self.tools.values(), key=lambda t: t["index"])],
        )
