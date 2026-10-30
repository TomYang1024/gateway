"""Shared atomic quotas, trusted credentials and encrypted continuation state."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from cryptography.fernet import Fernet, InvalidToken
from llm_gateway_core.contracts import Continuation, GatewayError, uid
from llm_gateway_core.security import fingerprint
from redis.asyncio import Redis
from redis.exceptions import RedisError

# Server time prevents worker clock skew. All keys for one account share a Redis Cluster hash tag.
ACQUIRE = """
local t=redis.call('TIME'); local now=tonumber(t[1])+tonumber(t[2])/1000000
redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',now-60)
redis.call('ZREMRANGEBYSCORE',KEYS[2],'-inf',now)
redis.call('ZREMRANGEBYSCORE',KEYS[3],'-inf',now)
if redis.call('ZCARD',KEYS[1]) >= tonumber(ARGV[2]) then return 1 end
if redis.call('ZCARD',KEYS[2]) >= tonumber(ARGV[3]) then return 2 end
if ARGV[5]=='batch' and redis.call('ZCARD',KEYS[3]) >= tonumber(ARGV[6]) then return 2 end
redis.call('ZADD',KEYS[1],now,ARGV[1]); redis.call('EXPIRE',KEYS[1],120)
redis.call('ZADD',KEYS[2],now+tonumber(ARGV[4]),ARGV[1]); redis.call('EXPIRE',KEYS[2],1200)
if ARGV[5]=='batch' then
redis.call('ZADD',KEYS[3],now+tonumber(ARGV[4]),ARGV[1]); redis.call('EXPIRE',KEYS[3],1200) end
return 0
"""
RESERVE = """
local t=redis.call('TIME'); local now=tonumber(t[1])+tonumber(t[2])/1000000
local expired=redis.call('ZRANGEBYSCORE',KEYS[1],'-inf',now-60)
for _,id in ipairs(expired) do redis.call('HDEL',KEYS[2],id) end
redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',now-60)
local sum=0; for _,v in ipairs(redis.call('HVALS',KEYS[2])) do sum=sum+tonumber(v) end
local old=tonumber(redis.call('HGET',KEYS[2],ARGV[1]) or '0')
if sum-old+tonumber(ARGV[2])>tonumber(ARGV[3]) then return 1 end
redis.call('HSET',KEYS[2],ARGV[1],ARGV[2]); redis.call('ZADD',KEYS[1],now,ARGV[1])
redis.call('EXPIRE',KEYS[1],120); redis.call('EXPIRE',KEYS[2],120)
return 0
"""
RELEASE = """
redis.call('ZREM',KEYS[1],ARGV[1]); redis.call('ZREM',KEYS[2],ARGV[1]); return 0
"""
SETTLE = """
if redis.call('HEXISTS',KEYS[1],ARGV[1])==1 then redis.call('HSET',KEYS[1],ARGV[1],ARGV[2]) end
return 0
"""

LOOP_ADMIT = """
local raw=redis.call('GET',KEYS[1]); if not raw then return 2 end
local s=cjson.decode(raw)
if s.attempts+1>tonumber(ARGV[1]) or s.rounds+tonumber(ARGV[2])>tonumber(ARGV[3]) then return 1 end
if tonumber(ARGV[4])>=0 and (not s.known or s.cost+tonumber(ARGV[5])>tonumber(ARGV[4])) then return 1 end
s.attempts=s.attempts+1; s.rounds=s.rounds+tonumber(ARGV[2]); s.cost=s.cost+tonumber(ARGV[5])
redis.call('SET',KEYS[1],cjson.encode(s),'KEEPTTL'); return 0
"""


@dataclass
class Lease:
    scope: str
    id: str


class Quotas:
    def __init__(self, redis: Redis):
        self.redis = redis

    def key(self, scope, suffix):
        return f"gw:{{{scope}}}:{suffix}"

    async def acquire(self, scope, rpm, concurrency, ttl, pool="online", reserved=0):
        lease = Lease(scope, uid("lease"))
        try:
            result = await self.redis.eval(
                ACQUIRE,
                3,
                self.key(scope, "rpm"),
                self.key(scope, "leases"),
                self.key(scope, "batch"),
                lease.id,
                rpm,
                concurrency,
                ttl,
                pool,
                concurrency - reserved,
            )
        except RedisError as exc:
            raise GatewayError("DEPENDENCY_UNAVAILABLE", "Shared quota coordination unavailable") from exc
        if result:
            raise GatewayError("GATEWAY_RATE_LIMITED", "RPM or concurrency limit reached")
        return lease

    async def reserve(self, lease, tokens, limit):
        try:
            result = await self.redis.eval(
                RESERVE,
                2,
                self.key(lease.scope, "token_times"),
                self.key(lease.scope, "tokens"),
                lease.id,
                tokens,
                limit,
            )
        except RedisError as exc:
            raise GatewayError("DEPENDENCY_UNAVAILABLE", "TPM coordination unavailable") from exc
        if result:
            raise GatewayError("GATEWAY_RATE_LIMITED", "TPM limit reached")

    async def release(self, lease):
        await self.redis.eval(
            RELEASE, 2, self.key(lease.scope, "leases"), self.key(lease.scope, "batch"), lease.id
        )

    async def settle(self, lease, actual: int | None):
        if actual is not None:
            await self.redis.eval(SETTLE, 1, self.key(lease.scope, "tokens"), lease.id, actual)


class Continuations:
    def __init__(self, redis: Redis, key: str, ttl: int):
        self.redis, self.cipher, self.ttl = redis, Fernet(key.encode()), ttl

    def owner_prefix(self, identity):
        return fingerprint([identity.team_id, identity.application_id])[:24]

    def meter_key(self, identity, loop_id):
        return f"gw:loop:{{{self.owner_prefix(identity)}:{loop_id}}}"

    def units(self, value):
        return int((Decimal(value) * 1000000).to_integral_value(rounding=ROUND_CEILING))

    async def reserve_attempt(self, identity, state, max_attempts, max_rounds, estimate, max_cost, new_round):
        limit = -1 if max_cost is None else self.units(max_cost)
        if limit > 9000000000000000:
            raise GatewayError("REQUEST_INVALID", "Tool loop budget exceeds supported monetary range")
        try:
            result = await self.redis.eval(
                LOOP_ADMIT,
                1,
                self.meter_key(identity, state["tool_loop_id"]),
                max_attempts,
                int(new_round),
                max_rounds,
                limit,
                self.units(estimate),
            )
        except RedisError as exc:
            raise GatewayError("DEPENDENCY_UNAVAILABLE", "Tool loop coordination unavailable") from exc
        if result:
            raise GatewayError("CONTINUATION_EXPIRED" if result == 2 else "TOOL_LOOP_LIMIT_EXCEEDED")

    async def save(self, identity, data, loop_id=None):
        loop_id = loop_id or uid("loop")
        ref = f"{self.owner_prefix(identity)}.{uid('state')}"
        payload = {
            **data,
            "team_id": identity.team_id,
            "application_id": identity.application_id,
            "tool_loop_id": loop_id,
            "state_version": 1,
        }
        raw = self.cipher.encrypt(json.dumps(payload, ensure_ascii=False).encode())
        try:
            if "total_attempts" in data:
                meter = {
                    "attempts": data["total_attempts"],
                    "rounds": data["round"],
                    "cost": self.units(data["known_cost"]),
                    "known": data["cost_complete"],
                }
                await self.redis.set(
                    self.meter_key(identity, loop_id),
                    json.dumps(meter),
                    nx=True,
                    ex=self.ttl + max(1, int(data["deadline"] - time.time())),
                )
            await self.redis.set(f"gw:continuation:{ref}", raw, ex=self.ttl)
        except RedisError as exc:
            raise GatewayError("DEPENDENCY_UNAVAILABLE", "Continuation storage unavailable") from exc
        return Continuation(tool_loop_id=loop_id, state_ref=ref)

    async def load(self, identity, ref):
        if not ref.state_ref.startswith(self.owner_prefix(identity) + "."):
            raise GatewayError("ACCESS_DENIED", "Continuation is outside application scope")
        try:
            raw = await self.redis.get(f"gw:continuation:{ref.state_ref}")
        except RedisError as exc:
            raise GatewayError("DEPENDENCY_UNAVAILABLE", "Continuation storage unavailable") from exc
        if not raw:
            raise GatewayError("CONTINUATION_EXPIRED")
        try:
            payload = json.loads(self.cipher.decrypt(raw, ttl=self.ttl))
        except (InvalidToken, ValueError) as exc:
            raise GatewayError("CONTINUATION_MISMATCH") from exc
        if payload["team_id"] != identity.team_id or payload["application_id"] != identity.application_id:
            raise GatewayError("ACCESS_DENIED")
        if payload["tool_loop_id"] != ref.tool_loop_id:
            raise GatewayError("CONTINUATION_MISMATCH")
        return payload
