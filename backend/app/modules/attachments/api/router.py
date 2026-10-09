from collections.abc import Awaitable, Iterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Path, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.security import CurrentActor
from app.core.storage import get_minio_client
from app.modules.access.api.schemas import PermissionAction
from app.modules.access.application.service import (
    AccessDenied,
    AccessResourceNotFound,
    AuthorizationService,
)
from app.modules.attachments.api.schemas import (
    AttachmentDeleteRead,
    AttachmentKind,
    AttachmentPage,
    AttachmentRead,
    AttachmentUpdate,
    AttachmentVersionRead,
)
from app.modules.attachments.application.service import AttachmentService
from app.modules.attachments.domain.errors import AttachmentNotFound, AttachmentValidationError
from app.modules.audit.application.service import AuditService
from app.modules.objects.application.service import RuntimeEntityNotFound, RuntimeObjectNotFound

router = APIRouter(prefix="/entities", tags=["Файлы объектов"])

EntityCode = Annotated[str, Path(alias="entityCode", description="Уникальный код опубликованной сущности")]
ObjectId = Annotated[UUID, Path(alias="objectId", description="Идентификатор объекта")]
AttachmentId = Annotated[UUID, Path(alias="attachmentId", description="Идентификатор файла")]


@router.get(
    "/{entityCode}/objects/{objectId}/attachments",
    response_model=AttachmentPage,
    response_model_by_alias=True,
    summary="Получить файлы объекта",
)
async def list_object_attachments(
    entity_code: EntityCode,
    object_id: ObjectId,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    kind: AttachmentKind | None = Query(default=None, description="Фильтр по типу файла"),
    field_id: Annotated[UUID | None, Query(alias="fieldId", description="Фильтр по file-полю")] = None,
    limit: int = Query(default=50, ge=1, le=500, description="Количество файлов на странице"),
    offset: int = Query(default=0, ge=0, description="Смещение от начала списка"),
) -> AttachmentPage:
    await _authorize(session, actor, "read", entity_code, object_id)
    return await _execute(
        _service(session).list_page(
            entity_code=entity_code,
            object_id=object_id,
            kind=kind,
            field_id=field_id,
            limit=limit,
            offset=offset,
        )
    )


@router.post(
    "/{entityCode}/objects/{objectId}/attachments",
    response_model=AttachmentRead,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    summary="Загрузить файл объекта",
    description="Сохраняет бинарный файл в MinIO, а в PostgreSQL — его метаданные и связь с объектом.",
)
async def upload_object_attachment(
    entity_code: EntityCode,
    object_id: ObjectId,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    kind: AttachmentKind = Query(description="Тип файла: photo или document"),
    field_id: Annotated[UUID | None, Query(alias="fieldId", description="UUID поля типа file")] = None,
    file: UploadFile = File(description="PDF, DOC, DOCX или изображение"),
) -> AttachmentRead:
    await _authorize(session, actor, "update", entity_code, object_id)
    response = await _execute(
        _service(session).upload(
            entity_code=entity_code,
            object_id=object_id,
            kind=kind,
            field_id=field_id,
            file=file,
            actor_id=actor.id,
        )
    )
    await AuditService(session).record(
        actor=actor,
        resource_type="attachment",
        resource_id=response.id,
        resource_code=response.storage_key,
        resource_name=response.original_name,
        action="attachment.created",
        old_value=None,
        new_value=response,
        metadata={"entityCode": entity_code, "objectId": str(object_id)},
    )
    return response


