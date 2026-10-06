from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.security import AdminActor, CurrentActor
from app.modules.audit.application.service import AuditService
from app.modules.entities.api.schemas import (
    EntityCreate,
    EntityDeleteRead,
    EntityDuplicateCreate,
    EntityListRead,
    EntityRead,
    EntityStatusRead,
    EntityUpdate,
)
from app.modules.entities.application.management import EntitySchemaManagementService
from app.modules.entities.application.service import CreateEntitySchemaService
from app.modules.entities.domain.enums import EntityStatus
from app.modules.entities.domain.errors import (
    EntityCodeAlreadyExists,
    EntityFieldReferenceError,
    EntitySchemaConflict,
    EntitySchemaNotFound,
)

router = APIRouter(prefix="/entities", tags=["Схемы сущностей"])


def get_redis(request: Request) -> Redis:
    return cast(Redis, request.app.state.redis)


def raise_entity_http_error(error: Exception) -> None:
    if isinstance(error, EntitySchemaNotFound):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "entity_not_found", "message": "Сущность не найдена"},
        ) from error
    if isinstance(error, EntityCodeAlreadyExists):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "entity_code_conflict", "message": str(error)},
        ) from error
    if isinstance(error, EntityFieldReferenceError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_field_reference", "message": str(error)},
        ) from error
    if isinstance(error, EntitySchemaConflict):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "entity_schema_conflict", "message": str(error)},
        ) from error
    if isinstance(error, IntegrityError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "entity_integrity_conflict",
                "message": "Изменение нарушает уникальность или связь данных",
            },
        ) from error
    raise error


