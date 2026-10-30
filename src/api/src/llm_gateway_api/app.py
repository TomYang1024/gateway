from __future__ import annotations

import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager, suppress
from typing import Literal

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from llm_gateway_core.config import AdminKey, AppKey, Snapshot, secret_hash
from llm_gateway_core.contracts import Contract, GatewayError, GatewayRequest, uid
from llm_gateway_core.security import authenticate, authenticate_admin
from llm_gateway_infrastructure.coordination import Continuations, Quotas
from llm_gateway_infrastructure.notifications import Notifications
from llm_gateway_infrastructure.observability import Observer, registry, setup_logging, setup_tracing
from llm_gateway_infrastructure.providers import ClientFactory
from llm_gateway_infrastructure.resilience import Circuits
from llm_gateway_infrastructure.settings import Settings, load_bootstrap
from llm_gateway_infrastructure.storage import Database, Recorder
from llm_gateway_services.control_plane import ControlPlane
from llm_gateway_services.gateway import Gateway
from llm_gateway_services.prompts import PromptStore
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import AwareDatetime, Field
from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class RequestTiming:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope.setdefault("state", {})["received_at"] = time.monotonic()
        await self.app(scope, receive, send)


class PolicyChange(Contract):
    base_version: int
    snapshot: dict
    reason: str = Field(min_length=1, max_length=1000)


class Rollback(Contract):
    base_version: int
    target_version: int
    reason: str = Field(min_length=1, max_length=1000)


class KeyIssue(Contract):
    base_version: int
    application_id: str
    expires_at: float | None = None
    reason: str = Field(min_length=1, max_length=1000)


class AdminIssue(Contract):
    base_version: int
    actor: str
    role: Literal["platform_admin", "team_report_reader", "finance_report_reader"]
    teams: list[str]
    expires_at: float
    reason: str = Field(min_length=1, max_length=1000)


class Switch(Contract):
    base_version: int
    enabled: bool
    reason: str = Field(min_length=1, max_length=1000)


class Adjustment(Contract):
    team_id: str
    amount: str = Field(pattern=r"^-?\d+(\.\d+)?$")
    currency: str
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    category: Literal["usage", "discount", "tax", "currency", "external_invoice"]
    reason: str = Field(min_length=1, max_length=1000)


def bearer(authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        raise GatewayError("AUTHENTICATION_FAILED", "Bearer credential required")
    return authorization[7:]


async def connected(request, awaitable, inflight):
    task = asyncio.create_task(awaitable)
    inflight.add(task)
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.05)
            if not task.done() and await request.is_disconnected():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                raise GatewayError("CALL_CANCELLED")
        return await task
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        inflight.discard(task)


async def sse(gateway, prepared, settings, inflight):
    queue = asyncio.Queue(maxsize=settings.stream_buffer_size)
    sentinel = object()

    async def produce():
        iterator = gateway.executor.stream(prepared)
        try:
            async for event in iterator:
                await asyncio.wait_for(queue.put(event), settings.backpressure_timeout_s)
        except TimeoutError:
            logger.warning("gateway.stream.backpressure_exceeded")
        finally:
            await iterator.aclose()
            # Do not block cleanup on a disconnected or stalled consumer.
            if not queue.full():
                queue.put_nowait(sentinel)

    producer = asyncio.create_task(produce())
    inflight.add(producer)
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), 5)
            except TimeoutError:
                if producer.done():
                    break
                yield ": heartbeat\n\n"
                continue
            if item is sentinel:
                break
            yield f"id: {item.sequence}\nevent: {item.type}\ndata: {item.model_dump_json()}\n\n"
            if item.type == "terminal":
                break
    finally:
        producer.cancel()
        with suppress(asyncio.CancelledError):
            await producer
        inflight.discard(producer)
        if not prepared.terminal:
            await gateway.finalize(prepared, "cancelled", GatewayError("CALL_CANCELLED"))


