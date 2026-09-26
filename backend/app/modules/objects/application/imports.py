from __future__ import annotations

import io
from uuid import UUID, uuid4

import orjson
from minio import Minio
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.rabbitmq import publish_json
from app.core.storage import ensure_bucket_exists
from app.modules.entities.infrastructure.models import EntitySchemaModel
from app.modules.objects.api.schemas import EntityObjectCreate, ObjectImportJobRead
from app.modules.objects.application.service import RuntimeEntityNotFound, RuntimeValidationError
from app.shared.db.models import ImportJobModel

_OBJECTS_ADAPTER = TypeAdapter(list[EntityObjectCreate])


class ObjectImportService:
    """Сервис постановки JSON-импорта объектов в фоновую очередь."""

    def __init__(self, session: AsyncSession, storage: Minio) -> None:
        self._session = session
        self._storage = storage

    async def parse_json_objects(self, content: bytes) -> list[EntityObjectCreate]:
        """Прочитать JSON-файл и проверить, что в нём массив объектов."""

        try:
            raw_payload = orjson.loads(content)
        except orjson.JSONDecodeError as error:
            raise RuntimeValidationError(
                [
                    {
                        "fieldCode": None,
                        "code": "invalid_json",
                        "message": "Файл должен содержать корректный JSON",
                    }
                ]
            ) from error
        if not isinstance(raw_payload, list):
            raise RuntimeValidationError(
                [
                    {
                        "fieldCode": None,
                        "code": "json_array_required",
                        "message": "Файл должен содержать JSON-массив объектов",
                    }
                ]
            )
        try:
            return _OBJECTS_ADAPTER.validate_python(raw_payload)
        except ValidationError as error:
            raise RuntimeValidationError(
                [
                    {
                        "fieldCode": None,
                        "code": "invalid_object_payload",
                        "message": "Структура объектов в JSON-файле не соответствует контракту",
                    }
                ]
            ) from error

    async def enqueue(
        self,
        *,
        entity_code: str,
        source_name: str,
        content: bytes,
        total_rows: int,
        actor_id: UUID,
    ) -> ObjectImportJobRead:
        """Сохранить временный JSON в MinIO и поставить импорт в RabbitMQ."""

        schema = await self._get_schema(entity_code)
        job_id = uuid4()
        object_key = f"{settings.import_temp_prefix.strip('/')}/{job_id}.json"
        uploaded = False
        try:
            await run_in_threadpool(ensure_bucket_exists, self._storage, settings.minio_bucket)
            await run_in_threadpool(
                self._storage.put_object,
                settings.minio_bucket,
                object_key,
                io.BytesIO(content),
                len(content),
                "application/json",
            )
            uploaded = True

            async with self._session.begin():
                job = ImportJobModel(
                    id=job_id,
                    entity_schema_id=schema.id,
                    status="queued",
                    source_name=source_name,
                    source_object_key=object_key,
                    mapping={},
                    total_rows=total_rows,
                    processed_rows=0,
                    error_rows=0,
                    created_by=actor_id,
                )
                self._session.add(job)

            await publish_json(
                settings.import_queue_name,
                {
                    "jobId": str(job_id),
                    "entityCode": entity_code,
                    "sourceObjectKey": object_key,
                    "actorId": str(actor_id),
                },
            )
        except Exception:
            if uploaded:
                await run_in_threadpool(self._storage.remove_object, settings.minio_bucket, object_key)
            async with self._session.begin():
                job = await self._session.get(ImportJobModel, job_id, with_for_update=True)
                if job is not None:
                    job.status = "failed"
                    job.source_object_key = None
            raise
        await self._session.refresh(job)
        return self.to_read_model(job)

    async def get(self, job_id: UUID) -> ObjectImportJobRead:
        job = await self._session.get(ImportJobModel, job_id)
        if job is None:
            raise RuntimeEntityNotFound
        return self.to_read_model(job)

    async def command(self, job_id: UUID, command: str) -> ObjectImportJobRead:
        async with self._session.begin():
            job = await self._session.get(ImportJobModel, job_id, with_for_update=True)
            if job is None:
                raise RuntimeEntityNotFound
            if command == "pause" and job.status in {"queued", "running"}:
                job.status = "paused"
            elif command == "resume" and job.status == "paused":
                job.status = "running"
            elif command == "cancel" and job.status in {"queued", "running", "paused"}:
                job.status = "cancelled"
        await self._session.refresh(job)
        return self.to_read_model(job)

    async def _get_schema(self, entity_code: str) -> EntitySchemaModel:
        schema = await self._session.scalar(
            select(EntitySchemaModel).where(EntitySchemaModel.code == entity_code, EntitySchemaModel.status == "active")
        )
        if schema is None:
            raise RuntimeEntityNotFound
        return schema

    @staticmethod
    def to_read_model(job: ImportJobModel) -> ObjectImportJobRead:
        return ObjectImportJobRead(
            id=job.id,
            entity_id=job.entity_schema_id,
            status=job.status,
            source_name=job.source_name,
            total_rows=job.total_rows,
            processed_rows=job.processed_rows,
            error_rows=job.error_rows,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )
