from __future__ import annotations

import json
from typing import Any

import aio_pika
from aio_pika import DeliveryMode, Message

from app.core.config import settings


async def publish_json(queue_name: str, payload: dict[str, Any]) -> None:
    """Опубликовать JSON-сообщение в durable очередь RabbitMQ."""

    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    async with connection:
        channel = await connection.channel()
        queue = await channel.declare_queue(queue_name, durable=True)
        message = Message(
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
            delivery_mode=DeliveryMode.PERSISTENT,
        )
        await channel.default_exchange.publish(message, routing_key=queue.name)
