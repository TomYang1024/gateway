"""Transactional Outbox delivery. Default channel is simulated and sends no external messages."""

from datetime import timedelta

import httpx
from sqlalchemy import select, update

from llm_gateway_infrastructure.storage import now, outbox


class Notifications:
    def __init__(self, db, settings, sender=None):
        self.db, self.settings, self.sender = db, settings, sender

    async def tick(self):
        async with self.db.engine.begin() as c:
            row = await c.execute(
                select(outbox)
                .where(outbox.c.status == "pending", outbox.c.available_at <= now())
                .order_by(outbox.c.available_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            row = row.mappings().first()
            if not row:
                return
            attempt = row["attempts"] + 1
            try:
                if self.sender:
                    await self.sender(row["id"], row["payload"])
                elif row["payload"]["channel"] != "mock":
                    channel = row["payload"]["channel"]
                    if (
                        not self.settings.notifications_enabled
                        or channel not in self.settings.notification_webhooks
                    ):
                        raise RuntimeError("channel_disabled")
                    async with httpx.AsyncClient(
                        timeout=5, follow_redirects=False, trust_env=False
                    ) as client:
                        response = await client.post(
                            self.settings.notification_webhooks[channel],
                            json=row["payload"],
                            headers={"Idempotency-Key": row["id"]},
                        )
                        response.raise_for_status()
                values = {"status": "delivered", "last_error": None}
            except Exception:
                values = {
                    "status": "dead" if attempt >= 3 else "pending",
                    "last_error": "delivery_failed",
                    "available_at": now() + timedelta(seconds=2**attempt),
                }
            await c.execute(update(outbox).where(outbox.c.id == row["id"]).values(attempts=attempt, **values))
