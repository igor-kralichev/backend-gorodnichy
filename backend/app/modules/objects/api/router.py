import re
from collections.abc import Awaitable
from typing import Annotated, Literal
from uuid import UUID

import orjson
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.core.security import CurrentActor
from app.core.storage import get_minio_client
from app.modules.attachments.application.service import delete_object_storage_files
from app.modules.access.api.schemas import PermissionAction
from app.modules.access.application.service import (
    AccessDenied,
    AccessResourceNotFound,
    AuthorizationService,
)
from app.modules.audit.application.service import AuditService
from app.modules.objects.api.schemas import (
    EntityObjectBulkCreateRead,
    EntityObjectCreate,
    EntityObjectDeleteRead,
    EntityObjectPage,
    EntityObjectPatch,
    EntityObjectRead,
    EntityObjectStatusRead,
    ObjectFilter,
    ObjectClusterPage,
    ObjectImportJobRead,
    ObjectSearch,
)
from app.modules.objects.application.imports import ObjectImportService
from app.modules.objects.application.service import (
    RevisionConflict,
    RuntimeEntityNotFound,
    RuntimeObjectNotFound,
    RuntimeObjectService,
    RuntimeValidationError,
)

router = APIRouter(prefix="/entities", tags=["Объекты сущностей"])
FILTER_PATTERN = re.compile(r"^filters\[([^]]+)]\[([^]]+)]$")
OPERATOR_ALIASES = {
    "eq": "equals",
    "ne": "notEquals",
    "neq": "notEquals",
    "starts": "startsWith",
    "ends": "endsWith",
    "gt": "greaterThan",
    "gte": "greaterOrEqual",
    "lt": "lessThan",
    "lte": "lessOrEqual",
    "notNull": "filled",
    "null": "empty",
}

EntityCode = Annotated[
    str,
    Path(alias="entityCode", description="Уникальный код опубликованной сущности"),
]
ObjectId = Annotated[UUID, Path(alias="objectId", description="Идентификатор объекта")]


@router.get(
    "/{entityCode}/objects",
    response_model=EntityObjectPage,
    response_model_by_alias=True,
    summary="Получить список объектов сущности",
    description=(
        "Возвращает страницу объектов с фильтрацией и сортировкой "
        "по системным и динамическим полям."
    ),
)
async def list_entity_objects(
    entity_code: EntityCode,
    request: Request,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    parent_object_id: Annotated[
        UUID | None,
        Query(alias="parentObjectId", description="Показать только вложенные объекты указанного родителя"),
    ] = None,
    logic: Literal["and", "or"] = Query(
        default="and",
        description="Логика объединения фильтров",
    ),
    sort: str | None = Query(
        default=None,
        max_length=130,
        description="Поле сортировки; префикс - означает убывание",
    ),
    limit: int = Query(
        default=25,
        ge=1,
        le=1000,
        description="Количество объектов на странице",
    ),
    offset: int = Query(default=0, ge=0, description="Смещение от начала выборки"),
    bbox: Annotated[
        str | None,
        Query(description="Область карты в формате minLon,minLat,maxLon,maxLat"),
    ] = None,
) -> EntityObjectPage:
    await _authorize(session, actor, "read", entity_code)
    filters = _query_filters(request)
    return await _execute(
        RuntimeObjectService(session).list_page(
            entity_code,
            ObjectSearch(
                logic=logic,
                filters=filters,
                sort=sort,
                parent_object_id=parent_object_id,
                bbox=_parse_bbox(bbox) if bbox else None,
                limit=limit,
                offset=offset,
            ),
        )
    )


