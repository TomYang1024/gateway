from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from llm_gateway_core.contracts import CallContext, Cost, GatewayError, uid
from llm_gateway_core.execution import Executor, Prepared
from llm_gateway_core.routing import plan
from llm_gateway_core.security import authorize, fingerprint
from llm_gateway_core.structured import check_schema
from llm_gateway_core.usage import aggregate_usage

logger = logging.getLogger(__name__)


class Gateway:
    def __init__(self, control, quotas, continuations, prompts, factory, circuits, recorder, db, observer):
        self.control, self.quotas, self.continuations = control, quotas, continuations
        self.prompts, self.factory, self.circuits = prompts, factory, circuits
        self.recorder, self.db, self.observer = recorder, db, observer
        self.executor = Executor(self)
        self.accepting = True

    async def prepare(self, request, identity, streaming=False, received_at=None):
        version, snapshot = self.control.get()
        context = CallContext(
            **identity.model_dump(),
            config_version=version,
            quota_policy_version=version,
            route_policy_version=version,
            data_policy_version=version,
        )
        started = received_at if received_at is not None else time.monotonic()
        created = datetime.now(UTC)
        row = {
            "id": context.call_id,
            "team_id": identity.team_id,
            "application_id": identity.application_id,
            "logical_model": request.logical_model,
            "business_ref": request.context.business_ref,
            "created_at": created.isoformat(),
            "status": "running",
            "payload": {
                **context.model_dump(),
                **request.context.model_dump(exclude={"trace_id", "metadata"}),
                "generation_requested": request.generation,
                "execution": request.execution.model_dump(),
                "input_hash": fingerprint(request.model_dump(mode="json")),
            },
        }
        p = Prepared(
            request=request.model_copy(deep=True),
            messages=[],
            context=context,
            snapshot=snapshot,
            route=None,
            plan=[],
            row=row,
            started=started,
            deadline=started + request.execution.total_timeout_s,
            app=None,
        )
        try:
            if not self.accepting:
                raise GatewayError("DEPENDENCY_UNAVAILABLE", "Gateway is draining")
            self.recorder.ensure_capacity()
            p.span = self.observer.start_call(context)
            row["payload"].update(context.model_dump())
            await self.recorder.write("llm_calls", row)
            p.recorded = True
            async with asyncio.timeout(max(0.001, p.deadline - time.monotonic())):
                p.app = authorize(snapshot, identity, request.logical_model)
                p.route = snapshot.routes[request.logical_model]
                p.deadline = min(p.deadline, started + p.route.total_timeout_s)
                p.lease = await self.quotas.acquire(
                    f"app:{identity.application_id}",
                    p.app.rpm,
                    p.app.concurrency,
                    p.deadline - time.monotonic() + 5,
                    p.app.pool,
                )
                # All rendering and other expensive work begins only after authentication/RPM/concurrency.
                if streaming and request.requirements.structured_output != "none":
                    raise GatewayError("FEATURE_NOT_SUPPORTED", "Business structured streaming is outside V1")
                if request.execution.model_repair and not p.route.allow_model_repair:
                    raise GatewayError("ACCESS_DENIED", "Model repair is not enabled for this route")
                defaults = {}
                if request.prompt:
                    p.messages, bundle = self.prompts.render(request, identity)
                    p.prompt = {
                        "name": request.prompt.name,
                        "version": request.prompt.version,
                        "hash": bundle["hash"],
                    }
                    p.row["payload"].update(
                        prompt_name=request.prompt.name,
                        prompt_version=request.prompt.version,
                        prompt_hash=bundle["hash"],
                    )
                    defaults = bundle["manifest"].get("generation", {})
                    if not p.request.tools:
                        p.request.tools = bundle["tools"]
                    if (
                        not p.request.output_schema
                        and p.request.requirements.structured_output == "json_schema"
                    ):
                        p.request.output_schema = bundle["output.schema.json"]
                else:
                    p.messages = p.request.messages
                if p.request.requirements.structured_output == "json_schema" and not p.request.output_schema:
                    raise GatewayError("REQUEST_INVALID", "Prompt has no output schema")
                if p.request.output_schema:
                    check_schema(p.request.output_schema)
                    p.row["payload"]["output_schema_hash"] = fingerprint(p.request.output_schema)
                for tool in p.request.tools:
                    check_schema(tool.parameters)
                if request.continuation:
                    if request.prompt:
                        raise GatewayError(
                            "TOOL_RESULT_INVALID", "Continuation requires explicit complete messages"
                        )
                    p.pinned = await self.continuations.load(identity, request.continuation)
                    self.check_continuation(p)
                    p.deadline = min(p.deadline, time.monotonic() + p.pinned["deadline"] - time.time())
                route_started = time.monotonic()
                blocked = await self.circuits.unavailable(p.route.candidates)
                p.plan, rejected = plan(
                    snapshot, p.request, p.messages, p.app, streaming, defaults, p.pinned, blocked
                )
                logger.info("gateway.route.selected", extra={"context": context.model_dump()})
                for c in p.plan:
                    # Catch unsupported explicit combinations before opening SSE or creating an Attempt.
                    self.factory.validate(
                        c.connection,
                        c.model,
                        p.request,
                        p.messages,
                        c.parameters,
                        streaming,
                        (p.pinned or {}).get("continuation_by_message"),
                    )
                p.row["payload"]["route_ms"] = (time.monotonic() - route_started) * 1000
                await self.recorder.write(
                    "llm_route_decisions",
                    {
                        "id": uid("route"),
                        "call_id": context.call_id,
                        "payload": {
                            "config_version": version,
                            "rejections": rejected,
                            "candidates": [
                                {
                                    "provider": c.provider_id,
                                    "model": c.model_id,
                                    "parameters": c.parameters,
                                    "input_estimate": c.input_tokens,
                                }
                                for c in p.plan
                            ],
                        },
                    },
                )
                await self.quotas.reserve(p.lease, max(c.reserved_tokens for c in p.plan), p.app.tpm)
                team = snapshot.teams[identity.team_id]
                if team.monthly_budget and team.budget_mode == "reject":
                    try:
                        status = await self.db.budget_status(identity.team_id, team)
                    except Exception as exc:
                        raise GatewayError("DEPENDENCY_UNAVAILABLE", "Hard budget data unavailable") from exc
                    if status and (
                        not status["complete"] or status["amount"] >= Decimal(team.monthly_budget)
                    ):
                        raise GatewayError("TEAM_BUDGET_EXCEEDED")
                self.executor.remaining(p)
            logger.info("gateway.call.started", extra={"context": context.model_dump()})
            return p
        except asyncio.CancelledError:
            if p.recorded:
                await self.finalize(p, "cancelled", GatewayError("CALL_CANCELLED"))
            elif p.span:
                p.span.end()
            raise
        except Exception as exc:
            error = (
                GatewayError("CALL_DEADLINE_EXCEEDED")
                if isinstance(exc, TimeoutError)
                else exc
                if isinstance(exc, GatewayError)
                else GatewayError("INTERNAL_ERROR")
            )
            error.call_id = context.call_id
            if p.recorded:
                await self.finalize(p, "failed", error)
            elif p.span:
                p.span.end()
            raise error from exc

    def check_continuation(self, p):
        state, req = p.pinned, p.request
        if (
            state["round"] >= min(state["max_rounds"], req.execution.max_tool_rounds)
            or time.time() >= state["deadline"]
        ):
            raise GatewayError("TOOL_LOOP_LIMIT_EXCEEDED")
        if state["logical_model"] != req.logical_model or state["run_id"] != req.context.run_id:
            raise GatewayError("CONTINUATION_MISMATCH")
        if state["tools_hash"] != fingerprint([t.model_dump() for t in req.tools]):
            raise GatewayError("CONTINUATION_MISMATCH", "Tool definitions changed")
        n = state["history_length"]
        history = [m.model_dump(mode="json") for m in p.messages[:n]]
        if len(history) != n or fingerprint(history) != state["history_hash"]:
            raise GatewayError("TOOL_RESULT_INVALID", "Assistant tool history does not match continuation")
        results = p.messages[n:]
        ids = [m.tool_call_id for m in results]
        if (
            any(m.role != "tool" for m in results)
            or len(ids) != len(set(ids))
            or set(ids) != set(state["expected_tools"])
            or any(m.name and m.name != state["expected_tools"][m.tool_call_id] for m in results)
        ):
            raise GatewayError("TOOL_RESULT_INVALID", "All tool results must match original tool IDs")

    def total_cost(self, p):
        currency = p.snapshot.teams[p.context.team_id].currency
        complete = all(c.complete for c in p.costs)
        known = sum((Decimal(c.known_amount) for c in p.costs), Decimal(0))
        return Cost(
            amount=str(known) if complete else None,
            known_amount=str(known),
            currency=currency,
            pricing_version=",".join(dict.fromkeys(c.pricing_version for c in p.costs)),
            complete=complete,
        )

    async def finalize(self, p, status, error=None):
        if p.terminal:
            return
        p.terminal = status
        cost = self.total_cost(p)
        usage = aggregate_usage(p.usages)
        p.row["status"] = status
        p.row["payload"].update(
            status=status,
            cost=cost.model_dump(),
            usage=usage.model_dump(),
            total_ms=(time.monotonic() - p.started) * 1000,
            attempts=len(p.attempts),
            error_code=error.code if error else None,
            partial=p.committed and status != "completed",
            finish_reason=p.response.finish_reason if p.response else None,
        )
        team = p.snapshot.teams[p.context.team_id]
        settlement = {
            "team_id": p.context.team_id,
            "period": datetime.now(UTC).astimezone(ZoneInfo(team.timezone)).strftime("%Y-%m"),
            "known_amount": cost.known_amount,
            "complete": cost.complete,
            "team": team.model_dump(),
            "version": p.context.config_version,
        }
        try:
            await self.recorder.write("llm_calls", p.row, settlement)
            await self.recorder.write(
                "llm_call_events",
                {
                    "id": p.context.call_id + ":terminal",
                    "call_id": p.context.call_id,
                    "payload": {
                        "type": "terminal",
                        "status": status,
                        "partial": p.committed and status != "completed",
                        "error_code": error.code if error else None,
                    },
                },
            )
        finally:
            if p.lease:
                with suppress(Exception):
                    # No upstream request means a known unused TPM reservation; unknown attempts retain it.
                    actual = (
                        usage.input_tokens + usage.output_tokens
                        if usage.usage_complete
                        else 0
                        if not p.attempts
                        else None
                    )
                    await self.quotas.settle(p.lease, actual)
                    await self.quotas.release(p.lease)
            self.observer.record_call(
                status,
                p.request.logical_model if p.request.logical_model in p.snapshot.routes else "unauthorized",
                time.monotonic() - p.started,
            )
            if p.span:
                p.span.end()
            logger.log(
                logging.WARNING if status == "failed" else logging.INFO,
                f"gateway.call.{status}",
                extra={"context": {**p.context.model_dump(), "error_code": error.code if error else None}},
            )

    async def generate(self, request, identity, received_at=None):
        return await self.executor.generate(await self.prepare(request, identity, received_at=received_at))

    async def stream(self, request, identity):
        p = await self.prepare(request, identity, True)
        async for event in self.executor.stream(p):
            yield event
