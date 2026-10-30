from __future__ import annotations

import asyncio
import logging
import time

from llm_gateway_core.contracts import GatewayError


class ControlPlane:
    def __init__(self, db, redis, ttl):
        self.db, self.redis, self.ttl = db, redis, ttl
        self.snapshot = None
        self.version = 0
        self.checked = 0.0
        self.lock = asyncio.Lock()

    async def refresh(self, required=False):
        async with self.lock:
            try:
                version, snapshot = await self.db.current()
                self.snapshot, self.version, self.checked = snapshot, version, time.monotonic()
            except Exception as exc:
                if required or not self.snapshot or time.monotonic() - self.checked > self.ttl:
                    raise GatewayError("DEPENDENCY_UNAVAILABLE", "No valid policy snapshot") from exc
        return self.version, self.snapshot

    def get(self):
        if not self.snapshot or time.monotonic() - self.checked > self.ttl:
            raise GatewayError("DEPENDENCY_UNAVAILABLE", "Policy snapshot expired")
        return self.version, self.snapshot.model_copy(deep=True)

    async def publish(self, snapshot, base, actor, reason):
        try:
            version = await self.db.publish(snapshot, base, actor, reason)
        except GatewayError:
            raise
        except Exception as exc:
            raise GatewayError("DEPENDENCY_UNAVAILABLE", "Policy transaction unavailable") from exc
        await self.refresh(required=True)
        try:
            await self.redis.publish("gw:policy.changed", str(version))
        except Exception:
            logging.getLogger(__name__).warning("gateway.config.notification_failed")
        return version

    async def subscribe(self):
        while True:
            try:
                async with self.redis.pubsub() as subscription:
                    await subscription.subscribe("gw:policy.changed")
                    async for message in subscription.listen():
                        if message["type"] == "message":
                            await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).error("gateway.config.subscription_failed")
                await asyncio.sleep(1)
