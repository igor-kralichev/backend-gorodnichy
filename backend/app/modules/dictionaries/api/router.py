from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, Query, UploadFile, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.config import settings
from app.core.security import AdminActor, CurrentActor
from app.modules.audit.application.service import AuditService
from app.modules.access.application.service import (
    AccessDenied,
    AccessResourceNotFound,
    AuthorizationService,
)
from app.modules.dictionaries.api.schemas import (
    DictionaryCreate,
    DictionaryDeleteRead,
    DictionaryExcelImportRead,
    DictionaryExcelPreviewRead,
    DictionaryItemMutation,
    DictionaryItemUpdate,
    DictionaryListRead,
    DictionaryRead,
    DictionaryStatusRead,
    DictionaryUpdate,
)
from app.modules.dictionaries.application.service import DictionaryService
from app.modules.excel.application.service import ExcelValidationError
from app.modules.dictionaries.domain.errors import (
    DictionaryAlreadyExists,
    DictionaryConflict,
    DictionaryEntityNotFound,
    DictionaryItemNotFound,
    DictionaryNotFound,
)
from app.modules.entities.infrastructure.models import EntitySchemaModel
from app.shared.db.models import MembershipModel

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
    _actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    entity_id: Annotated[
        UUID | None,
        Query(alias="entityId", description="Отобрать справочники конкретной сущности"),
    ] = None,
    include_shared: Annotated[
        bool,
        Query(alias="includeShared", description="Добавить global и справочники организации"),
    ] = True,
    scope: Annotated[
        Literal["global", "organization", "entity"] | None,
        Query(description="Область доступности справочника"),
    ] = None,
    owner_organization_id: Annotated[
        UUID | None,
        Query(alias="ownerOrganizationId", description="Организация-владелец"),
    ] = None,
    include_archived: Annotated[
        bool,
        Query(alias="includeArchived", description="Включить архивные справочники"),
    ] = False,
    query: Annotated[
        str | None,
        Query(alias="q", min_length=1, max_length=300, description="Поиск по коду и названию"),
    ] = None,
    sort: Annotated[
        Literal["name", "-name", "updatedAt", "-updatedAt"],
        Query(description="Сортировка справочников"),
    ] = "name",
    limit: Annotated[int, Query(ge=1, le=200, description="Количество справочников на странице")] = 50,
    offset: Annotated[int, Query(ge=0, description="Смещение от начала списка")] = 0,
) -> DictionaryListRead:
    can_manage = settings.admin_role_name in _actor.roles
    if entity_id is not None and not can_manage:
        entity_code = await session.scalar(
            select(EntitySchemaModel.code).where(EntitySchemaModel.id == entity_id)
        )
        if entity_code is None:
            raise HTTPException(status_code=404, detail="Сущность не найдена")
        try:
            await AuthorizationService(session).require_entity_code(
                _actor,
                "read",
                entity_code,
            )
        except AccessDenied as error:
            raise HTTPException(status_code=403, detail="Недостаточно прав") from error
        except AccessResourceNotFound as error:
            raise HTTPException(status_code=404, detail="Сущность не найдена") from error
    visible_organization_ids = set(
        await session.scalars(
            select(MembershipModel.organization_id).where(
                MembershipModel.user_id == _actor.id,
                MembershipModel.active.is_(True),
            )
        )
    )
    response = await DictionaryService(session).list_page(
        entity_id=entity_id,
        include_shared=include_shared,
        can_manage=can_manage,
        visible_organization_ids=visible_organization_ids,
        scope=scope,
        owner_organization_id=owner_organization_id,
        include_archived=include_archived,
        query=query,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    if can_manage:
        response.items = [_with_manage(item) for item in response.items]
    return response


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
        return _with_manage(response)
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
    _actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DictionaryRead:
    try:
        response = await DictionaryService(session).get(dictionary_id)
        await _ensure_dictionary_read(session, _actor, response)
        return (
            _with_manage(response)
            if settings.admin_role_name in _actor.roles
            else response
        )
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
        return _with_manage(response)
    except DictionaryNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except DictionaryAlreadyExists as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except DictionaryConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except IntegrityError as error:
        raise HTTPException(
            status_code=409,
            detail="Изменение нарушает уникальность или связь данных",
        ) from error


@router.post(
    "/{dictionaryId}/items",
    response_model=DictionaryRead,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить элемент справочника",
)
async def add_dictionary_item(
    dictionary_id: DictionaryId,
    payload: DictionaryItemMutation,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DictionaryRead:
    return await _mutate_item(
        session,
        actor,
        dictionary_id,
        "dictionary.item.created",
        lambda service: service.add_item(dictionary_id, payload),
    )


@router.patch(
    "/{dictionaryId}/items/{itemId}",
    response_model=DictionaryRead,
    response_model_by_alias=True,
    summary="Изменить элемент справочника",
)
async def update_dictionary_item(
    dictionary_id: DictionaryId,
    item_id: Annotated[UUID, Path(alias="itemId", description="UUID элемента")],
    payload: DictionaryItemUpdate,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DictionaryRead:
    return await _mutate_item(
        session,
        actor,
        dictionary_id,
        "dictionary.item.updated",
        lambda service: service.update_item(dictionary_id, item_id, payload),
    )


@router.delete(
    "/{dictionaryId}/items/{itemId}",
    response_model=DictionaryRead,
    response_model_by_alias=True,
    summary="Удалить элемент справочника",
)
async def delete_dictionary_item(
    dictionary_id: DictionaryId,
    item_id: Annotated[UUID, Path(alias="itemId", description="UUID элемента")],
    revision: Annotated[int, Query(ge=1, description="Ожидаемая ревизия справочника")],
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DictionaryRead:
    return await _mutate_item(
        session,
        actor,
        dictionary_id,
        "dictionary.item.deleted",
        lambda service: service.delete_item(dictionary_id, item_id, revision),
    )


@router.post(
    "/{dictionaryId}/excel/preview",
    response_model=DictionaryExcelPreviewRead,
    response_model_by_alias=True,
    summary="Проверить XLSX со значениями справочника",
)
async def preview_dictionary_excel(
    dictionary_id: DictionaryId,
    file: Annotated[UploadFile, File(description="Книга XLSX до 20 МБ")],
    _actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    sheet_name: Annotated[str | None, Form(alias="sheetName")] = None,
    header_row: Annotated[int, Form(alias="headerRow", ge=1)] = 1,
    value_column: Annotated[str | None, Form(alias="valueColumn")] = None,
    preview_rows: Annotated[int, Form(alias="previewRows", ge=1, le=200)] = 50,
) -> DictionaryExcelPreviewRead:
    content = await _dictionary_xlsx(file)
    try:
        return await DictionaryService(session).preview_excel(
            dictionary_id,
            content,
            sheet_name=sheet_name,
            header_row=header_row,
            value_column=value_column,
            preview_rows=preview_rows,
        )
    except DictionaryNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ExcelValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post(
    "/{dictionaryId}/excel/import",
    response_model=DictionaryExcelImportRead,
    response_model_by_alias=True,
    summary="Добавить значения справочника из XLSX",
    description="Идемпотентно добавляет новые значения и не удаляет существующие вручную.",
)
async def import_dictionary_excel(
    dictionary_id: DictionaryId,
    file: Annotated[UploadFile, File(description="Книга XLSX до 20 МБ")],
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    revision: Annotated[int, Form(ge=1)],
    sheet_name: Annotated[str | None, Form(alias="sheetName")] = None,
    header_row: Annotated[int, Form(alias="headerRow", ge=1)] = 1,
    value_column: Annotated[str | None, Form(alias="valueColumn")] = None,
) -> DictionaryExcelImportRead:
    content = await _dictionary_xlsx(file)
    service = DictionaryService(session)
    try:
        before = await service.get(dictionary_id)
        await session.rollback()
        response = await service.import_excel(
            dictionary_id,
            content,
            revision=revision,
            sheet_name=sheet_name,
            header_row=header_row,
            value_column=value_column,
        )
        await AuditService(session).record(
            actor=actor,
            resource_type="dictionary",
            resource_id=response.dictionary.id,
            resource_code=response.dictionary.code,
            resource_name=response.dictionary.name,
            action="dictionary.excel_imported",
            old_value=before,
            new_value=response,
        )
        return response.model_copy(
            update={"dictionary": _with_manage(response.dictionary)}
        )
    except DictionaryNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except DictionaryConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ExcelValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


async def _mutate_item(
    session: AsyncSession,
    actor,
    dictionary_id: UUID,
    action: str,
    operation,
) -> DictionaryRead:
    service = DictionaryService(session)
    try:
        before = await service.get(dictionary_id)
        await session.rollback()
        response = await operation(service)
        await AuditService(session).record(
            actor=actor,
            resource_type="dictionary",
            resource_id=response.id,
            resource_code=response.code,
            resource_name=response.name,
            action=action,
            old_value=before,
            new_value=response,
        )
        return _with_manage(response)
    except (DictionaryNotFound, DictionaryItemNotFound) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except DictionaryConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except IntegrityError as error:
        raise HTTPException(status_code=409, detail="Элемент уже существует") from error


async def _dictionary_xlsx(file: UploadFile) -> bytes:
    filename = (file.filename or "").strip().lower()
    if not filename.endswith(".xlsx"):
        raise HTTPException(status_code=422, detail="Поддерживается только XLSX")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="XLSX-файл пуст")
    if len(content) > min(settings.excel_max_file_size_bytes, 20 * 1024 * 1024):
        raise HTTPException(status_code=413, detail="XLSX-файл превышает 20 МБ")
    if not content.startswith(b"PK"):
        raise HTTPException(status_code=422, detail="Файл не является книгой XLSX")
    return content


def _with_manage(response: DictionaryRead) -> DictionaryRead:
    return response.model_copy(update={"capabilities": ["read", "manage"]})


async def _ensure_dictionary_read(
    session: AsyncSession,
    actor,
    dictionary: DictionaryRead,
) -> None:
    if settings.admin_role_name in actor.roles or dictionary.scope == "global":
        return
    if dictionary.scope == "organization":
        membership = await session.scalar(
            select(MembershipModel.id).where(
                MembershipModel.user_id == actor.id,
                MembershipModel.organization_id == dictionary.owner_organization_id,
                MembershipModel.active.is_(True),
            )
        )
        if membership is None:
            raise HTTPException(status_code=403, detail="Справочник недоступен")
        return
    entity_code = await session.scalar(
        select(EntitySchemaModel.code).where(EntitySchemaModel.id == dictionary.entity_id)
    )
    if entity_code is None:
        raise HTTPException(status_code=404, detail="Сущность справочника не найдена")
    try:
        await AuthorizationService(session).require_entity_code(actor, "read", entity_code)
    except AccessDenied as error:
        raise HTTPException(status_code=403, detail="Справочник недоступен") from error


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
