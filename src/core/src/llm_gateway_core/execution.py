"""Execution owns attempts, cancellation, retry/fallback and exactly one call terminal."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import suppress
from dataclasses import dataclass, field
from decimal import Decimal

from llm_gateway_core.contracts import (
    GatewayError,
    GatewayResponse,
    Message,
    StreamEvent,
    ToolCall,
    Usage,
    uid,
)
from llm_gateway_core.security import authorize, fingerprint
from llm_gateway_core.structured import parse_json, validate
from llm_gateway_core.usage import aggregate_usage, price

logger = logging.getLogger(__name__)


@dataclass
class Prepared:
    request: object
    messages: list
    context: object
    snapshot: object
    route: object
    plan: list
    row: dict
    started: float
    deadline: float
    app: object
    lease: object = None
    pinned: dict | None = None
    prompt: dict = field(default_factory=dict)
    attempts: list = field(default_factory=list)
    usages: list = field(default_factory=list)
    costs: list = field(default_factory=list)
    terminal: str | None = None
    committed: bool = False
    sequence: int = 0
    response: object = None
    recorded: bool = False
    loop_admitted: bool = False
    span: object = None


class ToolAssembler:
    def __init__(self, tools):
        self.definitions = {t.name: t for t in tools}
        self.slots = {}
        self.bytes = 0

    def feed(self, item):
        i = item["index"]
        if type(i) is not int or i < 0 or i > 1024 or (i not in self.slots and len(self.slots) >= 32):
            raise GatewayError("TOOL_ARGUMENT_VALIDATION_FAILED", "Too many tool calls")
        slot = self.slots.setdefault(
            i,
            {
                "id": "",
                "name": "",
                "arguments": "",
                "pending": [],
                "started": False,
                "ended": False,
                "call": None,
            },
        )
        if slot["ended"]:
            if item["kind"] == "tool_done":
                return []
            raise GatewayError("TOOL_ARGUMENT_VALIDATION_FAILED", "Tool changed after completion")
        out = []
        if item["kind"] == "tool_fragment":
            if slot["started"] and (item.get("id") or item.get("name")):
                raise GatewayError("TOOL_ARGUMENT_VALIDATION_FAILED", "Tool identity changed after commit")
            slot["id"] += item.get("id", "")
            slot["name"] += item.get("name", "")
            delta = item.get("arguments", "")
            slot["arguments"] += delta
            self.bytes += len(delta.encode()) + len(item.get("id", "")) + len(item.get("name", ""))
            if len(slot["arguments"].encode()) > 65536 or self.bytes > 1048576 or len(slot["id"]) > 256:
                raise GatewayError("TOOL_ARGUMENT_VALIDATION_FAILED", "Tool buffer exceeded")
            if delta:
                slot["pending"].append(delta)
        stable = item.get("stable", False) or item["kind"] == "tool_done"
        if stable and not slot["started"]:
            if not slot["id"] or slot["name"] not in self.definitions:
                raise GatewayError("TOOL_ARGUMENT_VALIDATION_FAILED", "Unknown tool or missing identity")
            if any(s["id"] == slot["id"] and j != i for j, s in self.slots.items()):
                raise GatewayError("TOOL_ARGUMENT_VALIDATION_FAILED", "Duplicate tool identity")
            slot["started"] = True
            out.append(("tool_call_start", {"tool_call_id": slot["id"], "index": i, "name": slot["name"]}))
        if slot["started"]:
            for delta in slot["pending"]:
                out.append(
                    ("tool_call_delta", {"tool_call_id": slot["id"], "index": i, "arguments_delta": delta})
                )
            slot["pending"] = []
        if item["kind"] == "tool_done":
            arguments = parse_json(
                slot["arguments"],
                self.definitions[slot["name"]].parameters,
                extract=False,
                code="TOOL_ARGUMENT_VALIDATION_FAILED",
            )
            if not isinstance(arguments, dict):
                raise GatewayError("TOOL_ARGUMENT_VALIDATION_FAILED", "Arguments must be object")
            slot["call"] = ToolCall(
                id=slot["id"], name=slot["name"], arguments=arguments, raw_arguments=slot["arguments"]
            )
            slot["ended"] = True
            out.append(
                (
                    "tool_call_end",
                    {
                        "tool_call_id": slot["id"],
                        "index": i,
                        "parsed_arguments": arguments,
                        "validation_result": "valid",
                        "executable": True,
                    },
                )
            )
        return out

    def calls(self):
        if any(not s["ended"] for s in self.slots.values()):
            raise GatewayError("TOOL_ARGUMENT_VALIDATION_FAILED", "Incomplete tool call")
        return [s["call"] for _, s in sorted(self.slots.items())]


class Executor:
    def __init__(self, gateway):
        self.g = gateway

    def remaining(self, p):
        remaining = p.deadline - time.monotonic()
        if remaining <= 0:
            raise GatewayError("CALL_DEADLINE_EXCEEDED")
        return remaining

    async def attempts(self, p):
        max_attempts = min(p.request.execution.max_total_attempts, p.route.max_total_attempts)
        cost_limits = [Decimal(v) for v in (p.request.execution.max_cost, p.route.max_cost) if v is not None]
        previous = None
        for fallback_no, candidate in enumerate(p.plan[: 1 if p.pinned else p.route.max_switches + 1]):
            for retry_no in range(p.route.max_retries_per_candidate + 1):
                self.remaining(p)
                if len(p.attempts) >= max_attempts:
                    raise GatewayError("ATTEMPT_BUDGET_EXCEEDED")
                loop_limit = min(
                    (p.pinned or {}).get(
                        "max_loop_attempts", p.request.execution.tool_loop_max_total_attempts
                    ),
                    p.request.execution.tool_loop_max_total_attempts,
                )
                if (p.pinned or {}).get("total_attempts", 0) + len(p.attempts) >= loop_limit:
                    raise GatewayError("TOOL_LOOP_LIMIT_EXCEEDED")
                if cost_limits:
                    limit = min(cost_limits)
                    # Unknown prior charges consume the budget conservatively; no unbounded retry.
                    if any(not c.complete for c in p.costs):
                        raise GatewayError("COST_BUDGET_EXCEEDED", "Unknown previous attempt cost")
                    spent = sum((Decimal(c.known_amount) for c in p.costs), Decimal(0))
                    estimate = (
                        Decimal(candidate.input_tokens) * Decimal(candidate.model.pricing.input)
                        + Decimal(
                            candidate.parameters.get("max_output_tokens", candidate.model.max_output_tokens)
                        )
                        * Decimal(candidate.model.pricing.output)
                    ) / candidate.model.pricing.unit
                    if spent + estimate > limit:
                        raise GatewayError("COST_BUDGET_EXCEEDED")
                if retry_no:
                    delay = (
                        previous.retry_after
                        if previous and previous.retry_after is not None
                        else min(p.route.max_delay_s, p.route.initial_delay_s * 2 ** (retry_no - 1))
                        * random.uniform(0.5, 1.5)
                    )
                    if delay >= self.remaining(p):
                        raise GatewayError("CALL_DEADLINE_EXCEEDED")
                    logger.warning(
                        "gateway.retry.scheduled", extra={"context": {"call_id": p.context.call_id}}
                    )
                    await asyncio.sleep(delay)
                try:
                    yield candidate, retry_no, fallback_no
                    return
                except GatewayError as exc:
                    previous = exc
                    if p.committed or exc.code in {
                        "PARAMETER_UNSUPPORTED",
                        "OUTPUT_PARSE_FAILED",
                        "OUTPUT_VALIDATION_FAILED",
                        "TOOL_ARGUMENT_VALIDATION_FAILED",
                        "ACCESS_DENIED",
                        "CALL_DEADLINE_EXCEEDED",
                        "COST_BUDGET_EXCEEDED",
                        "DEPENDENCY_UNAVAILABLE",
                    }:
                        raise
                    if not exc.retryable or retry_no == p.route.max_retries_per_candidate:
                        break
        raise previous or GatewayError("UPSTREAM_UNAVAILABLE")

    async def before(self, p, candidate):
        self.remaining(p)
        if len(p.attempts) >= min(p.request.execution.max_total_attempts, p.route.max_total_attempts):
            raise GatewayError("ATTEMPT_BUDGET_EXCEEDED")
        limits = [Decimal(v) for v in (p.request.execution.max_cost, p.route.max_cost) if v is not None]
        prior = p.pinned or {}
        loop_limits = [
            Decimal(v)
            for v in (prior.get("max_loop_cost"), p.request.execution.tool_loop_max_cost)
            if v is not None
        ]
        estimate = (
            Decimal(candidate.input_tokens)
            * max(
                Decimal(v)
                for v in (
                    candidate.model.pricing.input,
                    candidate.model.pricing.cache_read or "0",
                    candidate.model.pricing.cache_write or "0",
                )
            )
            + Decimal(candidate.parameters.get("max_output_tokens", candidate.model.max_output_tokens))
            * Decimal(candidate.model.pricing.output)
        ) / candidate.model.pricing.unit
        spent = sum((Decimal(c.known_amount) for c in p.costs), Decimal(0))
        if limits and (any(not c.complete for c in p.costs) or spent + estimate > min(limits)):
            raise GatewayError("COST_BUDGET_EXCEEDED")
        if loop_limits and (
            not prior.get("cost_complete", True)
            or any(not c.complete for c in p.costs)
            or Decimal(prior.get("known_cost", "0")) + spent + estimate > min(loop_limits)
        ):
            raise GatewayError("TOOL_LOOP_LIMIT_EXCEEDED", "Tool loop cost budget exhausted")
        if prior.get("total_attempts", 0) + len(p.attempts) >= min(
            prior.get("max_loop_attempts", p.request.execution.tool_loop_max_total_attempts),
            p.request.execution.tool_loop_max_total_attempts,
        ):
            raise GatewayError("TOOL_LOOP_LIMIT_EXCEEDED")
        version, latest = await self.g.control.refresh()
        authorize(latest, p.context, p.request.logical_model)
        live = latest.providers.get(candidate.provider_id)
        if not live or not live.enabled:
            raise GatewayError("UPSTREAM_UNAVAILABLE", "Connection emergency disabled")
        # A pinned loop cannot silently switch account identity after a credential rotation.
        if p.pinned and (
            live.credential_identity != p.pinned["credential_identity"]
            or live.protocol != p.pinned["protocol"]
            or candidate.model.upstream_model != p.pinned["upstream_model"]
        ):
            raise GatewayError("CONTINUATION_MISMATCH")
        consumed = sum(
            (u.input_tokens + u.output_tokens) if u.usage_complete else a["reserved_tokens"]
            for u, a in zip(p.usages, p.attempts, strict=True)
        )
        await self.g.quotas.reserve(p.lease, consumed + candidate.reserved_tokens, p.app.tpm)
        await self.g.circuits.before(candidate.provider_id, candidate.model_id, self.remaining(p))
        adapter = self.g.factory.adapter(candidate.connection)
        lease = await self.g.quotas.acquire(
            f"provider:{candidate.connection.vendor}:{candidate.connection.credential_identity}",
            candidate.connection.rpm,
            candidate.connection.concurrency,
            self.remaining(p) + 5,
            p.app.pool,
            candidate.connection.online_reserved,
        )
        try:
            await self.g.quotas.reserve(lease, candidate.reserved_tokens, candidate.connection.tpm)
            if p.pinned:
                await self.g.continuations.reserve_attempt(
                    p.context,
                    p.pinned,
                    min(prior["max_loop_attempts"], p.request.execution.tool_loop_max_total_attempts),
                    min(prior["max_rounds"], p.request.execution.max_tool_rounds),
                    estimate,
                    min(loop_limits) if loop_limits else None,
                    not p.loop_admitted,
                )
                p.loop_admitted = True
        except BaseException:
            with suppress(Exception):
                await self.g.quotas.release(lease)
            raise
        return version, adapter, lease

    async def record_attempt(self, p, candidate, row, response, error, started, lease):
        usage = response.usage if response else getattr(error, "usage", Usage())
        cost = price(usage, candidate.model.pricing)
        p.usages.append(usage)
        p.costs.append(cost)
        row.update(
            status="completed"
            if error is None
            else "cancelled"
            if error.code == "CALL_CANCELLED"
            else "failed",
            usage=usage.model_dump(),
            cost=cost.model_dump(),
            total_ms=(time.monotonic() - started) * 1000,
            error_code=error.code if error else None,
            finish_reason=response.finish_reason if response else None,
            provider_request_id=response.provider_request_id if response else None,
            upstream_http_status=error.details.get("upstream_http_status") if error else 200,
            upstream_code=error.details.get("upstream_code") if error else None,
            raw_finish_reason=response.raw_finish_reason if response else None,
        )
        logger.log(
            logging.WARNING if error else logging.INFO, "gateway.attempt.completed", extra={"context": row}
        )
        try:
            await self.g.recorder.write(
                "llm_attempts", {"id": row["attempt_id"], "call_id": p.context.call_id, "payload": row}
            )
        finally:
            with suppress(Exception):
                await self.g.quotas.release(lease)
        try:
            await self.g.quotas.settle(
                lease, usage.input_tokens + usage.output_tokens if usage.usage_complete else None
            )
            if error is None:
                await self.g.circuits.record(candidate.provider_id, candidate.model_id, True)
            elif error.code in {
                "UPSTREAM_UNAVAILABLE",
                "UPSTREAM_TIMEOUT",
                "STREAM_INTERRUPTED",
                "UPSTREAM_AUTH_FAILED",
            }:
                await self.g.circuits.record(candidate.provider_id, candidate.model_id, False)
            elif error.code == "UPSTREAM_RATE_LIMITED":
                await self.g.circuits.record(
                    candidate.provider_id, candidate.model_id, False, error.retry_after or 1
                )
        except Exception:
            logger.error("gateway.quota.cleanup_failed", extra={"context": {"call_id": p.context.call_id}})

    def attempt_row(self, p, candidate, retry_no, fallback_no, checked_version):
        row = {
            **p.context.model_dump(),
            "attempt_id": uid("attempt"),
            "attempt_no": len(p.attempts) + 1,
            "retry_no": retry_no,
            "fallback_no": fallback_no,
            "safety_check_version": checked_version,
            "provider_id": candidate.provider_id,
            "model_id": candidate.model_id,
            "upstream_model": candidate.model.upstream_model,
            "protocol": candidate.connection.protocol,
            "credential_identity": candidate.connection.credential_identity,
            "credential_version": candidate.connection.credential_version,
            "parameters": candidate.parameters,
            "status": "running",
            "input_estimate": candidate.input_tokens,
            "reserved_tokens": candidate.reserved_tokens,
            "estimate_source": "utf8_byte_upper_bound",
            "queue_ms": 0,
            "ttft_ms": None,
            "first_content_ms": None,
            "generation_ms": None,
        }
        p.attempts.append(row)
        p.row["payload"].update(
            actual_provider=candidate.provider_id, actual_model=candidate.model.upstream_model
        )
        return row

    def validate_response(self, p, response):
        names = {t.name: t for t in p.request.tools}
        if len({t.id for t in response.tool_calls}) != len(response.tool_calls):
            raise GatewayError("TOOL_ARGUMENT_VALIDATION_FAILED", "Duplicate tool ID")
        for call in response.tool_calls:
            if call.name not in names:
                raise GatewayError("TOOL_ARGUMENT_VALIDATION_FAILED", "Undeclared tool")
            validate(call.arguments, names[call.name].parameters, "TOOL_ARGUMENT_VALIDATION_FAILED")
        if response.tool_calls and response.finish_reason != "tool_calls":
            raise GatewayError("TOOL_ARGUMENT_VALIDATION_FAILED", "Tool finish reason mismatch")
        if response.finish_reason == "tool_calls" and not response.tool_calls:
            raise GatewayError("TOOL_ARGUMENT_VALIDATION_FAILED", "Tool finish without tools")
        if p.request.tool_choice == "required" and not response.tool_calls:
            raise GatewayError("TOOL_ARGUMENT_VALIDATION_FAILED", "Required tool call missing")
        if p.request.requirements.structured_output != "none":
            return parse_json(response.content, p.request.output_schema)
        return None

    async def continuation(self, p, candidate, response):
        if not response.tool_calls:
            return None
        prior = p.pinned or {}
        opaque = [
            *prior.get("continuation_by_message", []),
            {"message_index": len(p.messages), "blocks": response.provider_continuation},
        ]
        assistant = Message(
            role="assistant",
            content=[] if not response.content else [{"type": "text", "text": response.content}],
            tool_calls=response.tool_calls,
        )
        expected_history = [m.model_dump(mode="json") for m in [*p.messages, assistant]]
        data = {
            "provider_id": candidate.provider_id,
            "model_id": candidate.model_id,
            "upstream_model": candidate.model.upstream_model,
            "protocol": candidate.connection.protocol,
            "credential_identity": candidate.connection.credential_identity,
            "credential_version": candidate.connection.credential_version,
            "logical_model": p.request.logical_model,
            "round": prior.get("round", 0) + 1,
            "deadline": prior.get("deadline", time.time() + p.request.execution.tool_loop_timeout_s),
            "max_rounds": min(
                prior.get("max_rounds", p.request.execution.max_tool_rounds),
                p.request.execution.max_tool_rounds,
            ),
            "max_loop_attempts": min(
                prior.get("max_loop_attempts", p.request.execution.tool_loop_max_total_attempts),
                p.request.execution.tool_loop_max_total_attempts,
            ),
            "max_loop_cost": str(
                min(
                    Decimal(v)
                    for v in (prior.get("max_loop_cost"), p.request.execution.tool_loop_max_cost)
                    if v is not None
                )
            )
            if any(
                v is not None for v in (prior.get("max_loop_cost"), p.request.execution.tool_loop_max_cost)
            )
            else None,
            "cost_complete": prior.get("cost_complete", True) and all(c.complete for c in p.costs),
            "history_length": len(expected_history),
            "history_hash": fingerprint(expected_history),
            "expected_tools": {t.id: t.name for t in response.tool_calls},
            "tools_hash": fingerprint([t.model_dump() for t in p.request.tools]),
            "continuation_by_message": opaque,
            "run_id": p.request.context.run_id,
            "total_attempts": prior.get("total_attempts", 0) + len(p.attempts),
            "known_cost": str(
                Decimal(prior.get("known_cost", "0"))
                + sum((Decimal(c.known_amount) for c in p.costs), Decimal(0))
            ),
        }
        return await self.g.continuations.save(p.context, data, prior.get("tool_loop_id"))

    async def result(self, p, candidate, response, structured):
        continuation = await self.continuation(p, candidate, response)
        cost = self.g.total_cost(p)
        return GatewayResponse(
            call_id=p.context.call_id,
            actual_provider=candidate.provider_id,
            actual_model=candidate.model.upstream_model,
            content=response.content,
            tool_calls=response.tool_calls,
            structured_data=structured,
            usage=aggregate_usage(p.usages),
            cost=cost,
            finish_reason=response.finish_reason,
            continuation=continuation,
        )

    async def generate(self, p):
        planner = self.attempts(p)
        repair_used = False
        try:
            choice = await anext(planner)
            while True:
                candidate, retry, fallback = choice
                error, response, lease, row = None, None, None, None
                started = time.monotonic()
                try:
                    async with asyncio.timeout(self.remaining(p)):
                        version, adapter, lease = await self.before(p, candidate)
                        row = self.attempt_row(p, candidate, retry, fallback, version)
                        await self.g.recorder.write(
                            "llm_attempts",
                            {"id": row["attempt_id"], "call_id": p.context.call_id, "payload": row},
                        )
                        row["admission_ms"] = (time.monotonic() - started) * 1000
                        started = time.monotonic()
                        logger.info("gateway.attempt.started", extra={"context": row})
                        with self.g.observer.attempt_span(p.span, row):
                            response = await adapter.generate(
                                candidate.model,
                                p.request,
                                p.messages,
                                candidate.parameters,
                                (p.pinned or {}).get("continuation_by_message"),
                            )
                        structured = self.validate_response(p, response)
                except TimeoutError:
                    error = GatewayError("CALL_DEADLINE_EXCEEDED")
                except asyncio.CancelledError:
                    error = GatewayError("CALL_CANCELLED")
                    raise
                except GatewayError as exc:
                    error = exc
                except Exception:
                    error = GatewayError("INTERNAL_ERROR")
                finally:
                    if row:
                        await self.record_attempt(p, candidate, row, response, error, started, lease)
                if error:
                    if (
                        error.code in {"OUTPUT_PARSE_FAILED", "OUTPUT_VALIDATION_FAILED"}
                        and p.request.execution.model_repair
                        and p.route.allow_model_repair
                        and not repair_used
                        and not p.pinned
                        and not p.request.tools
                        and response is not None
                    ):
                        repair_used = True
                        p.row["payload"]["model_repair_attempted"] = True
                        p.messages = [
                            *p.messages,
                            Message(role="assistant", content=[{"type": "text", "text": response.content}]),
                            Message(
                                role="user",
                                content=[
                                    {
                                        "type": "text",
                                        "text": "Reformat the previous answer to the required JSON schema. "
                                        "Use only facts already present. Do not invent missing fields.",
                                    }
                                ],
                            ),
                        ]
                        from llm_gateway_core.routing import estimate_input

                        candidate.input_tokens = estimate_input(p.request, p.messages)
                        candidate.reserved_tokens = candidate.input_tokens + candidate.parameters.get(
                            "max_output_tokens", candidate.model.max_output_tokens
                        )
                        if candidate.reserved_tokens + 128 > candidate.model.context_window:
                            raise GatewayError("CONTEXT_BUDGET_EXCEEDED")
                        choice = (candidate, retry + 1, fallback)
                        continue
                    choice = await planner.athrow(error)
                    continue
                result = await self.result(p, candidate, response, structured)
                p.response = result
                await self.g.finalize(p, "completed")
                return result
        except asyncio.CancelledError:
            await self.g.finalize(p, "cancelled", GatewayError("CALL_CANCELLED"))
            raise
        except GatewayError as exc:
            exc.call_id = p.context.call_id
            await self.g.finalize(p, "failed", exc)
            raise
        except Exception as exc:
            error = GatewayError("INTERNAL_ERROR")
            error.call_id = p.context.call_id
            await self.g.finalize(p, "failed", error)
            raise error from exc
        finally:
            await planner.aclose()

    def event(self, p, kind, data):
        p.sequence += 1
        return StreamEvent(type=kind, call_id=p.context.call_id, sequence=p.sequence, data=data)

    async def stream(self, p):
        planner = self.attempts(p)
        terminal_data = None
        try:
            choice = await anext(planner)
            while True:
                candidate, retry, fallback = choice
                error, response, lease, row = None, None, None, None
                started = time.monotonic()
                attempt_span = None
                assembler = ToolAssembler(p.request.tools)
                try:
                    async with asyncio.timeout(self.remaining(p)):
                        version, adapter, lease = await self.before(p, candidate)
                        row = self.attempt_row(p, candidate, retry, fallback, version)
                        await self.g.recorder.write(
                            "llm_attempts",
                            {"id": row["attempt_id"], "call_id": p.context.call_id, "payload": row},
                        )
                        row["admission_ms"] = (time.monotonic() - started) * 1000
                        started = time.monotonic()
                        logger.info("gateway.attempt.started", extra={"context": row})
                        attempt_span = self.g.observer.start_attempt(p.span, row)
                        iterator = adapter.stream(
                            candidate.model,
                            p.request,
                            p.messages,
                            candidate.parameters,
                            (p.pinned or {}).get("continuation_by_message"),
                        )
                        first_deadline = time.monotonic() + candidate.connection.first_content_timeout_s
                        try:
                            while True:
                                try:
                                    wait = min(self.remaining(p), candidate.connection.read_idle_timeout_s)
                                    if not p.committed:
                                        wait = min(wait, max(0.001, first_deadline - time.monotonic()))
                                    item = await asyncio.wait_for(anext(iterator), wait)
                                except StopAsyncIteration:
                                    break
                                events = []
                                if item["kind"] == "text":
                                    if row["ttft_ms"] is None:
                                        row["ttft_ms"] = (time.monotonic() - started) * 1000
                                    events = [("delta", {"text": item["text"]})]
                                elif item["kind"] in {"tool_fragment", "tool_done"}:
                                    events = assembler.feed(item)
                                elif item["kind"] == "end":
                                    response = item["response"]
                                    response.tool_calls = assembler.calls()
                                    if response.tool_calls:
                                        response.finish_reason = "tool_calls"
                                for kind, data in events:
                                    if not p.committed:
                                        p.committed = True
                                        row["first_content_ms"] = (time.monotonic() - started) * 1000
                                        logger.info(
                                            "gateway.stream.committed",
                                            extra={
                                                "context": {
                                                    "call_id": p.context.call_id,
                                                    "attempt_id": row["attempt_id"],
                                                }
                                            },
                                        )
                                    yield self.event(p, kind, data)
                        finally:
                            await iterator.aclose()
                        if response is None:
                            raise GatewayError("STREAM_INTERRUPTED", retryable=True)
                        self.validate_response(p, response)
                        if row["first_content_ms"] is not None:
                            row["generation_ms"] = (time.monotonic() - started) * 1000 - row[
                                "first_content_ms"
                            ]
                except TimeoutError:
                    error = GatewayError(
                        "CALL_DEADLINE_EXCEEDED" if time.monotonic() >= p.deadline else "UPSTREAM_TIMEOUT",
                        retryable=True,
                    )
                except (asyncio.CancelledError, GeneratorExit):
                    error = GatewayError("CALL_CANCELLED")
                    raise
                except GatewayError as exc:
                    error = exc
                except Exception:
                    error = GatewayError("INTERNAL_ERROR")
                finally:
                    if attempt_span:
                        attempt_span.end()
                    if row:
                        await self.record_attempt(p, candidate, row, response, error, started, lease)
                if error:
                    choice = await planner.athrow(error)
                    continue
                p.response = await self.result(p, candidate, response, None)
                if response.usage.usage_source != "unknown":
                    yield self.event(p, "usage", response.usage.model_dump())
                await self.g.finalize(p, "completed")
                terminal_data = {
                    "status": "completed",
                    "finish_reason": response.finish_reason,
                    "partial": False,
                    "actual_provider": candidate.provider_id,
                    "actual_model": candidate.model.upstream_model,
                    "continuation": p.response.continuation.model_dump() if p.response.continuation else None,
                }
                break
        except (asyncio.CancelledError, GeneratorExit):
            await self.g.finalize(p, "cancelled", GatewayError("CALL_CANCELLED"))
            raise
        except Exception as exc:
            error = exc if isinstance(exc, GatewayError) else GatewayError("INTERNAL_ERROR")
            error.call_id = p.context.call_id
            await self.g.finalize(p, "failed", error)
            terminal_data = {"status": "failed", "partial": p.committed, "error": error.body()["error"]}
        finally:
            await planner.aclose()
        if terminal_data:
            yield self.event(p, "terminal", terminal_data)
