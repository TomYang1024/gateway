import asyncio
import json
import time
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from llm_gateway_core.contracts import Continuation, GatewayError, Identity
from llm_gateway_infrastructure.coordination import Continuations, Quotas
from llm_gateway_infrastructure.notifications import Notifications
from llm_gateway_infrastructure.resilience import Circuits
from llm_gateway_infrastructure.storage import Recorder, now, outbox
from llm_gateway_services.control_plane import ControlPlane
from redis.asyncio import Redis
from sqlalchemy import select, update
from test_gateway import ADMIN, publish

pytestmark = pytest.mark.integration


async def test_atomic_quota_two_instances_and_lease_recovery(infrastructure):
    _, redis, _, redis_url = infrastructure
    other = Redis.from_url(redis_url)
    quotas = [Quotas(redis), Quotas(other)]

    async def acquire(i):
        try:
            return await quotas[i % 2].acquire("shared", 1000, 5, 0.05)
        except GatewayError as exc:
            return exc

    results = await asyncio.gather(*(acquire(i) for i in range(30)))
    assert sum(not isinstance(r, GatewayError) for r in results) == 5
    await asyncio.sleep(0.07)
    lease = await quotas[1].acquire("shared", 1000, 5, 1)
    await quotas[1].release(lease)
    await other.aclose()


async def test_atomic_tpm_and_rpm(infrastructure):
    _, redis, _, _ = infrastructure
    quotas = Quotas(redis)
    leases = [await quotas.acquire("tokens", 100, 100, 2) for _ in range(10)]

    async def reserve(lease):
        try:
            await quotas.reserve(lease, 30, 100)
            return True
        except GatewayError:
            return False

    assert sum(await asyncio.gather(*(reserve(lease) for lease in leases))) == 3
    single = await quotas.acquire("rpm", 1, 10, 2)
    await quotas.release(single)
    with pytest.raises(GatewayError) as exc:
        await quotas.acquire("rpm", 1, 10, 2)
    assert exc.value.code == "GATEWAY_RATE_LIMITED"


async def test_online_reserved_capacity(infrastructure):
    _, redis, _, _ = infrastructure
    quotas = Quotas(redis)
    leases = [await quotas.acquire("pool", 100, 5, 10, "batch", 2) for _ in range(3)]
    with pytest.raises(GatewayError):
        await quotas.acquire("pool", 100, 5, 10, "batch", 2)
    leases.extend([await quotas.acquire("pool", 100, 5, 10, "online", 2) for _ in range(2)])
    with pytest.raises(GatewayError):
        await quotas.acquire("pool", 100, 5, 10, "online", 2)
    for lease in leases:
        await quotas.release(lease)


async def test_redis_unavailable_fails_closed():
    redis = Redis.from_url("redis://127.0.0.1:1/15", socket_connect_timeout=0.1, socket_timeout=0.1)
    try:
        with pytest.raises(GatewayError) as exc:
            await Quotas(redis).acquire("test", 1, 1, 1)
        assert exc.value.code == "DEPENDENCY_UNAVAILABLE"
    finally:
        await redis.aclose()


async def test_persistent_spool_recovery_and_exhaustion(client, basic, tmp_path, monkeypatch, upstream):
    gateway = client.app.state.gateway
    db, recorder = gateway.db, gateway.recorder
    original = db.record

    async def fail(*args, **kwargs):
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(db, "record", fail)
    first = await client.post("/v1/generate", json=basic)
    assert first.status_code == 200
    assert recorder.size() > 0
    recorder.max_bytes = recorder.size()
    second = await client.post("/v1/generate", json=basic)
    assert second.status_code == 503
    assert len(upstream.requests) == 1
    # Queue persists through recorder process lifecycle, then replays idempotently.
    await recorder.close()
    monkeypatch.setattr(db, "record", original)
    recovered = Recorder(db, str(recorder.path.parent), 1000000)
    gateway.recorder = recovered
    await recovered.flush()
    assert recovered.size() == 0
    result = await db.call(first.json()["call_id"], ["demo"])
    assert result["status"] == "completed"
    await recovered.close()
    # Lifespan owns the original recorder; reopen its connection for clean shutdown.
    import sqlite3

    recorder.connection = sqlite3.connect(recorder.path)


async def test_snapshot_loss_notification_compensation(infrastructure, snapshot, monkeypatch):
    db, redis, _, _ = infrastructure
    await db.bootstrap(snapshot)
    control = ControlPlane(db, redis, 0.01)
    await control.refresh()
    old_version = control.version
    snapshot.providers["glm"].enabled = False
    await db.publish(snapshot, old_version, {"actor": "test", "role": "platform_admin"}, "no notification")
    # Polling must converge without receiving Pub/Sub.
    await control.refresh()
    assert not control.get()[1].providers["glm"].enabled

    async def broken():
        raise ConnectionError()

    monkeypatch.setattr(db, "current", broken)
    await control.refresh()  # current valid cached snapshot
    await asyncio.sleep(0.02)
    with pytest.raises(GatewayError):
        await control.refresh()


