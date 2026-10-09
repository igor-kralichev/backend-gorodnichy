from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from minio import Minio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.rabbitmq import publish_json
from app.core.security import ActorContext
from app.core.storage import ensure_bucket_exists
from app.modules.entities.infrastructure.models import EntitySchemaModel
from app.modules.excel.api.schemas import (
    ExcelExportJobPage,
    ExcelExportJobRead,
    ExcelExportRequest,
)
from app.modules.objects.application.service import RuntimeEntityNotFound
from app.shared.db.models import ExcelExportJobModel


class ExcelExportJobNotFound(Exception):
    """Задача не найдена или не принадлежит текущему пользователю."""


class ExcelExportNotReady(Exception):
    """Файл ещё не сформирован или уже истёк."""


class ExcelExportQueueUnavailable(Exception):
    """Задачу не удалось опубликовать в RabbitMQ."""


class ExcelExportJobService:
    """Ставит XLSX-экспорт в RabbitMQ и защищает результат в MinIO."""

    def __init__(self, session: AsyncSession, storage: Minio) -> None:
        self._session = session
        self._storage = storage

    async def enqueue(
        self,
        *,
        entity_code: str,
        request: ExcelExportRequest,
        actor: ActorContext,
    ) -> ExcelExportJobRead:
        schema = await self._active_schema(entity_code)
        job = ExcelExportJobModel(
            id=uuid4(),
            entity_schema_id=schema.id,
            status="queued",
            request_payload=request.model_dump(mode="json", by_alias=True),
            created_by=actor.id,
            expires_at=datetime.now(UTC) + timedelta(hours=settings.excel_export_ttl_hours),
        )
        self._session.add(job)
        await self._session.commit()
        try:
            await publish_json(
                settings.excel_export_queue_name,
                {
                    "jobId": str(job.id),
                    "entityCode": schema.code,
                    "actorId": str(actor.id),
                    "actorRoles": sorted(actor.roles),
                    "actorUsername": actor.username,
                    "actorDisplayName": actor.display_name,
                    "actorEmail": actor.email,
                },
            )
        except Exception as error:
            job.status = "failed"
            job.error_message = "Не удалось поставить экспорт в очередь"
            await self._session.commit()
            raise ExcelExportQueueUnavailable(job.error_message) from error
        await self._session.refresh(job)
        return self._response(job, schema.code)

    async def get(self, job_id: UUID, actor_id: UUID) -> ExcelExportJobRead:
        job, entity_code = await self._owned_job(job_id, actor_id)
        return self._response(job, entity_code)

    async def list_page(
        self,
        *,
        actor_id: UUID,
        status: str | None,
        limit: int,
        offset: int,
    ) -> ExcelExportJobPage:
        conditions = [ExcelExportJobModel.created_by == actor_id]
        if status is not None:
            conditions.append(ExcelExportJobModel.status == status)
        total = int(
            await self._session.scalar(
                select(func.count()).select_from(ExcelExportJobModel).where(*conditions)
            )
            or 0
        )
        rows = (
            await self._session.execute(
                select(ExcelExportJobModel, EntitySchemaModel.code)
                .join(
                    EntitySchemaModel,
                    EntitySchemaModel.id == ExcelExportJobModel.entity_schema_id,
                )
                .where(*conditions)
                .order_by(ExcelExportJobModel.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
        return ExcelExportJobPage(
            items=[self._response(job, code) for job, code in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def cancel(self, job_id: UUID, actor_id: UUID) -> ExcelExportJobRead:
        job, entity_code = await self._owned_job(job_id, actor_id, for_update=True)
        if job.status in {"queued", "running"}:
            job.status = "cancelled"
            await self._session.commit()
            await self._session.refresh(job)
        return self._response(job, entity_code)

    async def download(
        self,
        job_id: UUID,
        actor_id: UUID,
    ) -> tuple[str, bytes]:
        job, _entity_code = await self._owned_job(job_id, actor_id)
        if (
            job.status != "completed"
            or not job.result_object_key
            or not job.result_filename
        ):
            raise ExcelExportNotReady("Файл экспорта ещё не готов")
        if job.expires_at <= datetime.now(UTC):
            raise ExcelExportNotReady("Срок хранения файла экспорта истёк")
        response = await run_in_threadpool(
            self._storage.get_object,
            settings.minio_bucket,
            job.result_object_key,
        )
        try:
            content = await run_in_threadpool(response.read)
        finally:
            response.close()
            response.release_conn()
        return job.result_filename, content

    async def entity_codes(self, job_id: UUID, actor_id: UUID) -> list[str]:
        """Вернуть реестры, права на которые нужно повторно проверить перед download."""

        job, entity_code = await self._owned_job(job_id, actor_id)
        request = ExcelExportRequest.model_validate(job.request_payload)
        return list(
            dict.fromkeys(
                [entity_code, *(child.entity_code for child in request.children)]
            )
        )

    async def _owned_job(
        self,
        job_id: UUID,
        actor_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[ExcelExportJobModel, str]:
        statement = (
            select(ExcelExportJobModel, EntitySchemaModel.code)
            .join(
                EntitySchemaModel,
                EntitySchemaModel.id == ExcelExportJobModel.entity_schema_id,
            )
            .where(
                ExcelExportJobModel.id == job_id,
                ExcelExportJobModel.created_by == actor_id,
            )
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            raise ExcelExportJobNotFound
        return row[0], row[1]

    async def _active_schema(self, entity_code: str) -> EntitySchemaModel:
        schema = await self._session.scalar(
            select(EntitySchemaModel).where(
                EntitySchemaModel.code == entity_code,
                EntitySchemaModel.status == "active",
            )
        )
        if schema is None:
            raise RuntimeEntityNotFound
        return schema

    @staticmethod
    def _response(job: ExcelExportJobModel, entity_code: str) -> ExcelExportJobRead:
        download_url = (
            f"/api/v1/excel/exportJobs/{job.id}/download"
            if job.status == "completed" and job.result_object_key
            else None
        )
        return ExcelExportJobRead(
            id=job.id,
            entity_id=job.entity_schema_id,
            entity_code=entity_code,
            status=job.status,
            total_rows=job.total_rows,
            filename=job.result_filename,
            error=job.error_message,
            download_url=download_url,
            expires_at=job.expires_at,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )


async def upload_export_result(
    storage: Minio,
    *,
    job_id: UUID,
    actor_id: UUID,
    filename: str,
    content: bytes,
) -> str:
    """Сохранить готовую книгу в общем бакете в папке задачи."""

    import io

    await run_in_threadpool(ensure_bucket_exists, storage, settings.minio_bucket)
    object_key = f"exports/{actor_id}/{job_id}/{filename}"
    await run_in_threadpool(
        storage.put_object,
        settings.minio_bucket,
        object_key,
        io.BytesIO(content),
        len(content),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    return object_key
