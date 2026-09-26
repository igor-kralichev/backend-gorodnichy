from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from typing import Any
from uuid import UUID

import aio_pika
from aio_pika.abc import AbstractIncomingMessage
from minio import Minio
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.database import session_factory
from app.core.storage import get_minio_client
from app.modules.objects.application.imports import ObjectImportService
from app.modules.objects.application.service import RuntimeObjectService, RuntimeValidationError
from app.shared.db.models import ImportJobModel, ImportRowModel

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
        queue = await channel.declare_queue(settings.import_queue_name, durable=True)
        log.info("Import worker слушает очередь %s", queue.name)
        await queue.consume(_handle_message)
        await asyncio.Future()


async def _handle_message(message: AbstractIncomingMessage) -> None:
    async with message.process(requeue=False):
        payload = json.loads(message.body.decode("utf-8"))
        job_id = UUID(payload["jobId"])
        entity_code = str(payload["entityCode"])
        actor_id = UUID(payload["actorId"])
        storage_key = str(payload["sourceObjectKey"])
        storage = get_minio_client()
        try:
            await _run_import(job_id, entity_code, actor_id, storage_key, storage)
        finally:
            await _delete_temp_file(storage, storage_key)


async def _run_import(
    job_id: UUID,
    entity_code: str,
    actor_id: UUID,
    storage_key: str,
    storage: Minio,
) -> None:
    await _mark_job_running(job_id)
    content = await _read_temp_file(storage, storage_key)
    async with session_factory() as session:
        service = ObjectImportService(session, storage)
        payloads = await service.parse_json_objects(content)
        await _set_total_rows(job_id, len(payloads))

    start = 0
    try:
        while start < len(payloads):
            should_continue = await _wait_until_allowed(job_id)
            if not should_continue:
                return
            batch = payloads[start : start + settings.import_batch_size]
            await _import_batch(job_id, entity_code, actor_id, start + 1, batch)
            start += len(batch)
        await _finish_job(job_id, "completed")
    except Exception:
        log.exception("Ошибка фонового импорта %s", job_id)
        await _finish_job(job_id, "failed")


async def _import_batch(
    job_id: UUID,
    entity_code: str,
    actor_id: UUID,
    first_row_number: int,
    batch: Sequence[Any],
) -> None:
    async with session_factory() as session:
        service = RuntimeObjectService(session)
        try:
            response = await service.create_many(entity_code, list(batch), actor_id)
        except RuntimeValidationError:
            await _import_batch_one_by_one(job_id, entity_code, actor_id, first_row_number, batch)
            return

        rows: list[ImportRowModel] = []
        error_rows = 0
        for index, item in enumerate(response.data):
            status = "imported" if item.data_quality == "complete" else "incomplete"
            if status == "incomplete":
                error_rows += 1
            rows.append(
                ImportRowModel(
                    import_job_id=job_id,
                    object_id=item.id,
                    row_number=first_row_number + index,
                    status=status,
                    raw_values=batch[index].model_dump(mode="json", by_alias=True),
                    normalized_values=item.values,
                    errors=[issue.model_dump(mode="json", by_alias=True) for issue in item.validation_errors],
                )
            )
        await _append_rows_and_progress(session, job_id, rows, processed=len(rows), error_rows=error_rows)


async def _import_batch_one_by_one(
    job_id: UUID,
    entity_code: str,
    actor_id: UUID,
    first_row_number: int,
    batch: Sequence[Any],
) -> None:
    for index, payload in enumerate(batch):
        async with session_factory() as session:
            service = RuntimeObjectService(session)
            try:
                response = await service.create_many(entity_code, [payload], actor_id)
            except RuntimeValidationError as error:
                row = ImportRowModel(
                    import_job_id=job_id,
                    object_id=None,
                    row_number=first_row_number + index,
                    status="failed",
                    raw_values=payload.model_dump(mode="json", by_alias=True),
                    normalized_values={},
                    errors=error.issues,
                )
                await _append_rows_and_progress(session, job_id, [row], processed=1, error_rows=1)
                continue

            item = response.data[0]
            status = "imported" if item.data_quality == "complete" else "incomplete"
            row = ImportRowModel(
                import_job_id=job_id,
                object_id=item.id,
                row_number=first_row_number + index,
                status=status,
                raw_values=payload.model_dump(mode="json", by_alias=True),
                normalized_values=item.values,
                errors=[issue.model_dump(mode="json", by_alias=True) for issue in item.validation_errors],
            )
            await _append_rows_and_progress(
                session,
                job_id,
                [row],
                processed=1,
                error_rows=1 if status == "incomplete" else 0,
            )


async def _append_rows_and_progress(
    session,
    job_id: UUID,
    rows: list[ImportRowModel],
    *,
    processed: int,
    error_rows: int,
) -> None:
    async with session.begin():
        job = await session.get(ImportJobModel, job_id, with_for_update=True)
        if job is None:
            return
        session.add_all(rows)
        job.processed_rows += processed
        job.error_rows += error_rows


async def _mark_job_running(job_id: UUID) -> None:
    async with session_factory() as session:
        async with session.begin():
            job = await session.get(ImportJobModel, job_id, with_for_update=True)
            if job is not None and job.status == "queued":
                job.status = "running"


async def _set_total_rows(job_id: UUID, total_rows: int) -> None:
    async with session_factory() as session:
        async with session.begin():
            job = await session.get(ImportJobModel, job_id, with_for_update=True)
            if job is not None:
                job.total_rows = total_rows


async def _wait_until_allowed(job_id: UUID) -> bool:
    while True:
        async with session_factory() as session:
            status = await session.scalar(select(ImportJobModel.status).where(ImportJobModel.id == job_id))
        if status == "cancelled":
            return False
        if status == "paused":
            await asyncio.sleep(1)
            continue
        return status in {"queued", "running"}


async def _finish_job(job_id: UUID, status: str) -> None:
    async with session_factory() as session:
        async with session.begin():
            job = await session.get(ImportJobModel, job_id, with_for_update=True)
            if job is not None and job.status != "cancelled":
                job.status = status


async def _read_temp_file(storage: Minio, storage_key: str) -> bytes:
    def read_object() -> bytes:
        response = storage.get_object(settings.minio_bucket, storage_key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    return await run_in_threadpool(read_object)


async def _delete_temp_file(storage: Minio, storage_key: str) -> None:
    try:
        await run_in_threadpool(storage.remove_object, settings.minio_bucket, storage_key)
    except Exception:
        log.warning("Не удалось удалить временный файл импорта %s", storage_key, exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
