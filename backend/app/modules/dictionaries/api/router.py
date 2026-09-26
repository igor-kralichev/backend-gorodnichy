from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.security import AdminActor
from app.modules.audit.application.service import AuditService
from app.modules.dictionaries.api.schemas import (
    DictionaryCreate,
    DictionaryDeleteRead,
    DictionaryListRead,
    DictionaryRead,
    DictionaryStatusRead,
    DictionaryUpdate,
)
from app.modules.dictionaries.application.service import DictionaryService
from app.modules.dictionaries.domain.errors import (
    DictionaryAlreadyExists,
    DictionaryEntityNotFound,
    DictionaryNotFound,
)

router = APIRouter(prefix="/dictionaries", tags=["Справочники"])
DictionaryId = Annotated[UUID, Path(alias="dictionaryId", description="UUID справочника")]


@router.get(
    "",
    response_model=DictionaryListRead,
    response_model_by_alias=True,
    summary="Получить список справочников",
    description="Архивные справочники по умолчанию исключены из рабочего списка.",
)
async def list_dictionaries(
    session: Annotated[AsyncSession, Depends(get_session)],
    entity_id: Annotated[
        UUID | None,
        Query(alias="entityId", description="Отобрать справочники конкретной сущности"),
    ] = None,
    include_archived: Annotated[
        bool,
        Query(alias="includeArchived", description="Включить архивные справочники"),
    ] = False,
    limit: Annotated[int, Query(ge=1, le=200, description="Количество справочников на странице")] = 50,
    offset: Annotated[int, Query(ge=0, description="Смещение от начала списка")] = 0,
) -> DictionaryListRead:
    return await DictionaryService(session).list_page(
        entity_id=entity_id,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    response_model=DictionaryRead,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    summary="Создать справочник",
    description=(
        "Создаёт справочник для сущности и начальные элементы. "
        "Доступно только пользователю с ролью Admin."
    ),
)
async def create_dictionary(
    payload: DictionaryCreate,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DictionaryRead:
    try:
        response = await DictionaryService(session).create(payload)
        await AuditService(session).record(
            actor=actor,
            resource_type="dictionary",
            resource_id=response.id,
            resource_code=response.code,
            resource_name=response.name,
            action="dictionary.created",
            old_value=None,
            new_value=response,
        )
        return response
    except DictionaryEntityNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except DictionaryAlreadyExists as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except IntegrityError as error:
        raise HTTPException(
            status_code=409,
            detail="Изменение нарушает уникальность или связь данных",
        ) from error


@router.get(
    "/{dictionaryId}",
    response_model=DictionaryRead,
    response_model_by_alias=True,
    summary="Получить справочник",
)
async def get_dictionary(
    dictionary_id: DictionaryId,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DictionaryRead:
    try:
        return await DictionaryService(session).get(dictionary_id)
    except DictionaryNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.patch(
    "/{dictionaryId}",
    response_model=DictionaryRead,
    response_model_by_alias=True,
    summary="Изменить справочник",
    description=(
        "Обновляет код, название или полный набор элементов справочника. "
        "Доступно только пользователю с ролью Admin."
    ),
)
async def update_dictionary(
    dictionary_id: DictionaryId,
    payload: DictionaryUpdate,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DictionaryRead:
    try:
        service = DictionaryService(session)
        before = await service.get(dictionary_id)
        await session.rollback()
        response = await service.update(dictionary_id, payload)
        await AuditService(session).record(
            actor=actor,
            resource_type="dictionary",
            resource_id=response.id,
            resource_code=response.code,
            resource_name=response.name,
            action="dictionary.updated",
            old_value=before,
            new_value=response,
        )
        return response
    except DictionaryNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except DictionaryAlreadyExists as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except IntegrityError as error:
        raise HTTPException(
            status_code=409,
            detail="Изменение нарушает уникальность или связь данных",
        ) from error


@router.post(
    "/{dictionaryId}/archive",
    response_model=DictionaryStatusRead,
    response_model_by_alias=True,
    summary="Переместить справочник в архив",
    description=(
        "Делает справочник недоступным для выбора новых значений. "
        "Доступно только пользователю с ролью Admin."
    ),
)
async def archive_dictionary(
    dictionary_id: DictionaryId,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DictionaryStatusRead:
    try:
        service = DictionaryService(session)
        before = await service.get(dictionary_id)
        await session.rollback()
        response = await service.archive(dictionary_id)
        await AuditService(session).record(
            actor=actor,
            resource_type="dictionary",
            resource_id=response.id,
            resource_code=before.code,
            resource_name=before.name,
            action="dictionary.archived",
            old_value=before,
            new_value=response,
        )
        return response
    except DictionaryNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post(
    "/{dictionaryId}/restore",
    response_model=DictionaryStatusRead,
    response_model_by_alias=True,
    summary="Восстановить справочник из архива",
    description=(
        "Возвращает справочник в рабочий список. "
        "Доступно только пользователю с ролью Admin."
    ),
)
async def restore_dictionary(
    dictionary_id: DictionaryId,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DictionaryStatusRead:
    try:
        service = DictionaryService(session)
        before = await service.get(dictionary_id)
        await session.rollback()
        response = await service.restore(dictionary_id)
        await AuditService(session).record(
            actor=actor,
            resource_type="dictionary",
            resource_id=response.id,
            resource_code=before.code,
            resource_name=before.name,
            action="dictionary.restored",
            old_value=before,
            new_value=response,
        )
        return response
    except DictionaryNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.delete(
    "/{dictionaryId}",
    response_model=DictionaryDeleteRead,
    response_model_by_alias=True,
    summary="Удалить справочник",
    description=(
        "Физически удаляет справочник и его элементы. "
        "Если справочник используется полем enum, удаление будет отклонено."
    ),
)
async def delete_dictionary(
    dictionary_id: DictionaryId,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DictionaryDeleteRead:
    try:
        service = DictionaryService(session)
        before = await service.get(dictionary_id)
        await session.rollback()
        response = await service.delete(dictionary_id)
        await AuditService(session).record(
            actor=actor,
            resource_type="dictionary",
            resource_id=response.id,
            resource_code=before.code,
            resource_name=before.name,
            action="dictionary.deleted",
            old_value=before,
            new_value=None,
        )
        return response
    except DictionaryNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except IntegrityError as error:
        raise HTTPException(
            status_code=409,
            detail="Справочник используется сущностью и не может быть удалён",
        ) from error
