from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import aio_pika
from aio_pika.abc import AbstractIncomingMessage
from minio import Minio
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.database import session_factory
from app.core.storage import get_minio_client
from app.core.security import ActorContext
from app.modules.access.application.service import AuthorizationService
from app.modules.objects.application.imports import ObjectImportService
from app.modules.objects.application.service import RuntimeObjectService, RuntimeValidationError
from app.modules.excel.api.schemas import ExcelMapping
from app.modules.excel.application.service import ExcelService
from app.modules.change_sets.api.schemas import ChangeSetCreate
from app.modules.change_sets.application.service import ChangeSetService
from app.shared.db.models import (
    EntityObjectModel,
    ImportJobModel,
    ImportRowModel,
    NotificationModel,
)

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
        actor = ActorContext(
            id=actor_id,
            role="",
            roles=frozenset(str(item) for item in payload.get("actorRoles", [])),
            username=payload.get("actorUsername"),
            display_name=payload.get("actorDisplayName"),
            email=payload.get("actorEmail"),
        )
        storage_key = str(payload["sourceObjectKey"])
        source_format = str(payload.get("sourceFormat", "json"))
        mapping = payload.get("mapping") if isinstance(payload.get("mapping"), dict) else {}
        storage = get_minio_client()
        try:
            await _run_import(
                job_id,
                entity_code,
                actor,
                storage_key,
                storage,
                source_format=source_format,
                mapping=mapping,
            )
        finally:
            await _delete_temp_file(storage, storage_key)


async def _run_import(
    job_id: UUID,
    entity_code: str,
    actor: ActorContext,
    storage_key: str,
    storage: Minio,
    *,
    source_format: str,
    mapping: dict[str, Any],
) -> None:
    await _mark_job_running(job_id)
    try:
        if not await _wait_until_allowed(job_id):
            return
        content = await _read_temp_file(storage, storage_key)
        async with session_factory() as session:
            await AuthorizationService(session).require_entity_code(
                actor,
                "import" if source_format == "xlsx" else "create",
                entity_code,
            )
            await session.rollback()
            if source_format == "xlsx":
                excel_mapping = ExcelMapping.model_validate(mapping)
                items = await ExcelService(session).to_change_set_items(
                    entity_code, content, excel_mapping
                )
            else:
                service = ObjectImportService(session, storage)
                payloads = await service.parse_json_objects(content)
        if source_format == "xlsx":
            if not await _wait_until_allowed(job_id):
                return
            await _run_excel_change_set(job_id, entity_code, actor, items)
            return
        await _set_total_rows(job_id, len(payloads))

        start = 0
        while start < len(payloads):
            should_continue = await _wait_until_allowed(job_id)
            if not should_continue:
                return
            batch = payloads[start : start + settings.import_batch_size]
            await _import_batch(job_id, entity_code, actor, start + 1, batch)
            start += len(batch)
        await _finish_job(job_id, "completed")
    except Exception:
        log.exception("Ошибка фонового импорта %s", job_id)
        await _finish_job(job_id, "failed")


