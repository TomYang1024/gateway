"""Frozen V1 HTTP and execution contracts. No HTTP framework dependencies."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True, allow_inf_nan=False)


class Text(Contract):
    type: Literal["text"] = "text"
    text: str = Field(max_length=262144)


class ToolCall(Contract):
    id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any]
    raw_arguments: str | None = None


class Message(Contract):
    role: Literal["system", "user", "assistant", "tool"]
    content: list[Text] = Field(default_factory=list, max_length=64)
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=32)
    tool_call_id: str | None = None
    name: str | None = None
    is_error: bool = False

    @model_validator(mode="after")
    def role_fields(self):
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool result requires tool_call_id")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant may contain tool_calls")
        if self.role != "tool" and (self.tool_call_id or self.is_error):
            raise ValueError("tool result fields require tool role")
        return self


class Tool(Contract):
    name: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_-]{0,127}$")
    description: str = Field(default="", max_length=4096)
    parameters: dict[str, Any]
    version: str = "1"


class PromptRef(Contract):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    variables: dict[str, Any] = Field(default_factory=dict)


class Requirements(Contract):
    tool_calling: bool = False
    parallel_tool_calls: bool = False
    structured_output: Literal["none", "json_schema", "json_object"] = "none"
    reasoning_level: str | None = None


class Execution(Contract):
    total_timeout_s: float = Field(default=60.0, gt=0, le=600)
    max_total_attempts: int = Field(default=4, ge=1, le=8)
    max_cost: str | None = Field(default=None, pattern=r"^\d+(\.\d+)?$")
    max_tool_rounds: int = Field(default=8, ge=1, le=32)
    tool_loop_timeout_s: float = Field(default=300.0, gt=0, le=1800)
    tool_loop_max_total_attempts: int = Field(default=32, ge=1, le=256)
    tool_loop_max_cost: str | None = Field(default=None, pattern=r"^\d+(\.\d+)?$")
    model_repair: bool = False


class Continuation(Contract):
    tool_loop_id: str
    state_ref: str


class BusinessContext(Contract):
    trace_id: str | None = Field(default=None, max_length=128)
    run_id: str | None = Field(default=None, max_length=128)
    step_id: str | None = Field(default=None, max_length=128)
    user_id: str | None = Field(default=None, max_length=128)
    task_type: str | None = Field(default=None, max_length=128)
    business_ref: str | None = Field(default=None, max_length=256)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=16)


class ResponseFormat(Contract):
    type: Literal["text", "json_object", "json_schema"]
    json_schema: dict[str, Any] | None = None


class GatewayRequest(Contract):
    logical_model: str = Field(
        min_length=1, max_length=128, validation_alias=AliasChoices("logical_model", "model")
    )
    stream: bool = False
    response_format: ResponseFormat | None = None
    messages: list[Message] | None = Field(default=None, min_length=1, max_length=256)
    prompt: PromptRef | None = None
    generation: dict[str, Any] = Field(default_factory=dict, max_length=16)
    requirements: Requirements = Field(default_factory=Requirements)
    tools: list[Tool] = Field(default_factory=list, max_length=32)
    tool_choice: Literal["auto", "none", "required"] = "auto"
    output_schema: dict[str, Any] | None = None
    execution: Execution = Field(default_factory=Execution)
    continuation: Continuation | None = None
    context: BusinessContext = Field(default_factory=BusinessContext)
    extensions: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def input_contract(self):
        if self.response_format:
            if self.requirements.structured_output != "none" or self.output_schema:
                raise ValueError("response_format conflicts with requirements.structured_output")
            if self.response_format.type != "text":
                self.requirements.structured_output = self.response_format.type
            if self.response_format.type == "json_schema":
                schema = (self.response_format.json_schema or {}).get("schema")
                if not isinstance(schema, dict):
                    raise ValueError("json_schema response_format requires json_schema.schema")
                self.output_schema = schema
        if (self.messages is None) == (self.prompt is None):
            raise ValueError("exactly one of messages or prompt is required")
        if len({t.name for t in self.tools}) != len(self.tools):
            raise ValueError("tool names must be unique")
        if self.requirements.structured_output == "json_schema" and not (self.output_schema or self.prompt):
            raise ValueError("json_schema requires output_schema")
        if self.output_schema and self.requirements.structured_output != "json_schema":
            raise ValueError("output_schema requires json_schema requirement")
        if self.tool_choice != "auto" and not self.tools:
            raise ValueError("tool_choice requires tools")
        if (
            not self.continuation
            and self.messages
            and any(m.tool_calls or m.role == "tool" for m in self.messages)
        ):
            raise ValueError("tool history requires continuation")
        return self


class Identity(Contract):
    team_id: str
    application_id: str
    api_key_id: str
    auth_policy_version: int


class CallContext(Identity):
    call_id: str = Field(default_factory=lambda: uid("call"))
    request_id: str = Field(default_factory=lambda: uid("req"))
    trace_id: str = Field(default_factory=lambda: uid("trace"))
    config_version: int
    quota_policy_version: int
    route_policy_version: int
    data_policy_version: int


class Usage(Contract):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    usage_source: Literal["provider", "estimated", "unknown"] = "unknown"
    usage_complete: bool = False
    raw_usage: dict[str, Any] = Field(default_factory=dict)


class Cost(Contract):
    amount: str | None = None
    known_amount: str = "0"
    currency: str
    pricing_version: str
    complete: bool = False
    items: dict[str, str] = Field(default_factory=dict)


class ModelResponse(Contract):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str = "stop"
    raw_finish_reason: str | None = None
    provider_request_id: str | None = None
    provider_continuation: list[dict[str, Any]] = Field(default_factory=list)


class GatewayResponse(Contract):
    call_id: str
    actual_provider: str
    actual_model: str
    content: str
    tool_calls: list[ToolCall]
    structured_data: Any = None
    usage: Usage
    cost: Cost
    finish_reason: str
    validation_result: str = "valid"
    continuation: Continuation | None = None


class StreamEvent(Contract):
    type: Literal["delta", "tool_call_start", "tool_call_delta", "tool_call_end", "usage", "terminal"]
    call_id: str
    sequence: int
    data: dict[str, Any]


HTTP_CODES = {
    "REQUEST_INVALID": 422,
    "AUTHENTICATION_FAILED": 401,
    "ACCESS_DENIED": 403,
    "PROMPT_NOT_FOUND": 404,
    "PROMPT_VARIABLE_INVALID": 422,
    "CONTEXT_BUDGET_EXCEEDED": 400,
    "NO_COMPATIBLE_MODEL": 503,
    "PARAMETER_UNSUPPORTED": 400,
    "UPSTREAM_AUTH_FAILED": 502,
    "UPSTREAM_RATE_LIMITED": 502,
    "UPSTREAM_TIMEOUT": 504,
    "UPSTREAM_UNAVAILABLE": 502,
    "STREAM_INTERRUPTED": 502,
    "OUTPUT_PARSE_FAILED": 502,
    "OUTPUT_VALIDATION_FAILED": 502,
    "ATTEMPT_BUDGET_EXCEEDED": 502,
    "COST_BUDGET_EXCEEDED": 429,
    "CALL_DEADLINE_EXCEEDED": 504,
    "CALL_CANCELLED": 499,
    "INTERNAL_ERROR": 500,
    "FEATURE_NOT_SUPPORTED": 400,
    "GATEWAY_RATE_LIMITED": 429,
    "TEAM_BUDGET_EXCEEDED": 429,
    "DATA_POLICY_DENIED": 403,
    "DEPENDENCY_UNAVAILABLE": 503,
    "TOOL_ARGUMENT_VALIDATION_FAILED": 502,
    "TOOL_RESULT_INVALID": 422,
    "CONTINUATION_EXPIRED": 409,
    "CONTINUATION_MISMATCH": 409,
    "TOOL_LOOP_LIMIT_EXCEEDED": 429,
    "CONFIG_CONFLICT": 409,
}


class GatewayError(Exception):
    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        retryable: bool = False,
        details: dict | None = None,
        retry_after: float | None = None,
    ):
        self.code, self.message = code, message or code
        self.retryable, self.details, self.retry_after = retryable, details or {}, retry_after
        self.call_id: str | None = None
        super().__init__(self.message)

    @property
    def status(self):
        return HTTP_CODES.get(self.code, 500)

    def body(self):
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "call_id": self.call_id,
                "retryable": self.retryable,
                "details": self.details,
            }
        }


def decimal(value: str) -> Decimal:
    return Decimal(value)
