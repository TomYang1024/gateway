"""PostgreSQL source of truth and bounded, durable per-instance recorder spool."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from llm_gateway_core.config import Snapshot
from llm_gateway_core.contracts import GatewayError, uid
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

metadata = MetaData()

policies = Table(
    "policy_versions",
    metadata,
    Column("version", Integer, primary_key=True, autoincrement=True),
    Column("snapshot", JSON, nullable=False),
    Column("reason", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
head = Table(
    "policy_head",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("version", Integer, nullable=False),
)
audit = Table(
    "admin_audit_logs",
    metadata,
    Column("id", String, primary_key=True),
    Column("actor", String, nullable=False),
    Column("role", String, nullable=False),
    Column("action", String, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
calls = Table(
    "llm_calls",
    metadata,
    Column("id", String, primary_key=True),
    Column("team_id", String, index=True, nullable=False),
    Column("application_id", String, index=True, nullable=False),
    Column("logical_model", String, index=True, nullable=False),
    Column("business_ref", String, index=True),
    Column("status", String, index=True, nullable=False),
    Column("created_at", DateTime(timezone=True), index=True, nullable=False),
    Column("payload", JSON, nullable=False),
)
attempts = Table(
    "llm_attempts",
    metadata,
    Column("id", String, primary_key=True),
    Column("call_id", String, index=True, nullable=False),
    Column("payload", JSON, nullable=False),
)
routes = Table(
    "llm_route_decisions",
    metadata,
    Column("id", String, primary_key=True),
    Column("call_id", String, index=True, nullable=False),
    Column("payload", JSON, nullable=False),
)
events = Table(
    "llm_call_events",
    metadata,
    Column("id", String, primary_key=True),
    Column("call_id", String, index=True, nullable=False),
    Column("payload", JSON, nullable=False),
)
content_refs = Table(
    "llm_content_refs",
    metadata,
    Column("id", String, primary_key=True),
    Column("call_id", String, index=True, nullable=False),
    Column("payload", JSON, nullable=False),
)
budget = Table(
    "budget_periods",
    metadata,
    Column("id", String, primary_key=True),
    Column("team_id", String, nullable=False),
    Column("period", String, nullable=False),
    Column("amount", Numeric(30, 12), nullable=False),
    Column("complete", Boolean, nullable=False),
    Column("updated_at", DateTime(timezone=True)),
)
settlements = Table("call_settlements", metadata, Column("call_id", String, primary_key=True))
outbox = Table(
    "notification_outbox",
    metadata,
    Column("id", String, primary_key=True),
    Column("dedup_key", String, unique=True, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("status", String, index=True, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("available_at", DateTime(timezone=True)),
    Column("last_error", String),
)
adjustments = Table(
    "cost_adjustments",
    metadata,
    Column("id", String, primary_key=True),
    Column("team_id", String, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
jobs = Table(
    "prompt_refresh_jobs",
    metadata,
    Column("id", String, primary_key=True),
    Column("payload", JSON, nullable=False),
)
TABLES = {t.name: t for t in (calls, attempts, routes, events, content_refs, jobs)}


def now():
    return datetime.now(UTC)


class Database:
    def __init__(self, url: str):
        self.engine = create_async_engine(
            url,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=10,
            connect_args={"timeout": 3, "command_timeout": 5},
        )

    async def ping(self):
        async with self.engine.connect() as c:
            await c.execute(select(head.c.version).limit(1))

    async def bootstrap(self, snapshot: Snapshot):
        async with self.engine.begin() as c:
            # Serialize first bootstrap across workers and never overwrite published policy.
            await c.execute(select(func.pg_advisory_xact_lock(9382341)))
            if (await c.execute(select(head))).first() is None:
                version = (
                    await c.execute(
                        insert(policies)
                        .values(
                            snapshot=snapshot.model_dump(mode="json"), reason="bootstrap", created_at=now()
                        )
                        .returning(policies.c.version)
                    )
                ).scalar_one()
                await c.execute(insert(head).values(id=1, version=version))

    async def current(self):
        async with self.engine.connect() as c:
            row = (
                (await c.execute(select(policies).join(head, policies.c.version == head.c.version)))
                .mappings()
                .one()
            )
        return row["version"], Snapshot.model_validate(row["snapshot"])

    async def version(self, version: int):
        async with self.engine.connect() as c:
            row = (
                await c.execute(select(policies.c.snapshot).where(policies.c.version == version))
            ).scalar_one_or_none()
        if row is None:
            raise GatewayError("REQUEST_INVALID", "Unknown policy version")
        return Snapshot.model_validate(row)

    async def publish(self, snapshot: Snapshot, base: int, actor: dict, reason: str):
        async with self.engine.begin() as c:
            active = (
                await c.execute(select(head.c.version).where(head.c.id == 1).with_for_update())
            ).scalar_one()
            if active != base:
                raise GatewayError("CONFIG_CONFLICT", "Policy base version changed")
            previous = (
                await c.execute(select(policies.c.snapshot).where(policies.c.version == active))
            ).scalar_one()
            changed = []

            def diff(a, b, prefix=""):
                if isinstance(a, dict) and isinstance(b, dict):
                    for key in sorted(a.keys() | b.keys()):
                        diff(a.get(key), b.get(key), f"{prefix}/{key}")
                elif a != b:
                    changed.append(prefix)

            diff(previous, snapshot.model_dump(mode="json"))
            version = (
                await c.execute(
                    insert(policies)
                    .values(snapshot=snapshot.model_dump(mode="json"), reason=reason, created_at=now())
                    .returning(policies.c.version)
                )
            ).scalar_one()
            await c.execute(update(head).values(version=version).where(head.c.id == 1))
            await self.audit(
                actor,
                "policy.publish",
                {"before": base, "after": version, "reason": reason, "changed_paths": changed},
                c,
            )
        return version

    async def audit(self, actor: dict, action: str, payload: dict, connection=None):
        values = dict(
            id=uid("audit"),
            actor=actor["actor"],
            role=actor["role"],
            action=action,
            payload=payload,
            created_at=now(),
        )
        if connection is not None:
            await connection.execute(insert(audit).values(**values))
        else:
            async with self.engine.begin() as c:
                await c.execute(insert(audit).values(**values))

    async def record(self, table: str, row: dict, settlement: dict | None = None):
        async with self.engine.begin() as c:
            target = TABLES[table]
            row = dict(row)
            if "created_at" in row and isinstance(row["created_at"], str):
                row["created_at"] = datetime.fromisoformat(row["created_at"])
            statement = pg_insert(target).values(**row)
            await c.execute(
                statement.on_conflict_do_update(
                    index_elements=[target.c.id], set_={k: v for k, v in row.items() if k != "id"}
                )
            )
            if settlement:
                await self._settle(c, row["id"], settlement)

    async def _settle(self, c, call_id, s):
        inserted = (
            await c.execute(
                pg_insert(settlements)
                .values(call_id=call_id)
                .on_conflict_do_nothing()
                .returning(settlements.c.call_id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            return
        key = f"{s['team_id']}:{s['period']}"
        await c.execute(
            pg_insert(budget)
            .values(
                id=key, team_id=s["team_id"], period=s["period"], amount=0, complete=True, updated_at=now()
            )
            .on_conflict_do_nothing()
        )
        old = (await c.execute(select(budget).where(budget.c.id == key).with_for_update())).mappings().one()
        amount = old["amount"] + Decimal(s["known_amount"])
        complete = old["complete"] and s["complete"]
        await c.execute(
            update(budget)
            .where(budget.c.id == key)
            .values(amount=amount, complete=complete, updated_at=now())
        )
        team = s["team"]
        notices = []
        if not complete:
            notices.append(("data_quality", None))
        if team.get("monthly_budget"):
            for threshold in team["thresholds"]:
                if amount >= Decimal(team["monthly_budget"]) * Decimal(threshold) / 100:
                    notices.append((str(threshold), threshold))
        for suffix, threshold in notices:
            # Team/period/threshold dedup is deliberately stronger than per-policy dedup.
            dedup = f"{key}:{suffix}"
            payload = {
                "team_id": s["team_id"],
                "period": s["period"],
                "threshold": threshold,
                "policy_version": s["version"],
                "amount": str(amount),
                "complete": complete,
                "owner": team["owner"],
                "recipients": team["recipients"],
                "channel": team["channel"],
            }
            await c.execute(
                pg_insert(outbox)
                .values(
                    id=uid("notice"),
                    dedup_key=dedup,
                    payload=payload,
                    status="pending",
                    attempts=0,
                    available_at=now(),
                )
                .on_conflict_do_nothing()
            )

    async def budget_status(self, team_id, team):
        period = now().astimezone(ZoneInfo(team.timezone)).strftime("%Y-%m")
        async with self.engine.connect() as c:
            row = (
                (await c.execute(select(budget).where(budget.c.id == f"{team_id}:{period}")))
                .mappings()
                .first()
            )
        return row

    async def query_calls(
        self,
        *,
        team_ids: list[str],
        application_id=None,
        business_ref=None,
        logical_model=None,
        status=None,
        since=None,
        until=None,
        offset=0,
        limit=50,
    ):
        q = select(calls).where(calls.c.team_id.in_(team_ids))
        for column, value in (
            (calls.c.application_id, application_id),
            (calls.c.business_ref, business_ref),
            (calls.c.logical_model, logical_model),
            (calls.c.status, status),
        ):
            if value is not None:
                q = q.where(column == value)
        if since:
            q = q.where(calls.c.created_at >= since)
        if until:
            q = q.where(calls.c.created_at < until)
        async with self.engine.connect() as c:
            return [
                dict(r)
                for r in (
                    await c.execute(
                        q.order_by(calls.c.created_at.desc(), calls.c.id).offset(offset).limit(limit)
                    )
                ).mappings()
            ]

    async def call(self, call_id, team_ids, application_id=None):
        q = select(calls).where(calls.c.id == call_id, calls.c.team_id.in_(team_ids))
        if application_id:
            q = q.where(calls.c.application_id == application_id)
        async with self.engine.connect() as c:
            row = (await c.execute(q)).mappings().first()
            if row is None:
                raise GatewayError("ACCESS_DENIED", "Call unavailable in authorized scope")
            result = dict(row)
            result["attempts"] = [
                r[0]
                for r in (
                    await c.execute(select(attempts.c.payload).where(attempts.c.call_id == call_id))
                ).all()
            ]
            result["route_decisions"] = [
                r[0]
                for r in (await c.execute(select(routes.c.payload).where(routes.c.call_id == call_id))).all()
            ]
            return result

    async def report(
        self,
        team_ids,
        application_id=None,
        group_by="logical_model",
        since=None,
        until=None,
        offset=0,
        limit=100,
    ):
        # SQL aggregation: reporting never loads an unbounded call list into worker memory.
        columns = {
            "team_id": calls.c.team_id,
            "application_id": calls.c.application_id,
            "logical_model": calls.c.logical_model,
            "model": calls.c.payload["actual_model"].as_string(),
            "prompt_version": calls.c.payload["prompt_version"].as_string(),
        }
        group = columns[group_by]
        currency = calls.c.payload["cost"]["currency"].as_string()
        q = select(
            group.label("group"),
            currency.label("currency"),
            func.count().label("calls"),
            func.count().filter(calls.c.status == "failed").label("failures"),
            func.count()
            .filter(calls.c.payload["cost"]["complete"].as_boolean().is_(False))
            .label("incomplete_cost_calls"),
            func.count()
            .filter(calls.c.payload["usage"]["usage_complete"].as_boolean().is_(False))
            .label("incomplete_usage_calls"),
            func.sum(calls.c.payload["cost"]["known_amount"].as_string().cast(Numeric(30, 12))).label(
                "known_cost"
            ),
            func.sum(calls.c.payload["usage"]["input_tokens"].as_integer()).label("input_tokens"),
            func.sum(calls.c.payload["usage"]["output_tokens"].as_integer()).label("output_tokens"),
        )
        q = q.where(calls.c.team_id.in_(team_ids), calls.c.status != "running")
        if application_id:
            q = q.where(calls.c.application_id == application_id)
        if since:
            q = q.where(calls.c.created_at >= since)
        if until:
            q = q.where(calls.c.created_at < until)
        async with self.engine.connect() as c:
            rows = [
                dict(r)
                for r in (
                    await c.execute(
                        q.group_by(group, currency).order_by(group, currency).offset(offset).limit(limit)
                    )
                ).mappings()
            ]
            for row in rows:
                row["known_cost"] = str(row["known_cost"]) if row["known_cost"] is not None else None
            return rows

    async def adjustment_list(self, team_ids, period=None, offset=0, limit=50):
        query = select(adjustments).where(adjustments.c.team_id.in_(team_ids))
        if period:
            query = query.where(adjustments.c.payload["period"].as_string() == period)
        async with self.engine.connect() as c:
            return [
                dict(r)
                for r in (
                    await c.execute(
                        query.order_by(adjustments.c.created_at.desc()).offset(offset).limit(limit)
                    )
                ).mappings()
            ]

    async def adjustment(self, actor, team_id, payload):
        ident = uid("adjustment")
        async with self.engine.begin() as c:
            await c.execute(
                insert(adjustments).values(id=ident, team_id=team_id, payload=payload, created_at=now())
            )
            await self.audit(actor, "cost.adjustment", {"id": ident, "team_id": team_id, **payload}, c)
        return ident


class Recorder:
    """Write-ahead SQLite queue; PostgreSQL replay is ordered and idempotent.

    Each replica MUST have its own persistent spool volume. SQLite is never used
    as the source of shared authentication, quota, policy, or reports.
    """

    def __init__(self, db: Database, directory: str, max_bytes: int):
        Path(directory).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = Path(directory) / "recorder.sqlite3"
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS queue (id INTEGER PRIMARY KEY, body TEXT NOT NULL)"
        )
        self.connection.commit()
        self.db, self.max_bytes = db, max_bytes
        self.lock = asyncio.Lock()
        self.failed = False

    def size(self):
        return self.connection.execute("SELECT COALESCE(SUM(length(body)),0) FROM queue").fetchone()[0]

    def ensure_capacity(self):
        if self.size() >= self.max_bytes or self.failed:
            raise GatewayError("DEPENDENCY_UNAVAILABLE", "Recorder durable buffer unavailable")

    async def write(self, table: str, row: dict, settlement=None):
        body = json.dumps(
            {"table": table, "row": row, "settlement": settlement}, default=str, separators=(",", ":")
        )
        try:
            # In-flight terminal records are attempted even after admission capacity is exhausted.
            self.connection.execute("INSERT INTO queue(body) VALUES (?)", (body,))
            self.connection.commit()
        except sqlite3.Error as exc:
            self.failed = True
            logging.getLogger(__name__).error("gateway.recorder.failed")
            raise GatewayError("DEPENDENCY_UNAVAILABLE", "Recorder durable write failed") from exc
        await self.flush()

    async def flush(self):
        async with self.lock:
            for ident, raw in self.connection.execute(
                "SELECT id,body FROM queue ORDER BY id LIMIT 200"
            ).fetchall():
                item = json.loads(raw)
                try:
                    await self.db.record(item["table"], item["row"], item["settlement"])
                except Exception:
                    logging.getLogger(__name__).error("gateway.recorder.failed")
                    return False
                self.connection.execute("DELETE FROM queue WHERE id=?", (ident,))
                self.connection.commit()
            self.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
            return True

    async def close(self):
        await self.flush()
        self.connection.close()
