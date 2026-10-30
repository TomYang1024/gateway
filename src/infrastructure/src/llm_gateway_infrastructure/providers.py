"""Three wire protocols; no retries, routing, SSE terminals or tool execution."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from llm_gateway_core.contracts import GatewayError, ModelResponse, ToolCall
from llm_gateway_core.security import fingerprint
from llm_gateway_core.usage import normalize_usage

PATHS = {
    "openai_chat": "/chat/completions",
    "openai_responses": "/responses",
    "anthropic_messages": "/messages",
}


def text(message):
    return "".join(p.text for p in message.content)


def wire_payload(connection, model, request, messages, parameters, streaming, continuation=None):
    protocol = connection.protocol
    opaque = {b["message_index"]: b["blocks"] for b in (continuation or [])}
    params = {connection.parameter_map.get(k, k): v for k, v in parameters.items()}
    max_output = params.pop("max_output_tokens", None)
    if max_output is not None:
        params[
            {
                "openai_chat": "max_tokens",
                "openai_responses": "max_output_tokens",
                "anthropic_messages": "max_tokens",
            }[protocol]
        ] = max_output
    payload = {"model": model.upstream_model, "stream": streaming, **params}
    if request.requirements.reasoning_level:
        if protocol == "openai_responses":
            payload["reasoning"] = {"effort": request.requirements.reasoning_level}
        elif protocol == "openai_chat":
            payload["reasoning_effort"] = request.requirements.reasoning_level
        else:
            payload["output_config"] = {"effort": request.requirements.reasoning_level}
    if protocol == "openai_chat":
        items = []
        for index, message in enumerate(messages):
            item = {"role": message.role, "content": text(message)}
            if message.role == "tool":
                item["tool_call_id"] = message.tool_call_id
                if message.name:
                    item["name"] = message.name
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": t.id,
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "arguments": t.raw_arguments or json.dumps(t.arguments, ensure_ascii=False),
                        },
                    }
                    for t in message.tool_calls
                ]
            for block in opaque.get(index, []):
                item.update(block)
            items.append(item)
        payload["messages"] = items
        if streaming:
            payload["stream_options"] = {"include_usage": True}
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in request.tools
            ]
            payload["tool_choice"] = request.tool_choice
            if request.requirements.parallel_tool_calls:
                payload["parallel_tool_calls"] = True
        if request.requirements.structured_output != "none":
            payload["response_format"] = {"type": request.requirements.structured_output}
            if request.output_schema:
                payload["response_format"]["json_schema"] = {
                    "name": "gateway_output",
                    "strict": True,
                    "schema": request.output_schema,
                }
    elif protocol == "openai_responses":
        items = []
        for index, message in enumerate(messages):
            if index in opaque:
                items.extend(opaque[index])  # exact output order, including encrypted reasoning items
                continue
            if message.role == "tool":
                items.append(
                    {"type": "function_call_output", "call_id": message.tool_call_id, "output": text(message)}
                )
                continue
            if message.content:
                items.append(
                    {
                        "role": message.role,
                        "content": [
                            {
                                "type": "output_text" if message.role == "assistant" else "input_text",
                                "text": text(message),
                            }
                        ],
                    }
                )
            for call in message.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": call.raw_arguments or json.dumps(call.arguments, ensure_ascii=False),
                    }
                )
        payload["input"] = items
        payload["store"] = False
        payload["include"] = ["reasoning.encrypted_content"]
        if request.tools:
            payload["tools"] = [
                {"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters}
                for t in request.tools
            ]
            payload["tool_choice"] = request.tool_choice
            if request.requirements.parallel_tool_calls:
                payload["parallel_tool_calls"] = True
        if request.requirements.structured_output != "none":
            form = {"type": request.requirements.structured_output}
            if request.output_schema:
                form.update(name="gateway_output", strict=True, schema=request.output_schema)
            payload["text"] = {"format": form}
    else:
        system, items = [], []
        for index, message in enumerate(messages):
            if message.role == "system":
                system.append({"type": "text", "text": text(message)})
                continue
            if message.role == "tool":
                item = {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.tool_call_id,
                            "content": text(message),
                            "is_error": message.is_error,
                        }
                    ],
                }
            else:
                blocks = opaque.get(index)
                if blocks is None:
                    blocks = [{"type": "text", "text": text(message)}] if message.content else []
                    blocks += [
                        {"type": "tool_use", "id": t.id, "name": t.name, "input": t.arguments}
                        for t in message.tool_calls
                    ]
                item = {"role": message.role, "content": blocks}
            if items and items[-1]["role"] == item["role"]:
                items[-1]["content"].extend(item["content"])
            else:
                items.append(item)
        payload.update(system=system, messages=items)
        if request.tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in request.tools
            ]
            payload["tool_choice"] = {
                "type": {"required": "any"}.get(request.tool_choice, request.tool_choice)
            }
            if request.requirements.parallel_tool_calls:
                payload["tool_choice"]["disable_parallel_tool_use"] = False
        if request.output_schema:
            payload.setdefault("output_config", {})["format"] = {
                "type": "json_schema",
                "schema": request.output_schema,
            }
        elif request.requirements.structured_output == "json_object":
            raise GatewayError("PARAMETER_UNSUPPORTED", "Messages does not expose native json_object")
    extras = request.extensions.get(connection.vendor, {})
    protected = {
        "model",
        "stream",
        "messages",
        "input",
        "system",
        "tools",
        "tool_choice",
        "response_format",
        "text",
        "output_config",
        "store",
        "include",
        "stream_options",
    } | set(payload)
    if set(extras) & protected:
        raise GatewayError("PARAMETER_UNSUPPORTED", "Extension cannot override protocol fields")
    payload.update(extras)
    return payload


def decode_response(protocol, body):
    if protocol == "openai_chat":
        choice = body["choices"][0]
        message = choice["message"]
        calls = [
            ToolCall(
                id=t["id"],
                name=t["function"]["name"],
                arguments=_arguments(t["function"]["arguments"]),
                raw_arguments=t["function"]["arguments"],
            )
            for t in message.get("tool_calls", [])
        ]
        opaque = [{k: message[k]} for k in ("reasoning_content", "reasoning") if k in message]
        content, finish = message.get("content") or "", choice.get("finish_reason")
    elif protocol == "openai_responses":
        if body.get("status") not in (None, "completed"):
            raise GatewayError("UPSTREAM_UNAVAILABLE", "Response did not complete")
        content, calls = "", []
        for item in body.get("output", []):
            if item["type"] == "message":
                content += "".join(
                    b.get("text", "") for b in item.get("content", []) if b["type"] == "output_text"
                )
            elif item["type"] == "function_call":
                calls.append(
                    ToolCall(
                        id=item["call_id"],
                        name=item["name"],
                        arguments=_arguments(item["arguments"]),
                        raw_arguments=item["arguments"],
                    )
                )
        opaque = body.get("output", [])
        finish = "tool_calls" if calls else "stop"
    else:
        content, calls = "", []
        for block in body.get("content", []):
            if block["type"] == "text":
                content += block["text"]
            elif block["type"] == "tool_use":
                calls.append(ToolCall(id=block["id"], name=block["name"], arguments=block["input"]))
        opaque, finish = body.get("content", []), body.get("stop_reason")
    if len(content.encode()) > 1048576 or len(calls) > 32:
        raise GatewayError("UPSTREAM_UNAVAILABLE", "Response exceeds gateway output limits")
    normalized_finish = (
        "tool_calls" if calls else {"end_turn": "stop", "max_tokens": "length"}.get(finish, finish or "stop")
    )
    return ModelResponse(
        content=content,
        tool_calls=calls,
        usage=normalize_usage(protocol, body.get("usage")),
        finish_reason=normalized_finish,
        raw_finish_reason=finish,
        provider_continuation=opaque,
        provider_request_id=body.get("id"),
    )


def _arguments(raw):
    from llm_gateway_core.structured import parse_json

    result = parse_json(raw, extract=False, code="TOOL_ARGUMENT_VALIDATION_FAILED")
    if not isinstance(result, dict):
        raise GatewayError("TOOL_ARGUMENT_VALIDATION_FAILED", "Tool arguments must be an object")
    return result


def upstream_error(response):
    status = response.status_code
    code, retryable = ("UPSTREAM_UNAVAILABLE", status >= 500)
    if status in (401, 403):
        code, retryable = "UPSTREAM_AUTH_FAILED", False
    elif status == 429:
        code, retryable = "UPSTREAM_RATE_LIMITED", True
    elif status in (400, 422):
        code, retryable = "PARAMETER_UNSUPPORTED", False
    delay = None
    value = response.headers.get("retry-after")
    if value:
        try:
            delay = max(0, float(value))
        except ValueError:
            try:
                delay = max(0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
            except (ValueError, TypeError):
                pass
    return GatewayError(
        code,
        "Upstream request failed",
        retryable=retryable,
        details={"upstream_http_status": status},
        retry_after=delay,
    )


async def decode_http_error(response):
    error = upstream_error(response)
    raw = bytearray()
    async for chunk in response.aiter_bytes():
        raw.extend(chunk)
        if len(raw) > 65536:
            break
    if len(raw) <= 65536:
        try:
            body = json.loads(raw)
            native = body.get("error") or body
            value = native.get("code") or native.get("type")
            if isinstance(value, (str, int)) and len(str(value)) <= 64:
                error.details["upstream_code"] = str(value)
        except (ValueError, AttributeError, TypeError):
            pass
    return error


async def read_sse(response):
    """HTTPX incremental UTF-8 line decoder handles arbitrary TCP and codepoint slicing."""
    data, event = [], "message"
    size = 0
    async for line in response.aiter_lines():
        size += len(line.encode())
        if size > 1048576:
            raise GatewayError("STREAM_INTERRUPTED", "Upstream event exceeds 1 MiB")
        if not line:
            if data:
                yield event, "\n".join(data)
            data, event, size = [], "message", 0
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif line.startswith("event:"):
            event = line[6:].strip()
    if data:
        yield event, "\n".join(data)


class ProtocolAdapter:
    def __init__(self, connection, client):
        self.connection, self.client = connection, client

    async def generate(self, model, request, messages, parameters, continuation=None):
        payload = wire_payload(self.connection, model, request, messages, parameters, False, continuation)
        try:
            async with self.client.stream("POST", PATHS[self.connection.protocol], json=payload) as response:
                if response.is_error:
                    raise await decode_http_error(response)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 2097152:
                        raise GatewayError("UPSTREAM_UNAVAILABLE", "Response exceeds 2 MiB")
            return decode_response(self.connection.protocol, json.loads(raw))
        except GatewayError as exc:
            if "raw" in locals():
                try:
                    exc.usage = normalize_usage(self.connection.protocol, json.loads(raw).get("usage"))
                except (ValueError, TypeError, AttributeError):
                    pass
            raise
        except httpx.TimeoutException as exc:
            raise GatewayError("UPSTREAM_TIMEOUT", retryable=True) from exc
        except httpx.TransportError as exc:
            raise GatewayError("UPSTREAM_UNAVAILABLE", retryable=True) from exc
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise GatewayError("UPSTREAM_UNAVAILABLE", "Malformed upstream response") from exc

    async def stream(self, model, request, messages, parameters, continuation=None):
        protocol = self.connection.protocol
        payload = wire_payload(self.connection, model, request, messages, parameters, True, continuation)
        normalizer = StreamNormalizer(protocol)
        try:
            async with self.client.stream("POST", PATHS[protocol], json=payload) as response:
                if response.is_error:
                    raise await decode_http_error(response)
                async for event, raw in read_sse(response):
                    if raw == "[DONE]":
                        normalizer.saw_done = True
                        break
                    body = json.loads(raw)
                    for item in normalizer.feed(event, body):
                        yield item
            if not normalizer.finished:
                raise GatewayError(
                    "STREAM_INTERRUPTED", "Upstream ended without a completion marker", retryable=True
                )
            yield {"kind": "end", "response": normalizer.result()}
        except GatewayError as exc:
            exc.usage = normalize_usage(protocol, normalizer.raw_usage)
            raise
        except httpx.TimeoutException as exc:
            raise GatewayError("UPSTREAM_TIMEOUT", retryable=True) from exc
        except httpx.TransportError as exc:
            raise GatewayError("STREAM_INTERRUPTED", retryable=True) from exc
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise GatewayError("STREAM_INTERRUPTED", "Malformed upstream stream") from exc


class StreamNormalizer:
    def __init__(self, protocol):
        self.protocol = protocol
        self.content, self.raw_usage, self.finish, self.request_id = "", {}, None, None
        self.finished, self.saw_done = False, False
        self.opaque, self.blocks, self.tool_indices = [], {}, set()
        self.reasoning = ""

    def feed(self, event, b):
        if (
            b.get("error")
            or event == "error"
            or b.get("type") in {"error", "response.failed", "response.incomplete"}
        ):
            raise GatewayError("UPSTREAM_UNAVAILABLE", "Upstream stream failed", retryable=True)
        result = []
        if self.protocol == "openai_chat":
            self.request_id = b.get("id", self.request_id)
            self.raw_usage.update(b.get("usage") or {})
            for choice in b.get("choices", []):
                if choice.get("index", 0) != 0:
                    raise GatewayError("STREAM_INTERRUPTED", "Multiple choices unsupported")
                delta = choice.get("delta", {})
                if delta.get("content"):
                    result.append({"kind": "text", "text": delta["content"]})
                self.reasoning += delta.get("reasoning_content") or ""
                for t in delta.get("tool_calls", []):
                    f = t.get("function", {})
                    result.append(
                        {
                            "kind": "tool_fragment",
                            "index": t["index"],
                            "id": t.get("id") or "",
                            "name": f.get("name") or "",
                            "arguments": f.get("arguments") or "",
                            "stable": not t.get("id") and not f.get("name"),
                        }
                    )
                    self.tool_indices.add(t["index"])
                if choice.get("finish_reason"):
                    self.finish, self.finished = choice["finish_reason"], True
                    result.extend({"kind": "tool_done", "index": i} for i in sorted(self.tool_indices))
        elif self.protocol == "openai_responses":
            kind = b.get("type", event)
            if kind == "response.output_text.delta":
                result.append({"kind": "text", "text": b["delta"]})
            elif kind == "response.output_item.added":
                item, i = b["item"], b["output_index"]
                if item["type"] == "function_call":
                    self.tool_indices.add(i)
                    result.append(
                        {
                            "kind": "tool_fragment",
                            "index": i,
                            "id": item["call_id"],
                            "name": item["name"],
                            "arguments": "",
                            "stable": True,
                        }
                    )
            elif kind == "response.function_call_arguments.delta":
                result.append(
                    {
                        "kind": "tool_fragment",
                        "index": b["output_index"],
                        "id": "",
                        "name": "",
                        "arguments": b["delta"],
                        "stable": True,
                    }
                )
            elif kind == "response.function_call_arguments.done":
                result.append({"kind": "tool_done", "index": b["output_index"]})
            elif kind == "response.completed":
                response = b["response"]
                if response.get("status") != "completed":
                    raise GatewayError("STREAM_INTERRUPTED")
                self.opaque, self.raw_usage = response.get("output", []), response.get("usage", {})
                self.request_id, self.finish, self.finished = response.get("id"), "stop", True
        else:
            kind = b.get("type", event)
            if kind == "message_start":
                self.raw_usage.update(b["message"].get("usage", {}))
                self.request_id = b["message"].get("id")
            elif kind == "content_block_start":
                block, i = dict(b["content_block"]), b["index"]
                self.blocks[i] = block
                if block["type"] == "tool_use":
                    self.tool_indices.add(i)
                    result.append(
                        {
                            "kind": "tool_fragment",
                            "index": i,
                            "id": block["id"],
                            "name": block["name"],
                            "arguments": "",
                            "stable": True,
                        }
                    )
                elif block["type"] == "text" and block.get("text"):
                    result.append({"kind": "text", "text": block["text"]})
            elif kind == "content_block_delta":
                i, delta = b["index"], b["delta"]
                if delta["type"] == "text_delta":
                    result.append({"kind": "text", "text": delta["text"]})
                    self.blocks[i]["text"] = self.blocks[i].get("text", "") + delta["text"]
                elif delta["type"] == "input_json_delta":
                    self.blocks[i]["_partial"] = self.blocks[i].get("_partial", "") + delta["partial_json"]
                    result.append(
                        {
                            "kind": "tool_fragment",
                            "index": i,
                            "id": "",
                            "name": "",
                            "arguments": delta["partial_json"],
                            "stable": True,
                        }
                    )
                elif delta["type"] in ("thinking_delta", "signature_delta"):
                    key = "thinking" if delta["type"] == "thinking_delta" else "signature"
                    self.blocks[i][key] = self.blocks[i].get(key, "") + delta[key]
            elif kind == "content_block_stop":
                i = b["index"]
                if i in self.tool_indices:
                    block = self.blocks[i]
                    block["input"] = _arguments(block.pop("_partial", "{}"))
                    result.append({"kind": "tool_done", "index": i})
            elif kind == "message_delta":
                self.raw_usage.update(b.get("usage", {}))
                self.finish = b.get("delta", {}).get("stop_reason", self.finish)
            elif kind == "message_stop":
                self.finished = self.finish is not None
                self.opaque = [self.blocks[i] for i in sorted(self.blocks)]
        for item in result:
            if item["kind"] == "text":
                self.content += item["text"]
        if len(self.content.encode()) + len(self.reasoning.encode()) + len(json.dumps(self.blocks)) > 2097152:
            raise GatewayError("STREAM_INTERRUPTED", "Stream exceeds output buffer limit")
        return result

    def result(self):
        if self.protocol == "openai_chat" and self.reasoning:
            self.opaque = [{"reasoning_content": self.reasoning}]
        finish = (
            "tool_calls"
            if self.tool_indices
            else {"end_turn": "stop", "max_tokens": "length"}.get(self.finish, self.finish)
        )
        return ModelResponse(
            content=self.content,
            usage=normalize_usage(self.protocol, self.raw_usage),
            finish_reason=finish or "stop",
            raw_finish_reason=self.finish,
            provider_request_id=self.request_id,
            provider_continuation=self.opaque,
        )


class ClientFactory:
    validate = staticmethod(wire_payload)

    def __init__(self, transport=None):
        self.clients = {}
        self.transport = transport

    def adapter(self, connection):
        credential = os.environ.get(connection.secret_ref[4:])
        if not credential:
            raise GatewayError("DEPENDENCY_UNAVAILABLE", "Provider secret reference unresolved")
        key = fingerprint([connection.model_dump(), fingerprint(credential)])
        if key not in self.clients:
            headers = dict(connection.extra_headers)
            if connection.protocol == "anthropic_messages":
                headers["anthropic-version"] = "2023-06-01"
            auth = connection.auth_type or (
                "api_key" if connection.protocol == "anthropic_messages" else "bearer"
            )
            if auth == "bearer":
                headers["authorization"] = f"Bearer {credential}"
            else:
                headers["x-api-key"] = credential
            self.clients[key] = httpx.AsyncClient(
                base_url=connection.base_url.rstrip("/") + "/",
                headers=headers,
                follow_redirects=False,
                timeout=httpx.Timeout(connection.read_idle_timeout_s, connect=connection.connect_timeout_s),
                limits=httpx.Limits(
                    max_connections=connection.max_connections,
                    max_keepalive_connections=connection.max_connections,
                ),
                transport=self.transport,
                trust_env=False,
            )
        return ProtocolAdapter(connection, self.clients[key])

    async def close(self):
        for client in self.clients.values():
            await client.aclose()