@router.get(
    "/{entityCode}/objects/{objectId}/attachments/{attachmentId}",
    response_model=AttachmentRead,
    response_model_by_alias=True,
    summary="Получить метаданные файла",
)
async def get_object_attachment(
    entity_code: EntityCode,
    object_id: ObjectId,
    attachment_id: AttachmentId,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AttachmentRead:
    await _authorize(session, actor, "read", entity_code, object_id)
    return await _execute(
        _service(session).get(
            entity_code=entity_code,
            object_id=object_id,
            attachment_id=attachment_id,
        )
    )


@router.get(
    "/{entityCode}/objects/{objectId}/attachments/{attachmentId}/download",
    summary="Скачать файл",
    description="Возвращает оригинальный файл из MinIO с пользовательским именем.",
)
async def download_object_attachment(
    entity_code: EntityCode,
    object_id: ObjectId,
    attachment_id: AttachmentId,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StreamingResponse:
    await _authorize(session, actor, "read", entity_code, object_id)
    metadata, storage_response = await _execute(
        _service(session).download(
            entity_code=entity_code,
            object_id=object_id,
            attachment_id=attachment_id,
        )
    )
    return StreamingResponse(
        _iter_storage_response(storage_response),
        media_type=metadata.mime_type,
        headers={
            "Content-Disposition": _content_disposition(metadata.original_name),
            "Content-Length": str(metadata.size_bytes),
        },
    )


@router.get(
    "/{entityCode}/objects/{objectId}/attachments/{attachmentId}/versions",
    response_model=list[AttachmentVersionRead],
    response_model_by_alias=True,
    summary="Получить версии файла",
)
async def list_object_attachment_versions(
    entity_code: EntityCode,
    object_id: ObjectId,
    attachment_id: AttachmentId,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[AttachmentVersionRead]:
    await _authorize(session, actor, "read", entity_code, object_id)
    return await _execute(
        _service(session).list_versions(
            entity_code=entity_code,
            object_id=object_id,
            attachment_id=attachment_id,
        )
    )


@router.get(
    "/{entityCode}/objects/{objectId}/attachments/{attachmentId}/versions/{version}/download",
    summary="Скачать выбранную версию файла",
)
async def download_object_attachment_version(
    entity_code: EntityCode,
    object_id: ObjectId,
    attachment_id: AttachmentId,
    version: Annotated[int, Path(ge=1)],
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StreamingResponse:
    await _authorize(session, actor, "read", entity_code, object_id)
    metadata, storage_response = await _execute(
        _service(session).download_version(
            entity_code=entity_code,
            object_id=object_id,
            attachment_id=attachment_id,
            version=version,
        )
    )
    return StreamingResponse(
        _iter_storage_response(storage_response),
        media_type=metadata.mime_type,
        headers={
            "Content-Disposition": _content_disposition(metadata.original_name),
            "Content-Length": str(metadata.size_bytes),
        },
    )


@router.patch(
    "/{entityCode}/objects/{objectId}/attachments/{attachmentId}",
    response_model=AttachmentRead,
    response_model_by_alias=True,
    summary="Изменить метаданные файла",
)
async def update_object_attachment(
    entity_code: EntityCode,
    object_id: ObjectId,
    attachment_id: AttachmentId,
    payload: AttachmentUpdate,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AttachmentRead:
    await _authorize(session, actor, "update", entity_code, object_id)
    service = _service(session)
    before = await _execute(service.get(entity_code=entity_code, object_id=object_id, attachment_id=attachment_id))
    await session.rollback()
    response = await _execute(
        service.update_metadata(
            entity_code=entity_code,
            object_id=object_id,
            attachment_id=attachment_id,
            original_name=payload.original_name,
            kind=payload.kind,
            field_id=payload.field_id if "field_id" in payload.model_fields_set else ...,
        )
    )
    await AuditService(session).record(
        actor=actor,
        resource_type="attachment",
        resource_id=response.id,
        resource_code=response.storage_key,
        resource_name=response.original_name,
        action="attachment.updated",
        old_value=before,
        new_value=response,
        metadata={"entityCode": entity_code, "objectId": str(object_id)},
    )
    return response


@router.put(
    "/{entityCode}/objects/{objectId}/attachments/{attachmentId}/file",
    response_model=AttachmentRead,
    response_model_by_alias=True,
    summary="Заменить файл",
    description="Создаёт новую неизменяемую версию содержимого, сохраняя UUID файла.",
)
async def replace_object_attachment_file(
    entity_code: EntityCode,
    object_id: ObjectId,
    attachment_id: AttachmentId,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    file: UploadFile = File(description="Новый файл того же типа: фото или документ"),
) -> AttachmentRead:
    await _authorize(session, actor, "update", entity_code, object_id)
    service = _service(session)
    before = await _execute(service.get(entity_code=entity_code, object_id=object_id, attachment_id=attachment_id))
    await session.rollback()
    response = await _execute(
        service.replace_file(
            entity_code=entity_code,
            object_id=object_id,
            attachment_id=attachment_id,
            file=file,
            actor_id=actor.id,
        )
    )
    await AuditService(session).record(
        actor=actor,
        resource_type="attachment",
        resource_id=response.id,
        resource_code=response.storage_key,
        resource_name=response.original_name,
        action="attachment.file_replaced",
        old_value=before,
        new_value=response,
        metadata={"entityCode": entity_code, "objectId": str(object_id)},
    )
    return response


@router.delete(
    "/{entityCode}/objects/{objectId}/attachments/{attachmentId}",
    response_model=AttachmentDeleteRead,
    response_model_by_alias=True,
    summary="Удалить файл",
    description="Удаляет объект из MinIO и метаданные из PostgreSQL.",
)
async def delete_object_attachment(
    entity_code: EntityCode,
    object_id: ObjectId,
    attachment_id: AttachmentId,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AttachmentDeleteRead:
    await _authorize(session, actor, "update", entity_code, object_id)
    service = _service(session)
    before = await _execute(service.get(entity_code=entity_code, object_id=object_id, attachment_id=attachment_id))
    await session.rollback()
    response = await _execute(
        service.delete(
            entity_code=entity_code,
            object_id=object_id,
            attachment_id=attachment_id,
        )
    )
    await AuditService(session).record(
        actor=actor,
        resource_type="attachment",
        resource_id=response.id,
        resource_code=before.storage_key,
        resource_name=before.original_name,
        action="attachment.deleted",
        old_value=before,
        new_value=None,
        metadata={"entityCode": entity_code, "objectId": str(object_id)},
    )
    return response


def _service(session: AsyncSession) -> AttachmentService:
    return AttachmentService(session, get_minio_client())


async def _execute[ResultT](awaitable: Awaitable[ResultT]) -> ResultT:
    try:
        return await awaitable
    except RuntimeEntityNotFound as error:
        raise HTTPException(status_code=404, detail="Опубликованная схема сущности не найдена") from error
    except RuntimeObjectNotFound as error:
        raise HTTPException(status_code=404, detail="Объект сущности не найден") from error
    except AttachmentNotFound as error:
        raise HTTPException(status_code=404, detail="Файл объекта не найден") from error
    except AttachmentValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


async def _authorize(
    session: AsyncSession,
    actor,
    action: PermissionAction,
    entity_code: str,
    object_id: UUID,
) -> None:
    try:
        await AuthorizationService(session).require_entity_code(
            actor,
            action,
            entity_code,
            object_id=object_id,
        )
    except AccessDenied as error:
        raise HTTPException(status_code=403, detail="Недостаточно прав для файла объекта") from error
    except AccessResourceNotFound as error:
        raise HTTPException(status_code=404, detail="Сущность или объект не найден") from error
    finally:
        await session.rollback()


def _iter_storage_response(storage_response: object) -> Iterator[bytes]:
    try:
        yield from storage_response.stream(32 * 1024)
    finally:
        storage_response.close()
        storage_response.release_conn()


def _content_disposition(filename: str) -> str:
    from urllib.parse import quote

    return f"attachment; filename*=UTF-8''{quote(filename)}"