async def _run_excel_change_set(
    job_id: UUID,
    entity_code: str,
    actor: ActorContext,
    items,
) -> None:
    await _set_total_rows(job_id, len(items))
    if not await _wait_until_allowed(job_id):
        return
    async with session_factory() as session:
        authorization = AuthorizationService(session)
        checked: set[tuple[str, UUID | None]] = set()
        for item in items:
            action = {
                "create": "create",
                "update": "update",
                "archive": "archive",
                "confirm": "confirm",
            }[item.operation]
            key = (action, item.object_id)
            if key in checked:
                continue
            await authorization.require_entity_code(
                actor,
                action,
                entity_code,
                object_id=item.object_id,
            )
            checked.add(key)
        await session.rollback()
        service = ChangeSetService(session)
        change_set = await service.create(
            ChangeSetCreate(
                entity_code=entity_code,
                source="excel",
                idempotency_key=f"excel-import:{job_id}",
                items=items,
                metadata={"importJobId": str(job_id)},
            ),
            actor,
        )
        await session.rollback()
        if change_set.status == "validated":
            if not await _wait_until_allowed(job_id):
                await service.decide(change_set.id, "cancelled", actor)
                return
            change_set = await service.apply(change_set.id, actor)
            await session.rollback()
        recorded_rows = set(
            await session.scalars(
                select(ImportRowModel.row_number).where(
                    ImportRowModel.import_job_id == job_id
                )
            )
        )
        await session.rollback()
        rows = []
        error_rows = 0
        for index, item in enumerate(change_set.items, start=1):
            if index in recorded_rows:
                continue
            errors = [error.model_dump(mode="json", by_alias=True) for error in item.validation_errors]
            successful = item.status == "applied"
            if not successful:
                error_rows += 1
            rows.append(
                ImportRowModel(
                    import_job_id=job_id,
                    object_id=item.result_object_id or item.object_id,
                    row_number=index,
                    status="imported" if successful else "failed",
                    raw_values={"operation": item.operation, "values": item.proposed_values},
                    normalized_values=item.proposed_values,
                    errors=errors,
                )
            )
        await _append_rows_and_progress(
            session,
            job_id,
            rows,
            processed=len(rows),
            error_rows=error_rows,
        )
    await _finish_job(job_id, "completed" if error_rows == 0 else "failed")


async def _import_batch(
    job_id: UUID,
    entity_code: str,
    actor: ActorContext,
    first_row_number: int,
    batch: Sequence[Any],
) -> None:
    numbered = [
        (
            first_row_number + index,
            payload,
            _import_object_id(job_id, first_row_number + index),
        )
        for index, payload in enumerate(batch)
    ]
    async with session_factory() as session:
        completed_rows = set(
            await session.scalars(
                select(ImportRowModel.row_number).where(
                    ImportRowModel.import_job_id == job_id,
                    ImportRowModel.row_number.in_([item[0] for item in numbered]),
                )
            )
        )
        pending = [item for item in numbered if item[0] not in completed_rows]
        if not pending:
            return
        existing_objects = {
            item.id: {
                "data_quality": item.data_quality,
                "values": dict(item.values),
                "validation_errors": list(item.validation_errors),
            }
            for item in await session.scalars(
                select(EntityObjectModel).where(
                    EntityObjectModel.id.in_([item[2] for item in pending])
                )
            )
        }
        to_create = [item for item in pending if item[2] not in existing_objects]
        await session.rollback()
        service = RuntimeObjectService(session)
        try:
            response = await service.create_many(
                entity_code,
                [item[1] for item in to_create],
                actor.id,
                object_ids=[item[2] for item in to_create],
                actor_roles=actor.roles,
            ) if to_create else None
        except RuntimeValidationError:
            await _import_batch_one_by_one(
                job_id,
                entity_code,
                actor,
                first_row_number,
                batch,
            )
            return

        created = {item.id: item for item in response.data} if response is not None else {}
        rows: list[ImportRowModel] = []
        error_rows = 0
        for row_number, payload, object_id in pending:
            item = created.get(object_id)
            persisted = existing_objects.get(object_id)
            if item is None and persisted is None:
                continue
            data_quality = item.data_quality if item is not None else persisted["data_quality"]
            values = item.values if item is not None else persisted["values"]
            validation_errors = (
                item.validation_errors if item is not None else persisted["validation_errors"]
            )
            status = "imported" if data_quality == "complete" else "incomplete"
            if status == "incomplete":
                error_rows += 1
            rows.append(
                ImportRowModel(
                    import_job_id=job_id,
                    object_id=object_id,
                    row_number=row_number,
                    status=status,
                    raw_values=payload.model_dump(mode="json", by_alias=True),
                    normalized_values=values,
                    errors=[
                        issue.model_dump(mode="json", by_alias=True)
                        if hasattr(issue, "model_dump")
                        else issue
                        for issue in validation_errors
                    ],
                )
            )
        await _append_rows_and_progress(session, job_id, rows, processed=len(rows), error_rows=error_rows)


