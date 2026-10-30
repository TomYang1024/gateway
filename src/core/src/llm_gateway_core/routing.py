from __future__ import annotations

import json
import math
from dataclasses import dataclass

from llm_gateway_core.contracts import GatewayError


@dataclass
class PlannedCandidate:
    provider_id: str
    model_id: str
    connection: object
    model: object
    parameters: dict
    input_tokens: int
    reserved_tokens: int


def estimate_input(request, messages, provider_continuation=None):
    # Deliberately conservative generic byte estimate. Not reported as actual provider usage.
    encoded = json.dumps(
        {
            "messages": [m.model_dump() for m in messages],
            "tools": [t.model_dump() for t in request.tools],
            "schema": request.output_schema,
            "continuation": provider_continuation,
        },
        ensure_ascii=False,
    ).encode()
    return len(encoded) + 64  # one token per UTF-8 byte plus protocol margin


def constrain(parameters, constraints, error="PARAMETER_UNSUPPORTED"):
    for key, value in parameters.items():
        if type(value) is float and not math.isfinite(value):
            raise GatewayError(error, "Parameter must be finite", details={"parameter": key})
        if key not in constraints:
            raise GatewayError(error, "Parameter not supported", details={"parameter": key})
        rule = constraints[key]
        kind = rule.get("type")
        valid = {
            "integer": type(value) is int,
            "number": type(value) in (int, float),
            "string": isinstance(value, str),
            "array": isinstance(value, list),
            "boolean": type(value) is bool,
        }
        if kind and not valid.get(kind, False):
            raise GatewayError(error, "Parameter has invalid type", details={"parameter": key})
        if "enum" in rule and value not in rule["enum"]:
            raise GatewayError(error, "Parameter value unsupported", details={"parameter": key})
        if ("minimum" in rule and value < rule["minimum"]) or ("maximum" in rule and value > rule["maximum"]):
            raise GatewayError(error, "Parameter exceeds allowed range", details={"parameter": key})
        if isinstance(value, list) and (
            len(value) > rule.get("maxItems", 16)
            or any(not isinstance(v, str) or len(v) > 1024 for v in value)
        ):
            raise GatewayError(error, "Invalid stop list", details={"parameter": key})


def plan(snapshot, request, messages, app, streaming=False, prompt_defaults=None, pinned=None, blocked=None):
    route = snapshot.routes[request.logical_model]
    if set(request.generation) - set(app.allowed_parameters):
        raise GatewayError("ACCESS_DENIED", "Caller may not override these parameters")
    if app.parameter_limits:
        constrain(
            {k: v for k, v in request.generation.items() if k in app.parameter_limits}, app.parameter_limits
        )
    estimated = estimate_input(request, messages, pinned.get("provider_continuation") if pinned else None)
    accepted, rejected = [], []
    for candidate in route.candidates:
        provider, model = snapshot.providers[candidate.provider], snapshot.models[candidate.model]
        try:
            if pinned and (
                candidate.provider != pinned["provider_id"] or candidate.model != pinned["model_id"]
            ):
                continue
            if blocked and (candidate.provider, candidate.model) in blocked:
                raise GatewayError("UPSTREAM_UNAVAILABLE", "Candidate circuit is unavailable")
            if (
                not provider.enabled
                or provider.region not in route.allowed_regions
                or provider.vendor not in route.allowed_vendors
                or route.data_level not in provider.data_levels
            ):
                raise GatewayError("DATA_POLICY_DENIED")
            caps = model.capabilities
            if streaming and not caps.streaming:
                raise GatewayError("FEATURE_NOT_SUPPORTED", "Streaming unavailable")
            if request.tools or request.requirements.tool_calling:
                if not (caps.tool_calling and caps.tool_loop and caps.reasoning_roundtrip):
                    raise GatewayError("FEATURE_NOT_SUPPORTED", "Tool loop not verified")
                if streaming and not caps.streaming_tool_calls:
                    raise GatewayError("FEATURE_NOT_SUPPORTED", "Tool streaming unavailable")
            if request.requirements.parallel_tool_calls and not caps.parallel_tool_calls:
                raise GatewayError("FEATURE_NOT_SUPPORTED", "Parallel tools unavailable")
            if (
                request.requirements.structured_output != "none"
                and request.requirements.structured_output not in caps.structured_output_modes
            ):
                raise GatewayError("FEATURE_NOT_SUPPORTED", "Native structured output unavailable")
            if (
                provider.protocol == "anthropic_messages"
                and request.requirements.structured_output == "json_object"
            ):
                raise GatewayError("FEATURE_NOT_SUPPORTED", "Messages has no native json_object")
            if (
                request.requirements.reasoning_level
                and request.requirements.reasoning_level not in caps.reasoning_levels
            ):
                raise GatewayError("PARAMETER_UNSUPPORTED", "Reasoning level unavailable")
            if request.extensions:
                if set(request.extensions) != {provider.vendor}:
                    raise GatewayError("PARAMETER_UNSUPPORTED", "Vendor extensions constrain the candidate")
                if set(request.extensions[provider.vendor]) - set(provider.extension_parameters):
                    raise GatewayError("PARAMETER_UNSUPPORTED", "Undeclared vendor parameter")
            parameters = {**model.defaults, **(prompt_defaults or {}), **route.defaults, **request.generation}
            constrain(parameters, model.parameter_constraints)
            output = parameters.get("max_output_tokens", model.max_output_tokens)
            if output > model.max_output_tokens or output < 1:
                raise GatewayError("PARAMETER_UNSUPPORTED", "Output limit exceeds model allowance")
            if estimated + output + 128 > model.context_window:
                raise GatewayError("CONTEXT_BUDGET_EXCEEDED")
            accepted.append(
                PlannedCandidate(
                    candidate.provider,
                    candidate.model,
                    provider,
                    model,
                    parameters,
                    estimated,
                    estimated + output,
                )
            )
        except GatewayError as exc:
            rejected.append({"provider": candidate.provider, "model": candidate.model, "reason": exc.code})
    if not accepted:
        reasons = {r["reason"] for r in rejected}
        code = (
            next(iter(reasons))
            if len(reasons) == 1
            and reasons <= {"DATA_POLICY_DENIED", "PARAMETER_UNSUPPORTED", "CONTEXT_BUDGET_EXCEEDED"}
            else "NO_COMPATIBLE_MODEL"
        )
        raise GatewayError(code, "No candidate satisfies all requirements", details={"rejections": rejected})
    return accepted, rejected
