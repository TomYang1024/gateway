"""Shared per-connection/model rolling circuit breaker with a single half-open probe."""

import json

from llm_gateway_core.contracts import GatewayError
from redis.exceptions import RedisError

BEFORE = """
local t=redis.call('TIME'); local now=tonumber(t[1])+tonumber(t[2])/1000000
local raw=redis.call('GET',KEYS[1]); if not raw then return 0 end
local s=cjson.decode(raw)
if s.open_until and s.open_until>now then return 1 end
if s.open_until then
 if s.probe_until and s.probe_until>now then return 1 end
 s.probe_until=now+tonumber(ARGV[1]); redis.call('SET',KEYS[1],cjson.encode(s),'EX',1200)
end
return 0
"""
RECORD = """
local t=redis.call('TIME'); local now=tonumber(t[1])+tonumber(t[2])/1000000
local raw=redis.call('GET',KEYS[1]); local s={samples={},successes=0}
if raw then s=cjson.decode(raw) end
local ok=ARGV[1]=='1'; local cooldown=tonumber(ARGV[2])
if cooldown>0 then s.open_until=now+cooldown; s.successes=0
elseif s.open_until then
 s.probe_until=nil
 if ok then
  s.successes=(s.successes or 0)+1
  if s.successes>=3 then s={samples={},successes=0} end
 else s.open_until=now+30; s.successes=0 end
else
 local samples={}; local failures=0
 for _,v in ipairs(s.samples) do
  if v[1]>now-30 then table.insert(samples,v); if v[2]==0 then failures=failures+1 end end
 end
 table.insert(samples,{now,ok and 1 or 0}); if not ok then failures=failures+1 end
 s.samples=samples
 if #samples>=20 and failures/#samples>=0.5 then s.open_until=now+30; s.successes=0 end
end
redis.call('SET',KEYS[1],cjson.encode(s),'EX',1200); return 0
"""


class Circuits:
    def __init__(self, redis):
        self.redis = redis

    def key(self, provider, model):
        return f"gw:circuit:{{{provider}:{model}}}"

    async def unavailable(self, candidates):
        try:
            seconds, micros = await self.redis.time()
            current = seconds + micros / 1000000
            async with self.redis.pipeline(transaction=False) as pipeline:
                for c in candidates:
                    pipeline.get(self.key(c.provider, c.model))
                states = await pipeline.execute()
        except RedisError as exc:
            raise GatewayError("DEPENDENCY_UNAVAILABLE", "Circuit state unavailable") from exc
        blocked = set()
        for c, raw in zip(candidates, states, strict=True):
            state = json.loads(raw) if raw else {}
            if state.get("open_until", 0) > current or state.get("probe_until", 0) > current:
                blocked.add((c.provider, c.model))
        return blocked

    async def before(self, provider, model, remaining_s):
        try:
            denied = await self.redis.eval(BEFORE, 1, self.key(provider, model), remaining_s + 5)
        except RedisError as exc:
            raise GatewayError("DEPENDENCY_UNAVAILABLE", "Circuit coordination unavailable") from exc
        if denied:
            raise GatewayError("UPSTREAM_UNAVAILABLE", "Candidate circuit is open")

    async def record(self, provider, model, ok, cooldown=0):
        await self.redis.eval(RECORD, 1, self.key(provider, model), int(ok), cooldown)