async def _import_batch_one_by_one(
    job_id: UUID,
    entity_code: str,
    actor: ActorContext,
    first_row_number: int,
    batch: Sequence[Any],
) -> None:
    for index, payload in enumerate(batch):
        row_number = first_row_number + index
        object_id = _import_object_id(job_id, row_number)
        async with session_factory() as session:
            recorded = await session.scalar(
                select(ImportRowModel.id).where(
                    ImportRowModel.import_job_id == job_id,
                    ImportRowModel.row_number == row_number,
                )
            )
            if recorded is not None:
                continue
            existing_model = await session.get(EntityObjectModel, object_id)
            existing = (
                {
                    "data_quality": existing_model.data_quality,
                    "values": dict(existing_model.values),
                    "validation_errors": list(existing_model.validation_errors),
                }
                if existing_model is not None
                else None
            )
            await session.rollback()
            service = RuntimeObjectService(session)
            try:
                response = (
                    await service.create_many(
                        entity_code,
                        [payload],
                        actor.id,
                        object_ids=[object_id],
                        actor_roles=actor.roles,
                    )
                    if existing is None
                    else None
                )
            except RuntimeValidationError as error:
                row = ImportRowModel(
                    import_job_id=job_id,
                    object_id=None,
                    row_number=row_number,
                    status="failed",
                    raw_values=payload.model_dump(mode="json", by_alias=True),
                    normalized_values={},
                    errors=error.issues,
                )
                await _append_rows_and_progress(session, job_id, [row], processed=1, error_rows=1)
                continue

            item = response.data[0] if response is not None else None
            data_quality = item.data_quality if item is not None else existing["data_quality"]
            values = item.values if item is not None else existing["values"]
            validation_errors = (
                item.validation_errors if item is not None else existing["validation_errors"]
            )
            status = "imported" if data_quality == "complete" else "incomplete"
            row = ImportRowModel(
                import_job_id=job_id,
                object_id=object_id,
                row_number=row_number,
                status=status,
                raw_values=payload.model_dump(mode="json", by_alias=True),
                normalized_values=values,
                errors=[
                    issue.model_dump(mode="json", by_alias=True)
                    if hasattr(issue, "model_dump")
                    else issue
                    for issue in validation_errors
                ],
            )
            await _append_rows_and_progress(
                session,
                job_id,
                [row],
                processed=1,
                error_rows=1 if status == "incomplete" else 0,
            )


def _import_object_id(job_id: UUID, row_number: int) -> UUID:
    """Получить стабильный UUID строки для идемпотентной повторной доставки."""

    return uuid5(NAMESPACE_URL, f"municipal-import:{job_id}:{row_number}")


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
                if job.created_by is not None:
                    notification_exists = await session.scalar(
                        select(NotificationModel.id).where(
                            NotificationModel.user_id == job.created_by,
                            NotificationModel.type == f"import.{status}",
                            NotificationModel.resource_type == "import_job",
                            NotificationModel.resource_id == job.id,
                        )
                    )
                    if notification_exists is None:
                        session.add(NotificationModel(
                            user_id=job.created_by,
                            type=f"import.{status}",
                            title="Импорт завершён" if status == "completed" else "Ошибка импорта",
                            message=(
                                f"Файл «{job.source_name}» успешно обработан"
                                if status == "completed"
                                else f"Не удалось обработать файл «{job.source_name}»"
                            ),
                            resource_type="import_job",
                            resource_id=job.id,
                            metadata_json={
                                "processedRows": job.processed_rows,
                                "errorRows": job.error_rows,
                            },
                        ))


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
