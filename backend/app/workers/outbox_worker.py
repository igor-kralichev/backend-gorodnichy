from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

import aio_pika
from aio_pika import DeliveryMode, Message
from sqlalchemy import select

from app.core.config import settings
from app.core.database import session_factory
from app.shared.db.models import OutboxEventModel

log = logging.getLogger(__name__)


async def main() -> None:
    """Публиковать подтверждённые события БД в RabbitMQ."""

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log.info("Outbox worker публикует события в очередь %s", settings.outbox_queue_name)
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    async with connection:
        channel = await connection.channel()
        queue = await channel.declare_queue(settings.outbox_queue_name, durable=True)
        while True:
            processed = await _publish_batch(channel, queue.name)
            if processed == 0:
                await asyncio.sleep(settings.outbox_poll_interval_seconds)


async def _publish_batch(channel, routing_key: str) -> int:
    async with session_factory() as session:
        async with session.begin():
            events = list(
                await session.scalars(
                    select(OutboxEventModel)
                    .where(OutboxEventModel.published_at.is_(None))
                    .order_by(OutboxEventModel.occurred_at, OutboxEventModel.id)
                    .limit(settings.outbox_batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            published = 0
            for event in events:
                try:
                    payload = {
                        "eventId": str(event.id),
                        "eventType": event.event_type,
                        "aggregateType": event.aggregate_type,
                        "aggregateId": str(event.aggregate_id),
                        "occurredAt": event.occurred_at.isoformat(),
                        "payload": event.payload,
                    }
                    await channel.default_exchange.publish(
                        Message(
                            body=json.dumps(payload, ensure_ascii=False).encode(),
                            content_type="application/json",
                            delivery_mode=DeliveryMode.PERSISTENT,
                            message_id=str(event.id),
                        ),
                        routing_key=routing_key,
                    )
                except Exception as error:
                    event.attempts += 1
                    event.last_error = str(error)[:4_000]
                    log.exception("Не удалось опубликовать outbox-событие %s", event.id)
                    continue
                event.published_at = datetime.now(UTC)
                event.attempts += 1
                event.last_error = None
                published += 1
            return published


if __name__ == "__main__":
    asyncio.run(main())