@router.post(
    "",
    response_model=EntityRead,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    summary="Создать черновик сущности",
    description=(
        "Создаёт схему, поля, настройки карты и первую версию. "
        "Коды можно не передавать: они будут сформированы из названий."
    ),
)
async def create_entity_schema(
    payload: EntityCreate,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> EntityRead:
    try:
        response = await CreateEntitySchemaService(session, redis).execute(payload, actor.id)
        await AuditService(session).record(
            actor=actor,
            resource_type="entity_schema",
            resource_id=response.id,
            resource_code=response.code,
            resource_name=response.name,
            action="entity_schema.created",
            old_value=None,
            new_value=response,
        )
        return response
    except Exception as error:
        raise_entity_http_error(error)
        raise


@router.get(
    "",
    response_model=EntityListRead,
    response_model_by_alias=True,
    summary="Получить список сущностей",
    description="Архивные сущности по умолчанию исключены из рабочего списка.",
)
async def list_entity_schemas(
    _actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    status_filter: Annotated[
        EntityStatus | None,
        Query(alias="status", description="Отобрать сущности по статусу"),
    ] = None,
    include_archived: Annotated[
        bool,
        Query(alias="includeArchived", description="Включить архивные сущности"),
    ] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EntityListRead:
    return await EntitySchemaManagementService(session, redis).list(
        status_filter=status_filter,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/{identifier}/publish",
    response_model=EntityRead,
    response_model_by_alias=True,
    summary="Опубликовать сущность",
    description=(
        "Активирует схему, синхронизирует картографический слой и открывает "
        "универсальные runtime-маршруты."
    ),
)
async def publish_entity_schema(
    identifier: str,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> EntityRead:
    try:
        service = EntitySchemaManagementService(session, redis)
        before = await service.get(identifier)
        await session.rollback()
        response = await service.publish(identifier, actor.id)
        await AuditService(session).record(
            actor=actor,
            resource_type="entity_schema",
            resource_id=response.id,
            resource_code=response.code,
            resource_name=response.name,
            action="entity_schema.published",
            old_value=before,
            new_value=response,
        )
        return response
    except Exception as error:
        raise_entity_http_error(error)
        raise


@router.post(
    "/{identifier}/duplicate",
    response_model=EntityRead,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    summary="Создать копию сущности",
    description=(
        "Копирует поля и настройки карты в новый черновик, но не копирует объекты."
    ),
)
async def duplicate_entity_schema(
    identifier: str,
    payload: EntityDuplicateCreate,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> EntityRead:
    try:
        service = EntitySchemaManagementService(session, redis)
        source = await service.get(identifier)
        await session.rollback()
        response = await service.duplicate(identifier, payload, actor.id)
        await AuditService(session).record(
            actor=actor,
            resource_type="entity_schema",
            resource_id=response.id,
            resource_code=response.code,
            resource_name=response.name,
            action="entity_schema.duplicated",
            old_value=None,
            new_value=response,
            metadata={"sourceEntityId": str(source.id), "sourceEntityCode": source.code},
        )
        return response
    except Exception as error:
        raise_entity_http_error(error)
        raise


@router.post(
    "/{identifier}/archive",
    response_model=EntityStatusRead,
    response_model_by_alias=True,
    summary="Переместить сущность в архив",
    description=(
        "Архивирует сущность: отключает слой, скрывает её из runtime "
        "и оставляет возможность восстановления."
    ),
)
async def archive_entity_schema(
    identifier: str,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> EntityStatusRead:
    try:
        service = EntitySchemaManagementService(session, redis)
        before = await service.get(identifier)
        await session.rollback()
        response = await service.archive(identifier, actor.id)
        await AuditService(session).record(
            actor=actor,
            resource_type="entity_schema",
            resource_id=response.id,
            resource_code=response.code,
            resource_name=before.name,
            action="entity_schema.archived",
            old_value=before,
            new_value=response,
        )
        return response
    except Exception as error:
        raise_entity_http_error(error)
        raise


@router.post(
    "/{identifier}/restore",
    response_model=EntityStatusRead,
    response_model_by_alias=True,
    summary="Восстановить сущность из архива",
    description=(
        "Возвращает архивную сущность в черновики. Для runtime её нужно "
        "опубликовать повторно."
    ),
)
async def restore_entity_schema(
    identifier: str,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> EntityStatusRead:
    try:
        service = EntitySchemaManagementService(session, redis)
        before = await service.get(identifier)
        await session.rollback()
        response = await service.restore(identifier, actor.id)
        await AuditService(session).record(
            actor=actor,
            resource_type="entity_schema",
            resource_id=response.id,
            resource_code=response.code,
            resource_name=before.name,
            action="entity_schema.restored",
            old_value=before,
            new_value=response,
        )
        return response
    except Exception as error:
        raise_entity_http_error(error)
        raise


@router.get(
    "/{identifier}",
    response_model=EntityRead,
    response_model_by_alias=True,
    summary="Получить сущность по UUID или коду",
)
async def get_entity_schema(
    identifier: str,
    _actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> EntityRead:
    try:
        return await EntitySchemaManagementService(session, redis).get(identifier)
    except Exception as error:
        raise_entity_http_error(error)
        raise


@router.patch(
    "/{identifier}",
    response_model=EntityRead,
    response_model_by_alias=True,
    summary="Изменить схему сущности",
    description=(
        "При наличии объектов запрещает удалять поля, менять их типы и отключать "
        "уже разрешённые геометрии."
    ),
)
async def update_entity_schema(
    identifier: str,
    payload: EntityUpdate,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> EntityRead:
    try:
        service = EntitySchemaManagementService(session, redis)
        before = await service.get(identifier)
        await session.rollback()
        response = await service.update(identifier, payload, actor.id)
        await AuditService(session).record(
            actor=actor,
            resource_type="entity_schema",
            resource_id=response.id,
            resource_code=response.code,
            resource_name=response.name,
            action="entity_schema.updated",
            old_value=before,
            new_value=response,
        )
        return response
    except Exception as error:
        raise_entity_http_error(error)
        raise


@router.delete(
    "/{identifier}",
    response_model=EntityDeleteRead,
    response_model_by_alias=True,
    summary="Удалить сущность",
    description=(
        "Физически удаляет сущность, её поля, настройки, слой, объекты, "
        "историю объектов, импорты и связанные справочники. "
        "Если на сущность ссылаются другие схемы, удаление будет отклонено."
    ),
)
async def delete_entity_schema(
    identifier: str,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> EntityDeleteRead:
    try:
        service = EntitySchemaManagementService(session, redis)
        before = await service.get(identifier)
        await session.rollback()
        response = await service.delete(identifier, actor.id)
        await AuditService(session).record(
            actor=actor,
            resource_type="entity_schema",
            resource_id=response.id,
            resource_code=response.code,
            resource_name=before.name,
            action="entity_schema.deleted",
            old_value=before,
            new_value=None,
        )
        return response
    except Exception as error:
        raise_entity_http_error(error)
        raise