async def test_continuation_ownership_ttl_and_tamper(infrastructure):
    _, redis, _, _ = infrastructure
    states = Continuations(redis, Fernet.generate_key().decode(), 1)
    owner = Identity(team_id="t", application_id="a", api_key_id="k", auth_policy_version=1)
    other = Identity(team_id="t", application_id="other", api_key_id="k2", auth_policy_version=1)
    ref = await states.save(owner, {"secret_block": "opaque-sensitive"})
    assert b"opaque-sensitive" not in await redis.get(f"gw:continuation:{ref.state_ref}")
    with pytest.raises(GatewayError) as exc:
        await states.load(other, ref)
    assert exc.value.code == "ACCESS_DENIED"
    with pytest.raises(GatewayError) as exc:
        await states.load(owner, Continuation(tool_loop_id="forged", state_ref=ref.state_ref))
    assert exc.value.code == "CONTINUATION_MISMATCH"
    await redis.delete(f"gw:continuation:{ref.state_ref}")
    with pytest.raises(GatewayError) as exc:
        await states.load(owner, ref)
    assert exc.value.code == "CONTINUATION_EXPIRED"


async def test_shared_circuit_half_open_recovery(infrastructure):
    _, redis, _, _ = infrastructure
    first, second = Circuits(redis), Circuits(redis)
    for _ in range(20):
        await first.record("provider", "model", False)
    with pytest.raises(GatewayError):
        await second.before("provider", "model", 1)
    state = json.loads(await redis.get(first.key("provider", "model")))
    state["open_until"] = time.time() - 1
    await redis.set(first.key("provider", "model"), json.dumps(state))
    for _ in range(3):
        await second.before("provider", "model", 1)
        with pytest.raises(GatewayError):
            await first.before("provider", "model", 1)
        await second.record("provider", "model", True)
    await first.before("provider", "model", 1)
    await second.before("provider", "model", 1)  # closed allows concurrent calls


async def test_budget_outbox_dedup_and_failure_queue(client, basic):
    def change(s):
        s["teams"]["demo"].update(
            monthly_budget="0.00005", owner="test-owner", recipients=["mock-group"], channel="mock"
        )

    await publish(client, change)
    gateway = client.app.state.gateway
    for _ in range(3):
        response = await client.post("/v1/generate", json=basic)
        assert response.status_code == 200
    async with gateway.db.engine.connect() as c:
        rows = (await c.execute(select(outbox))).mappings().all()
    assert sorted(r["payload"]["threshold"] for r in rows) == [80, 100]
    row = rows[0]
    delivered_ids = []

    async def broken(ident, payload):
        delivered_ids.append(ident)
        raise OSError("mock failure")

    worker = Notifications(gateway.db, client.app.state.settings, broken)
    for _ in range(3):
        async with gateway.db.engine.begin() as c:
            await c.execute(update(outbox).where(outbox.c.id != row["id"]).values(status="delivered"))
            await c.execute(
                update(outbox)
                .where(outbox.c.id == row["id"])
                .values(available_at=now() - timedelta(seconds=1))
            )
        await worker.tick()
    async with gateway.db.engine.connect() as c:
        result = (await c.execute(select(outbox).where(outbox.c.id == row["id"]))).mappings().one()
    assert result["status"] == "dead"
    assert delivered_ids == [row["id"]] * 3


async def test_hard_budget_and_append_only_adjustments(client, basic):
    def change(s):
        s["teams"]["demo"].update(
            monthly_budget="0.00001", owner="owner", recipients=["mock"], budget_mode="reject"
        )

    await publish(client, change)
    first = await client.post("/v1/generate", json=basic)
    assert first.status_code == 200
    second = await client.post("/v1/generate", json=basic)
    assert second.json()["error"]["code"] == "TEAM_BUDGET_EXCEEDED"
    original = (await client.get(f"/v1/calls/{first.json()['call_id']}")).json()["payload"]["cost"]
    result = await client.post(
        "/admin/v1/cost-adjustments",
        headers=ADMIN,
        json={
            "team_id": "demo",
            "amount": "-0.1",
            "currency": "CNY",
            "period": "2026-10",
            "category": "discount",
            "reason": "invoice reconciliation",
        },
    )
    assert result.status_code == 200
    assert (await client.get(f"/v1/calls/{first.json()['call_id']}")).json()["payload"]["cost"] == original
