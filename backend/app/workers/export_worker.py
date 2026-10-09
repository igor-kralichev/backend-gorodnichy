from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from uuid import UUID

import aio_pika
from aio_pika.abc import AbstractIncomingMessage
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.database import session_factory
from app.core.security import ActorContext
from app.core.storage import get_minio_client
from app.modules.access.application.service import (
    AccessDenied,
    AccessResourceNotFound,
    AuthorizationService,
)
from app.modules.excel.api.schemas import ExcelExportRequest
from app.modules.excel.application.exports import upload_export_result
from app.modules.excel.application.service import ExcelService
from app.shared.db.models import ExcelExportJobModel, NotificationModel

log = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)
        queue = await channel.declare_queue(
            settings.excel_export_queue_name,
            durable=True,
        )
        log.info("Export worker слушает очередь %s", queue.name)
        await queue.consume(_handle_message)
        cleanup_task = asyncio.create_task(_cleanup_expired_results())
        try:
            await asyncio.Future()
        finally:
            cleanup_task.cancel()


async def _handle_message(message: AbstractIncomingMessage) -> None:
    async with message.process(requeue=False):
        payload = json.loads(message.body.decode("utf-8"))
        job_id = UUID(payload["jobId"])
        actor = ActorContext(
            id=UUID(payload["actorId"]),
            role="",
            roles=frozenset(str(item) for item in payload.get("actorRoles", [])),
            username=payload.get("actorUsername"),
            display_name=payload.get("actorDisplayName"),
            email=payload.get("actorEmail"),
        )
        try:
            await _run_export(job_id, str(payload["entityCode"]), actor)
        except Exception as error:
            log.exception("Ошибка фонового экспорта %s", job_id)
            await _finish_failed(job_id, actor.id, _public_error(error))


async def _run_export(
    job_id: UUID,
    entity_code: str,
    actor: ActorContext,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            job = await session.scalar(
                select(ExcelExportJobModel)
                .where(ExcelExportJobModel.id == job_id)
                .with_for_update()
            )
            if job is None or job.status == "cancelled":
                return
            if job.status == "completed":
                return
            job.status = "running"
            request_payload = dict(job.request_payload)

        request = ExcelExportRequest.model_validate(request_payload)
        authorization = AuthorizationService(session)
        await authorization.require_entity_code(actor, "export", entity_code)
        for child in request.children:
            await authorization.require_entity_code(
                actor,
                "export",
                child.entity_code,
            )
        await session.rollback()

        filename, content, total_rows = await ExcelService(session).export_tree(
            entity_code,
            request,
            actor=actor,
            max_rows=settings.excel_export_max_rows,
        )

    if await _is_cancelled(job_id):
        return
    storage = get_minio_client()
    object_key = await upload_export_result(
        storage,
        job_id=job_id,
        actor_id=actor.id,
        filename=filename,
        content=content,
    )

    async with session_factory() as session:
        async with session.begin():
            job = await session.scalar(
                select(ExcelExportJobModel)
                .where(ExcelExportJobModel.id == job_id)
                .with_for_update()
            )
            if job is None or job.status == "cancelled":
                await run_in_threadpool(
                    storage.remove_object,
                    settings.minio_bucket,
                    object_key,
                )
                return
            job.status = "completed"
            job.result_object_key = object_key
            job.result_filename = filename
            job.total_rows = total_rows
            job.error_message = None
            session.add(
                NotificationModel(
                    user_id=actor.id,
                    type="excel_export.completed",
                    title="Выгрузка Excel готова",
                    message=f"Файл {filename} сформирован и доступен для скачивания.",
                    resource_type="excel_export_job",
                    resource_id=job_id,
                    metadata_json={"downloadUrl": f"/api/v1/excel/exportJobs/{job_id}/download"},
                )
            )


async def _is_cancelled(job_id: UUID) -> bool:
    async with session_factory() as session:
        status = await session.scalar(
            select(ExcelExportJobModel.status).where(ExcelExportJobModel.id == job_id)
        )
        return status in {None, "cancelled"}


async def _finish_failed(job_id: UUID, actor_id: UUID, message: str) -> None:
    async with session_factory() as session:
        async with session.begin():
            job = await session.scalar(
                select(ExcelExportJobModel)
                .where(ExcelExportJobModel.id == job_id)
                .with_for_update()
            )
            if job is None or job.status == "cancelled":
                return
            job.status = "failed"
            job.error_message = (message or "Неизвестная ошибка экспорта")[:4000]
            session.add(
                NotificationModel(
                    user_id=actor_id,
                    type="excel_export.failed",
                    title="Выгрузка Excel не сформирована",
                    message=job.error_message,
                    resource_type="excel_export_job",
                    resource_id=job_id,
                )
            )


def _public_error(error: Exception) -> str:
    if isinstance(error, AccessDenied):
        return "Недостаточно прав для экспорта реестра или подреестра"
    if isinstance(error, AccessResourceNotFound):
        return "Сущность для экспорта не найдена"
    return str(error) or "Неизвестная ошибка экспорта"


async def _cleanup_expired_results() -> None:
    """Удалять истёкшие файлы из MinIO, сохраняя запись задачи."""

    storage = get_minio_client()
    while True:
        try:
            async with session_factory() as session:
                jobs = list(
                    (
                        await session.scalars(
                            select(ExcelExportJobModel)
                            .where(
                                ExcelExportJobModel.status == "completed",
                                ExcelExportJobModel.expires_at <= datetime.now(UTC),
                                ExcelExportJobModel.result_object_key.is_not(None),
                            )
                            .order_by(ExcelExportJobModel.expires_at)
                            .limit(100)
                        )
                    ).all()
                )
                for job in jobs:
                    await run_in_threadpool(
                        storage.remove_object,
                        settings.minio_bucket,
                        job.result_object_key,
                    )
                    job.status = "failed"
                    job.error_message = "Срок хранения файла экспорта истёк"
                    job.result_object_key = None
                if jobs:
                    await session.commit()
        except Exception:
            log.exception("Не удалось очистить истёкшие XLSX-выгрузки")
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