def create_app(settings: Settings | None = None, *, transport=None):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        setup_logging()
        tracing = setup_tracing()
        db = Database(settings.database_url)
        redis = Redis.from_url(settings.redis_url, socket_timeout=3, socket_connect_timeout=3)
        recorder = Recorder(db, settings.spool_dir, settings.spool_max_bytes)
        control = ControlPlane(db, redis, settings.snapshot_ttl_s)
        prompts = PromptStore(settings.bundle_root, settings.bundle_index)
        factory = ClientFactory(transport)
        gateway = Gateway(
            control,
            Quotas(redis),
            Continuations(redis, settings.continuation_key, settings.continuation_ttl_s),
            prompts,
            factory,
            Circuits(redis),
            recorder,
            db,
            Observer(),
        )
        notifications = Notifications(db, settings)
        app.state.gateway, app.state.settings, app.state.inflight = gateway, settings, set()
        app.state.ready = False
        last_prompts = 0.0

        async def reconcile():
            nonlocal last_prompts
            while True:
                try:
                    if not control.snapshot:
                        await db.bootstrap(load_bootstrap(settings.bootstrap_path))
                    await control.refresh()
                    await redis.ping()
                    if time.monotonic() - last_prompts >= settings.prompt_poll_s:
                        try:
                            await asyncio.to_thread(prompts.refresh)
                            last_prompts = time.monotonic()
                        except Exception:
                            logger.error("gateway.prompt.refresh_failed")
                            if not prompts.bundles:
                                raise
                    await recorder.flush()
                    recorder.ensure_capacity()
                    app.state.ready = True
                    await notifications.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    app.state.ready = False
                    logger.error("gateway.dependency.unavailable")
                await asyncio.sleep(settings.policy_poll_s)

        # Startup does not falsely advertise readiness before initial validation.
        worker = asyncio.create_task(reconcile())
        subscription = asyncio.create_task(control.subscribe())
        for _ in range(100):
            if app.state.ready:
                break
            await asyncio.sleep(0.02)
        try:
            yield
        finally:
            gateway.accepting = False
            app.state.ready = False
            for task in (worker, subscription):
                task.cancel()
            await asyncio.gather(worker, subscription, return_exceptions=True)
            if app.state.inflight:
                _, pending = await asyncio.wait(app.state.inflight, timeout=20)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            await factory.close()
            await recorder.close()
            await redis.aclose()
            await db.engine.dispose()
            if tracing:
                await asyncio.to_thread(tracing.shutdown)

    app = FastAPI(title="LLM Gateway", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestTiming)

    @app.exception_handler(GatewayError)
    async def gateway_error(request, error):
        return JSONResponse(error.body(), status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # Pydantic's input/ctx may contain full prompts or secrets; never echo them.
        return JSONResponse(
            GatewayError(
                "REQUEST_INVALID",
                "Request does not satisfy the API contract",
                details={"fields": [list(e["loc"]) for e in error.errors()]},
            ).body(),
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def internal_error(request, error):
        logger.error("gateway.internal_error")
        return JSONResponse(GatewayError("INTERNAL_ERROR").body(), status_code=500)

    async def identity(authorization: str | None = Header(default=None)):
        version, snapshot = app.state.gateway.control.get()
        return authenticate(snapshot, version, bearer(authorization))

    async def admin(authorization: str | None = Header(default=None)):
        _, snapshot = app.state.gateway.control.get()
        return authenticate_admin(snapshot, bearer(authorization))

    async def writer(actor=Depends(admin)):
        if actor["role"] != "platform_admin":
            raise GatewayError("ACCESS_DENIED", "Management write role required")
        return actor

    def report_teams(actor):
        _, snapshot = app.state.gateway.control.get()
        return list(snapshot.teams) if actor["role"] == "platform_admin" else actor["teams"]

    @app.get("/health/live")
    async def live():
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready():
        gateway = app.state.gateway
        try:
            gateway.control.get()
            await gateway.quotas.redis.ping()
            gateway.recorder.ensure_capacity()
            if not app.state.ready:
                raise GatewayError("DEPENDENCY_UNAVAILABLE")
        except Exception:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return {"status": "ready", "policy_version": gateway.control.version}

    @app.get("/metrics")
    async def metrics(actor=Depends(admin)):
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/generate")
    async def generate(body: GatewayRequest, request: Request, who=Depends(identity)):
        return await connected(
            request, app.state.gateway.generate(body, who, request.state.received_at), app.state.inflight
        )

    @app.post("/v1/stream")
    async def stream(body: GatewayRequest, request: Request, who=Depends(identity)):
        prepared = await connected(
            request, app.state.gateway.prepare(body, who, True, request.state.received_at), app.state.inflight
        )
        return StreamingResponse(
            sse(app.state.gateway, prepared, settings, app.state.inflight),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/models")
    async def models(who=Depends(identity)):
        _, snapshot = app.state.gateway.control.get()
        application = snapshot.applications[who.application_id]
        return {
            "models": [
                {
                    "logical_model": name,
                    "candidates": [
                        {
                            "provider": c.provider,
                            "model": c.model,
                            "capabilities": snapshot.models[c.model].capabilities.model_dump(),
                        }
                        for c in snapshot.routes[name].candidates
                        if snapshot.providers[c.provider].enabled
                    ],
                }
                for name in application.use_cases
            ]
        }

    @app.get("/v1/calls")
    async def list_calls(
        business_ref: str | None = None,
        logical_model: str | None = None,
        status: str | None = None,
        since: AwareDatetime | None = None,
        until: AwareDatetime | None = None,
        offset: int = Query(default=0, ge=0, le=100000),
        limit: int = Query(default=50, ge=1, le=100),
        who=Depends(identity),
    ):
        return {
            "calls": await app.state.gateway.db.query_calls(
                team_ids=[who.team_id],
                application_id=who.application_id,
                business_ref=business_ref,
                logical_model=logical_model,
                status=status,
                since=since,
                until=until,
                offset=offset,
                limit=limit,
            )
        }

    @app.get("/v1/calls/{call_id}")
    async def call(call_id: str, who=Depends(identity)):
        return await app.state.gateway.db.call(call_id, [who.team_id], who.application_id)

    @app.get("/v1/reports/usage")
    async def report(
        group_by: Literal["logical_model", "model", "prompt_version"] = "logical_model",
        since: AwareDatetime | None = None,
        until: AwareDatetime | None = None,
        who=Depends(identity),
        offset: int = Query(default=0, ge=0, le=100000),
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        return {
            "rows": await app.state.gateway.db.report(
                [who.team_id], who.application_id, group_by, since, until, offset, limit
            )
        }

    @app.get("/admin/v1/reports/usage")
    async def admin_report(
        group_by: Literal[
            "team_id", "application_id", "logical_model", "model", "prompt_version"
        ] = "team_id",
        team_id: str | None = None,
        since: AwareDatetime | None = None,
        until: AwareDatetime | None = None,
        actor=Depends(admin),
        offset: int = Query(default=0, ge=0, le=100000),
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        teams = report_teams(actor)
        if team_id and team_id not in teams:
            raise GatewayError("ACCESS_DENIED")
        await app.state.gateway.db.audit(actor, "report.read", {"teams": [team_id] if team_id else teams})
        return {
            "rows": await app.state.gateway.db.report(
                [team_id] if team_id else teams, None, group_by, since, until, offset, limit
            )
        }

    @app.get("/admin/v1/policies/current")
    async def current_policy(actor=Depends(writer)):
        version, snapshot = app.state.gateway.control.get()
        return {"version": version, "snapshot": snapshot.model_dump(mode="json")}

    @app.post("/admin/v1/policies")
    async def policy(body: PolicyChange, actor=Depends(writer)):
        try:
            snapshot = Snapshot.model_validate(body.snapshot)
        except ValueError as exc:
            raise GatewayError("REQUEST_INVALID", "Policy validation failed") from exc
        version = await app.state.gateway.control.publish(snapshot, body.base_version, actor, body.reason)
        return {"version": version}

    @app.post("/admin/v1/policies/rollback")
    async def rollback(body: Rollback, actor=Depends(writer)):
        gateway = app.state.gateway
        snapshot = await gateway.db.version(body.target_version)
        _, current = gateway.control.get()
        # Operational rollback never resurrects a revoked credential.
        snapshot.keys, snapshot.admins = current.keys, current.admins
        snapshot = Snapshot.model_validate(snapshot.model_dump())
        return {"version": await gateway.control.publish(snapshot, body.base_version, actor, body.reason)}

    @app.post("/admin/v1/keys")
    async def issue_key(body: KeyIssue, actor=Depends(writer)):
        gateway = app.state.gateway
        _, snapshot = gateway.control.get()
        if body.application_id not in snapshot.applications or (
            body.expires_at and body.expires_at <= time.time()
        ):
            raise GatewayError("REQUEST_INVALID")
        raw, ident = "gw_app_" + secrets.token_urlsafe(32), uid("key")
        snapshot.keys[ident] = AppKey(
            application_id=body.application_id, digest=secret_hash(raw), expires_at=body.expires_at
        )
        version = await gateway.control.publish(snapshot, body.base_version, actor, body.reason)
        return {"key_id": ident, "key": raw, "version": version}

    @app.post("/admin/v1/keys/{key_id}")
    async def switch_key(key_id: str, body: Switch, actor=Depends(writer)):
        gateway = app.state.gateway
        _, snapshot = gateway.control.get()
        if key_id not in snapshot.keys:
            raise GatewayError("REQUEST_INVALID")
        snapshot.keys[key_id].enabled = body.enabled
        return {"version": await gateway.control.publish(snapshot, body.base_version, actor, body.reason)}

    @app.post("/admin/v1/admin-keys")
    async def issue_admin(body: AdminIssue, actor=Depends(writer)):
        gateway = app.state.gateway
        _, snapshot = gateway.control.get()
        if body.expires_at <= time.time() or set(body.teams) - snapshot.teams.keys():
            raise GatewayError("REQUEST_INVALID")
        raw, ident = "gw_admin_" + secrets.token_urlsafe(32), uid("admin")
        snapshot.admins[ident] = AdminKey(
            actor=body.actor,
            role=body.role,
            teams=body.teams,
            expires_at=body.expires_at,
            digest=secret_hash(raw),
        )
        version = await gateway.control.publish(snapshot, body.base_version, actor, body.reason)
        return {"key_id": ident, "key": raw, "version": version}

    @app.post("/admin/v1/admin-keys/{key_id}")
    async def switch_admin(key_id: str, body: Switch, actor=Depends(writer)):
        gateway = app.state.gateway
        _, snapshot = gateway.control.get()
        if key_id not in snapshot.admins:
            raise GatewayError("REQUEST_INVALID")
        snapshot.admins[key_id].enabled = body.enabled
        return {"version": await gateway.control.publish(snapshot, body.base_version, actor, body.reason)}

    @app.post("/admin/v1/connections/{provider_id}")
    async def connection_switch(provider_id: str, body: Switch, actor=Depends(writer)):
        gateway = app.state.gateway
        _, snapshot = gateway.control.get()
        if provider_id not in snapshot.providers:
            raise GatewayError("REQUEST_INVALID")
        snapshot.providers[provider_id].enabled = body.enabled
        return {"version": await gateway.control.publish(snapshot, body.base_version, actor, body.reason)}

    @app.post("/admin/v1/prompts/refresh", status_code=202)
    async def refresh(actor=Depends(writer)):
        gateway = app.state.gateway
        ident = uid("refresh")
        row = {"id": ident, "payload": {"status": "pending", "actor": actor["actor"]}}
        await gateway.recorder.write("prompt_refresh_jobs", row)

        async def run():
            try:
                bundles = await asyncio.to_thread(gateway.prompts.refresh)
                row["payload"].update(status="completed", bundles=bundles)
                await gateway.db.audit(actor, "prompt.refresh", {"job_id": ident, "result": "completed"})
            except Exception:
                row["payload"].update(status="failed", error_code="REQUEST_INVALID")
            await gateway.recorder.write("prompt_refresh_jobs", row)

        task = asyncio.create_task(run())
        app.state.inflight.add(task)
        task.add_done_callback(app.state.inflight.discard)
        return {"job_id": ident, "status": "pending"}

    @app.get("/admin/v1/prompts/refresh/{job_id}")
    async def refresh_result(job_id: str, actor=Depends(writer)):
        from llm_gateway_infrastructure.storage import jobs
        from sqlalchemy import select

        async with app.state.gateway.db.engine.connect() as c:
            row = (await c.execute(select(jobs.c.payload).where(jobs.c.id == job_id))).scalar_one_or_none()
        if row is None:
            raise GatewayError("REQUEST_INVALID", "Refresh job not found")
        return row

    @app.post("/admin/v1/cost-adjustments")
    async def adjustment(body: Adjustment, actor=Depends(writer)):
        _, snapshot = app.state.gateway.control.get()
        if body.team_id not in snapshot.teams or body.currency != snapshot.teams[body.team_id].currency:
            raise GatewayError("REQUEST_INVALID")
        return {"id": await app.state.gateway.db.adjustment(actor, body.team_id, body.model_dump())}

    @app.get("/admin/v1/cost-adjustments")
    async def list_adjustments(
        period: str | None = None,
        offset: int = Query(default=0, ge=0, le=100000),
        limit: int = Query(default=50, ge=1, le=100),
        actor=Depends(admin),
    ):
        teams = report_teams(actor)
        await app.state.gateway.db.audit(actor, "cost.adjustments.read", {"teams": teams, "period": period})
        return {"adjustments": await app.state.gateway.db.adjustment_list(teams, period, offset, limit)}

    return app