@router.get(
    "/{entityCode}/objects/clusters",
    response_model=ObjectClusterPage,
    response_model_by_alias=True,
    summary="Получить кластеры объектов в области карты",
)
async def list_entity_object_clusters(
    entity_code: EntityCode,
    bbox: Annotated[str, Query(description="minLon,minLat,maxLon,maxLat")],
    zoom: Annotated[int, Query(ge=0, le=24)],
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ObjectClusterPage:
    await _authorize(session, actor, "read", entity_code)
    return await _execute(
        RuntimeObjectService(session).list_clusters(entity_code, _parse_bbox(bbox), zoom)
    )


@router.post(
    "/{entityCode}/objects/search",
    response_model=EntityObjectPage,
    response_model_by_alias=True,
    summary="Найти объекты сущности",
    description=(
        "Выполняет расширенный поиск по набору фильтров "
        "из тела запроса."
    ),
)
async def search_entity_objects(
    entity_code: EntityCode,
    payload: ObjectSearch,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EntityObjectPage:
    await _authorize(session, actor, "read", entity_code)
    return await _execute(RuntimeObjectService(session).list_page(entity_code, payload))


@router.get(
    "/{entityCode}/objects/{objectId}",
    response_model=EntityObjectRead,
    response_model_by_alias=True,
    summary="Получить объект сущности",
)
async def get_entity_object(
    entity_code: EntityCode,
    object_id: ObjectId,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EntityObjectRead:
    await _authorize(session, actor, "read", entity_code, object_id)
    return await _execute(RuntimeObjectService(session).get(entity_code, object_id))


@router.post(
    "/{entityCode}/objects",
    response_model=EntityObjectBulkCreateRead | ObjectImportJobRead,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_202_ACCEPTED: {
            "description": "Массив принят в фоновую загрузку",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ObjectImportJobRead"}
                }
            },
        }
    },
    summary="Создать объекты сущности",
    description=(
        "Принимает JSON-массив объектов; один объект также передаётся массивом из "
        "одного элемента. Небольшой массив обрабатывается сразу. Для большого "
        "массива backend сам создаёт временный JSON в MinIO и передаёт задачу "
        "import-worker через RabbitMQ."
    ),
)
async def create_entity_object(
    entity_code: EntityCode,
    payload: list[EntityObjectCreate],
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EntityObjectBulkCreateRead | ObjectImportJobRead:
    await _authorize(session, actor, "create", entity_code)
    import_service = ObjectImportService(session, get_minio_client())

    if len(payload) > settings.import_async_threshold:
        content = orjson.dumps(
            [item.model_dump(mode="json", by_alias=True) for item in payload]
        )
        job = await _execute(
            import_service.enqueue(
                entity_code=entity_code,
                source_name=f"{entity_code}-objects.json",
                content=content,
                total_rows=len(payload),
                actor=actor,
            )
        )
        await AuditService(session).record(
            actor=actor,
            resource_type="import_job",
            resource_id=job.id,
            resource_code=entity_code,
            resource_name=job.source_name,
            action="entity_object.import_queued",
            old_value=None,
            new_value=job,
            metadata={"totalRows": job.total_rows},
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=job.model_dump(mode="json", by_alias=True),
        )

    response = await _execute(RuntimeObjectService(session).create_many(entity_code, payload, actor.id))
    audit = AuditService(session)
    for item in response.data:
        await audit.record(
            actor=actor,
            resource_type="entity_object",
            resource_id=item.id,
            resource_code=entity_code,
            resource_name=_object_resource_name(item),
            action="entity_object.created",
            old_value=None,
            new_value=item,
            metadata={
                "bulk": True,
                "bulkSize": response.created,
                "parentObjectId": str(item.parent_object_id) if item.parent_object_id else None,
            },
        )
    return response


@router.patch(
    "/{entityCode}/objects/{objectId}",
    response_model=EntityObjectRead,
    response_model_by_alias=True,
    summary="Изменить объект сущности",
    description=(
        "Частично обновляет значения или геометрию "
        "и создаёт новую ревизию истории."
    ),
)
async def update_entity_object(
    entity_code: EntityCode,
    object_id: ObjectId,
    payload: EntityObjectPatch,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EntityObjectRead:
    await _authorize(session, actor, "update", entity_code, object_id)
    service = RuntimeObjectService(session)
    before = await _execute(service.get(entity_code, object_id))
    await session.rollback()
    response = await _execute(service.update(entity_code, object_id, payload, actor.id))
    await AuditService(session).record(
        actor=actor,
        resource_type="entity_object",
        resource_id=response.id,
        resource_code=entity_code,
        resource_name=_object_resource_name(response),
        action="entity_object.updated",
        old_value=before,
        new_value=response,
    )
    return response


@router.post(
    "/{entityCode}/objects/{objectId}/copy",
    response_model=EntityObjectRead,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    summary="Создать копию объекта",
    description=(
        "Копирует значения, геометрию и принадлежность объекта. "
        "Уникальные, архивные и вычисляемые поля очищаются."
    ),
)
async def copy_entity_object(
    entity_code: EntityCode,
    object_id: ObjectId,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EntityObjectRead:
    await _authorize(session, actor, "read", entity_code, object_id)
    await _authorize(session, actor, "create", entity_code)
    response = await _execute(
        RuntimeObjectService(session).copy(entity_code, object_id, actor.id)
    )
    await AuditService(session).record(
        actor=actor,
        resource_type="entity_object",
        resource_id=response.id,
        resource_code=entity_code,
        resource_name=_object_resource_name(response),
        action="entity_object.copied",
        old_value=None,
        new_value=response,
        metadata={"sourceObjectId": str(object_id)},
    )
    return response


@router.post(
    "/{entityCode}/objects/{objectId}/archive",
    response_model=EntityObjectStatusRead,
    response_model_by_alias=True,
    summary="Переместить объект в архив",
    description=(
        "Архивирует объект: он скрывается из рабочего списка, "
        "но остаётся в базе и может быть восстановлен."
    ),
)
async def archive_entity_object(
    entity_code: EntityCode,
    object_id: ObjectId,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EntityObjectStatusRead:
    await _authorize(session, actor, "archive", entity_code, object_id)
    service = RuntimeObjectService(session)
    before = await _execute(service.get(entity_code, object_id))
    await session.rollback()
    response = await _execute(service.archive(entity_code, object_id, actor.id))
    await AuditService(session).record(
        actor=actor,
        resource_type="entity_object",
        resource_id=response.id,
        resource_code=entity_code,
        resource_name=_object_resource_name(before),
        action="entity_object.archived",
        old_value=before,
        new_value=response,
    )
    return response


@router.post(
    "/{entityCode}/objects/{objectId}/restore",
    response_model=EntityObjectStatusRead,
    response_model_by_alias=True,
    summary="Восстановить объект из архива",
    description=(
        "Возвращает объект из архива и повторно проверяет полноту данных. "
        "Полный объект становится опубликованным, неполный — черновиком."
    ),
)
async def restore_entity_object(
    entity_code: EntityCode,
    object_id: ObjectId,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EntityObjectStatusRead:
    await _authorize(session, actor, "archive", entity_code, object_id)
    service = RuntimeObjectService(session)
    before = await _execute(service.get(entity_code, object_id))
    await session.rollback()
    response = await _execute(service.restore(entity_code, object_id, actor.id))
    await AuditService(session).record(
        actor=actor,
        resource_type="entity_object",
        resource_id=response.id,
        resource_code=entity_code,
        resource_name=_object_resource_name(before),
        action="entity_object.restored",
        old_value=before,
        new_value=response,
    )
    return response


@router.delete(
    "/{entityCode}/objects/{objectId}",
    response_model=EntityObjectDeleteRead,
    response_model_by_alias=True,
    summary="Удалить объект",
    description=(
        "Физически удаляет объект, его вложения и историю. "
        "Строки импорта, которые ссылались на объект, остаются без связи с ним."
    ),
)
async def delete_entity_object(
    entity_code: EntityCode,
    object_id: ObjectId,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EntityObjectDeleteRead:
    await _authorize(session, actor, "delete", entity_code, object_id)
    service = RuntimeObjectService(session)
    before = await _execute(service.get(entity_code, object_id))
    await session.rollback()
    await delete_object_storage_files(session, get_minio_client(), object_id)
    await session.rollback()
    response = await _execute(service.delete(entity_code, object_id, actor.id))
    await AuditService(session).record(
        actor=actor,
        resource_type="entity_object",
        resource_id=response.id,
        resource_code=entity_code,
        resource_name=_object_resource_name(before),
        action="entity_object.deleted",
        old_value=before,
        new_value=None,
    )
    return response


async def _execute[ResultT](awaitable: Awaitable[ResultT]) -> ResultT:
    try:
        return await awaitable
    except RuntimeEntityNotFound as error:
        raise HTTPException(
            status_code=404,
            detail="Опубликованная схема сущности не найдена",
        ) from error
    except RuntimeObjectNotFound as error:
        raise HTTPException(status_code=404, detail="Объект сущности не найден") from error
    except RuntimeValidationError as error:
        raise HTTPException(status_code=422, detail=error.issues) from error
    except RevisionConflict as error:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "revision_conflict",
                "message": "Объект уже изменён другим запросом",
                "currentRevision": error.current_revision,
            },
        ) from error


async def _authorize(
    session: AsyncSession,
    actor,
    action: PermissionAction,
    entity_code: str,
    object_id: UUID | None = None,
) -> None:
    try:
        await AuthorizationService(session).require_entity_code(
            actor,
            action,
            entity_code,
            object_id=object_id,
        )
    except AccessDenied as error:
        raise HTTPException(status_code=403, detail="Недостаточно прав для операции") from error
    except AccessResourceNotFound as error:
        raise HTTPException(status_code=404, detail="Сущность или объект не найден") from error
    finally:
        await session.rollback()


def _query_filters(request: Request) -> list[ObjectFilter]:
    filters: list[ObjectFilter] = []
    for key, value in request.query_params.multi_items():
        match = FILTER_PATTERN.match(key)
        if not match:
            continue
        field, raw_operator = match.groups()
        operator = OPERATOR_ALIASES.get(raw_operator, raw_operator)
        try:
            filters.append(ObjectFilter(field=field.removeprefix("values."), operator=operator, value=value))
        except ValueError as error:
            raise HTTPException(
                status_code=422,
                detail=f"Оператор фильтра «{raw_operator}» не поддерживается",
            ) from error
    return filters


def _object_resource_name(value: EntityObjectRead) -> str:
    for field_code in ("name", "nazvanie", "title", "naimenovanie"):
        field_value = value.values.get(field_code)
        if isinstance(field_value, str) and field_value.strip():
            return field_value.strip()
    return str(value.id)


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    try:
        parts = tuple(float(item.strip()) for item in value.split(","))
    except ValueError as error:
        raise HTTPException(status_code=422, detail="bbox должен содержать четыре числа") from error
    if len(parts) != 4:
        raise HTTPException(status_code=422, detail="bbox должен содержать четыре числа")
    min_lon, min_lat, max_lon, max_lat = parts
    if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
        raise HTTPException(status_code=422, detail="bbox находится за пределами WGS 84")
    return min_lon, min_lat, max_lon, max_lat
