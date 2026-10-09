from __future__ import annotations

import hashlib
from io import BytesIO
from types import EllipsisType
from typing import Any
from uuid import UUID, uuid4

from fastapi import UploadFile
from minio import Minio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.storage import ensure_bucket_exists
from app.modules.attachments.api.schemas import (
    AttachmentDeleteRead,
    AttachmentKind,
    AttachmentPage,
    AttachmentRead,
    AttachmentVersionRead,
)
from app.modules.attachments.domain.errors import AttachmentNotFound, AttachmentValidationError
from app.modules.entities.infrastructure.models import EntitySchemaModel
from app.modules.objects.application.service import RuntimeEntityNotFound, RuntimeObjectNotFound
from app.shared.db.models import AttachmentModel, AttachmentVersionModel, EntityObjectModel

PHOTO_MIME_PREFIX = "image/"
PHOTO_EXTENSIONS = {".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".svg", ".tif", ".tiff", ".webp"}
DOCUMENT_MIME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx"}


class AttachmentService:
    """Сервис хранения файлов объектов в MinIO и метаданных в PostgreSQL."""

    def __init__(self, session: AsyncSession, storage: Minio) -> None:
        self._session = session
        self._storage = storage

    async def list_page(
        self,
        *,
        entity_code: str,
        object_id: UUID,
        kind: AttachmentKind | None,
        field_id: UUID | None,
        limit: int,
        offset: int,
    ) -> AttachmentPage:
        schema, model = await self._object_context(entity_code, object_id)
        conditions: list[Any] = [AttachmentModel.object_id == model.id]
        if kind is not None:
            conditions.append(AttachmentModel.kind == kind)
        if field_id is not None:
            conditions.append(AttachmentModel.entity_field_id == field_id)
        total = int(
            await self._session.scalar(
                select(func.count()).select_from(AttachmentModel).where(*conditions)
            )
            or 0
        )
        attachments = (
            await self._session.scalars(
                select(AttachmentModel)
                .where(*conditions)
                .order_by(AttachmentModel.created_at.desc(), AttachmentModel.original_name)
                .offset(offset)
                .limit(limit)
            )
        ).all()
        return AttachmentPage(
            items=[self._to_response(schema, item) for item in attachments],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def upload(
        self,
        *,
        entity_code: str,
        object_id: UUID,
        kind: AttachmentKind,
        field_id: UUID | None,
        file: UploadFile,
        actor_id: UUID | None,
    ) -> AttachmentRead:
        schema, model = await self._object_context(entity_code, object_id, for_update=True)
        payload = await self._file_payload(file)
        await self._validate_file_field(schema, field_id)
        self._validate_file(kind, payload["original_name"], payload["mime_type"], payload["size"])
        attachment_id = uuid4()
        object_key = f"{model.id}/{attachment_id}/v1"
        await self._put_object(object_key, payload["content"], payload["mime_type"])
        attachment = AttachmentModel(
            id=attachment_id,
            entity_schema_id=schema.id,
            object_id=model.id,
            entity_field_id=field_id,
            kind=kind,
            original_name=payload["original_name"],
            object_key=object_key,
            mime_type=payload["mime_type"],
            size_bytes=payload["size"],
            checksum_sha256=payload["checksum"],
            uploaded_by=actor_id,
            current_version=1,
            scan_status="clean",
        )
        self._session.add(attachment)
        self._session.add(
            AttachmentVersionModel(
                attachment_id=attachment_id,
                version=1,
                object_key=object_key,
                original_name=payload["original_name"],
                mime_type=payload["mime_type"],
                size_bytes=payload["size"],
                checksum_sha256=payload["checksum"],
                uploaded_by=actor_id,
            )
        )
        model.attachment_paths = _add_path(model.attachment_paths, object_key)
        await self._session.commit()
        await self._session.refresh(attachment)
        return self._to_response(schema, attachment)

    async def get(self, *, entity_code: str, object_id: UUID, attachment_id: UUID) -> AttachmentRead:
        schema, _, attachment = await self._attachment_context(entity_code, object_id, attachment_id)
        return self._to_response(schema, attachment)

    async def download(self, *, entity_code: str, object_id: UUID, attachment_id: UUID) -> tuple[AttachmentRead, Any]:
        schema, _, attachment = await self._attachment_context(entity_code, object_id, attachment_id)
        response = await run_in_threadpool(
            self._storage.get_object,
            settings.minio_bucket,
            attachment.object_key,
        )
        return self._to_response(schema, attachment), response

    async def update_metadata(
        self,
        *,
        entity_code: str,
        object_id: UUID,
        attachment_id: UUID,
        original_name: str | None,
        kind: AttachmentKind | None,
        field_id: UUID | None | EllipsisType,
    ) -> AttachmentRead:
        schema, _, attachment = await self._attachment_context(entity_code, object_id, attachment_id, for_update=True)
        if original_name is not None:
            attachment.original_name = original_name
        if kind is not None:
            self._validate_file(kind, attachment.original_name, attachment.mime_type, attachment.size_bytes)
            attachment.kind = kind
        if field_id is not ...:
            await self._validate_file_field(schema, field_id)
            attachment.entity_field_id = field_id
        await self._session.commit()
        await self._session.refresh(attachment)
        return self._to_response(schema, attachment)

    async def replace_file(
        self,
        *,
        entity_code: str,
        object_id: UUID,
        attachment_id: UUID,
        file: UploadFile,
        actor_id: UUID | None,
    ) -> AttachmentRead:
        schema, _, attachment = await self._attachment_context(entity_code, object_id, attachment_id, for_update=True)
        payload = await self._file_payload(file)
        self._validate_file(attachment.kind, payload["original_name"], payload["mime_type"], payload["size"])
        previous_key = attachment.object_key
        next_version = attachment.current_version + 1
        object_key = f"{object_id}/{attachment.id}/v{next_version}"
        await self._put_object(object_key, payload["content"], payload["mime_type"])
        attachment.object_key = object_key
        attachment.original_name = payload["original_name"]
        attachment.mime_type = payload["mime_type"]
        attachment.size_bytes = payload["size"]
        attachment.checksum_sha256 = payload["checksum"]
        attachment.uploaded_by = actor_id
        attachment.current_version = next_version
        attachment.scan_status = "clean"
        self._session.add(
            AttachmentVersionModel(
                attachment_id=attachment.id,
                version=next_version,
                object_key=object_key,
                original_name=payload["original_name"],
                mime_type=payload["mime_type"],
                size_bytes=payload["size"],
                checksum_sha256=payload["checksum"],
                uploaded_by=actor_id,
            )
        )
        model = await self._session.get(EntityObjectModel, object_id, with_for_update=True)
        if model is not None:
            model.attachment_paths = _replace_path(model.attachment_paths, previous_key, object_key)
        await self._session.commit()
        await self._session.refresh(attachment)
        return self._to_response(schema, attachment)

    async def delete(
        self,
        *,
        entity_code: str,
        object_id: UUID,
        attachment_id: UUID,
    ) -> AttachmentDeleteRead:
        _, model, attachment = await self._attachment_context(entity_code, object_id, attachment_id, for_update=True)
        version_keys = list(
            await self._session.scalars(
                select(AttachmentVersionModel.object_key).where(
                    AttachmentVersionModel.attachment_id == attachment.id
                )
            )
        )
        for object_key in version_keys:
            await self._remove_object(object_key)
        await self._session.delete(attachment)
        model.attachment_paths = _remove_path(model.attachment_paths, attachment.object_key)
        await self._session.commit()
        return AttachmentDeleteRead(id=attachment_id, deleted=True)

    async def list_versions(
        self, *, entity_code: str, object_id: UUID, attachment_id: UUID
    ) -> list[AttachmentVersionRead]:
        await self._attachment_context(entity_code, object_id, attachment_id)
        rows = (
            await self._session.scalars(
                select(AttachmentVersionModel)
                .where(AttachmentVersionModel.attachment_id == attachment_id)
                .order_by(AttachmentVersionModel.version.desc())
            )
        ).all()
        return [AttachmentVersionRead.model_validate(row, from_attributes=True) for row in rows]

    async def download_version(
        self,
        *,
        entity_code: str,
        object_id: UUID,
        attachment_id: UUID,
        version: int,
    ) -> tuple[AttachmentVersionRead, Any]:
        await self._attachment_context(entity_code, object_id, attachment_id)
        model = await self._session.scalar(
            select(AttachmentVersionModel).where(
                AttachmentVersionModel.attachment_id == attachment_id,
                AttachmentVersionModel.version == version,
            )
        )
        if model is None:
            raise AttachmentNotFound
        response = await run_in_threadpool(
            self._storage.get_object, settings.minio_bucket, model.object_key
        )
        return AttachmentVersionRead.model_validate(model, from_attributes=True), response

    async def _object_context(
        self,
        entity_code: str,
        object_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[EntitySchemaModel, EntityObjectModel]:
        schema = await self._session.scalar(
            select(EntitySchemaModel).where(EntitySchemaModel.code == entity_code, EntitySchemaModel.status == "active")
        )
        if schema is None:
            raise RuntimeEntityNotFound
        statement = select(EntityObjectModel).where(
            EntityObjectModel.entity_schema_id == schema.id,
            EntityObjectModel.id == object_id,
        )
        if for_update:
            statement = statement.with_for_update()
        model = await self._session.scalar(statement)
        if model is None:
            raise RuntimeObjectNotFound
        return schema, model

    @staticmethod
    async def _validate_file_field(
        schema: EntitySchemaModel,
        field_id: UUID | None,
    ) -> None:
        if field_id is None:
            return
        field = next((item for item in schema.fields if item.id == field_id), None)
        if field is None or field.archived or field.field_type != "file":
            raise AttachmentValidationError("Указанное поле не является активным file-полем сущности")

    async def _attachment_context(
        self,
        entity_code: str,
        object_id: UUID,
        attachment_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[EntitySchemaModel, EntityObjectModel, AttachmentModel]:
        schema, model = await self._object_context(entity_code, object_id, for_update=for_update)
        statement = select(AttachmentModel).where(
            AttachmentModel.id == attachment_id,
            AttachmentModel.object_id == object_id,
        )
        if for_update:
            statement = statement.with_for_update()
        attachment = await self._session.scalar(statement)
        if attachment is None:
            raise AttachmentNotFound
        return schema, model, attachment

    async def _file_payload(self, file: UploadFile) -> dict[str, Any]:
        original_name = (file.filename or "file").strip()
        if not original_name:
            raise AttachmentValidationError("Имя файла не может состоять только из пробелов")
        content = await file.read()
        size = len(content)
        if size == 0:
            raise AttachmentValidationError("Нельзя загрузить пустой файл")
        if size > settings.max_attachment_size_bytes:
            raise AttachmentValidationError("Файл превышает допустимый размер")
        mime_type = file.content_type or "application/octet-stream"
        checksum = hashlib.sha256(content).hexdigest()
        return {
            "original_name": original_name,
            "content": content,
            "mime_type": mime_type,
            "size": size,
            "checksum": checksum,
        }

    async def _put_object(self, object_key: str, content: bytes, mime_type: str) -> None:
        await run_in_threadpool(ensure_bucket_exists, self._storage, settings.minio_bucket)
        await run_in_threadpool(
            self._storage.put_object,
            settings.minio_bucket,
            object_key,
            BytesIO(content),
            len(content),
            content_type=mime_type,
        )

    async def _remove_object(self, object_key: str) -> None:
        await run_in_threadpool(ensure_bucket_exists, self._storage, settings.minio_bucket)
        await run_in_threadpool(self._storage.remove_object, settings.minio_bucket, object_key)

    @staticmethod
    def _validate_file(kind: AttachmentKind, original_name: str, mime_type: str, size: int) -> None:
        extension = _extension(original_name)
        if size < 0:
            raise AttachmentValidationError("Размер файла не может быть отрицательным")
        if kind == "photo":
            if not mime_type.startswith(PHOTO_MIME_PREFIX) and extension not in PHOTO_EXTENSIONS:
                raise AttachmentValidationError("Для фото можно загрузить только изображение")
            return
        if mime_type in DOCUMENT_MIME_TYPES or extension in DOCUMENT_EXTENSIONS:
            return
        raise AttachmentValidationError("Для документа можно загрузить только PDF, DOC или DOCX")

    @staticmethod
    def _to_response(schema: EntitySchemaModel, attachment: AttachmentModel) -> AttachmentRead:
        return AttachmentRead(
            id=attachment.id,
            entity_id=schema.id,
            entity_code=schema.code,
            object_id=attachment.object_id,
            field_id=attachment.entity_field_id,
            kind=attachment.kind,
            original_name=attachment.original_name,
            storage_key=attachment.object_key,
            mime_type=attachment.mime_type,
            size_bytes=attachment.size_bytes,
            checksum_sha256=attachment.checksum_sha256,
            uploaded_by=attachment.uploaded_by,
            created_at=attachment.created_at,
            updated_at=attachment.updated_at,
            current_version=attachment.current_version,
            scan_status=attachment.scan_status,
        )


def _extension(filename: str) -> str:
    _, dot, suffix = filename.strip().lower().rpartition(".")
    return f"{dot}{suffix}" if dot else ""


async def delete_object_storage_files(session: AsyncSession, storage: Minio, object_id: UUID) -> None:
    """Удалить из MinIO все файлы объекта перед физическим удалением объекта."""

    current_keys = list(
        await session.scalars(
            select(AttachmentModel.object_key).where(AttachmentModel.object_id == object_id)
        )
    )
    version_keys = list(
        await session.scalars(
            select(AttachmentVersionModel.object_key)
            .join(AttachmentModel, AttachmentModel.id == AttachmentVersionModel.attachment_id)
            .where(AttachmentModel.object_id == object_id)
        )
    )
    object_keys = list(dict.fromkeys([*current_keys, *version_keys]))
    if not object_keys:
        return
    await run_in_threadpool(ensure_bucket_exists, storage, settings.minio_bucket)
    for object_key in object_keys:
        await run_in_threadpool(storage.remove_object, settings.minio_bucket, object_key)


def _add_path(paths: list[str] | None, path: str) -> list[str]:
    current = list(paths or [])
    if path not in current:
        current.append(path)
    return current


def _remove_path(paths: list[str] | None, path: str) -> list[str]:
    return [item for item in paths or [] if item != path]


def _replace_path(paths: list[str] | None, old_path: str, new_path: str) -> list[str]:
    return [new_path if item == old_path else item for item in paths or []]
