import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, Query, UploadFile
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.core.security import CurrentActor
from app.core.storage import get_minio_client
from app.modules.access.api.schemas import PermissionAction
from app.modules.access.application.service import (
    AccessDenied,
    AccessResourceNotFound,
    AuthorizationService,
)
from app.modules.excel.api.schemas import (
    ExcelMapping,
    ExcelPreviewRead,
    ImportProfileCreate,
    ImportProfileDeleteRead,
    ImportProfileRead,
)
from app.modules.excel.application.service import ExcelService, ExcelValidationError, ImportProfileService
from app.modules.objects.api.schemas import ObjectImportJobRead
from app.modules.objects.application.imports import ObjectImportService
from app.modules.objects.application.service import RuntimeEntityNotFound

router = APIRouter(tags=["Excel"])
EntityCode = Annotated[str, Path(alias="entityCode")]


@router.post(
    "/entities/{entityCode}/excel/preview",
    response_model=ExcelPreviewRead,
    summary="Предварительно проверить XLSX",
    description="Возвращает листы, заголовки, предложенное сопоставление и первые строки без записи объектов.",
)
async def preview_excel(
    entity_code: EntityCode,
    file: Annotated[UploadFile, File(description="Книга XLSX без макросов")],
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    mapping_json: Annotated[str | None, Form(alias="mapping")] = None,
    preview_rows: Annotated[int, Form(alias="previewRows", ge=1, le=100)] = 20,
) -> ExcelPreviewRead:
    await _authorize(session, actor, "import", entity_code)
    content = await _xlsx_content(file)
    mapping = _mapping(mapping_json)
    try:
        return await ExcelService(session).preview(entity_code, content, mapping, preview_rows)
    except (ExcelValidationError, RuntimeEntityNotFound) as error:
        raise _http_error(error) from error


@router.post(
    "/entities/{entityCode}/excel/import",
    response_model=ObjectImportJobRead,
    status_code=202,
    summary="Поставить XLSX-импорт в очередь",
    description="Преобразует подтверждённое сопоставление в единый объектный контракт и выполняет импорт worker-ом.",
)
async def import_excel(
    entity_code: EntityCode,
    file: Annotated[UploadFile, File(description="Книга XLSX без макросов")],
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    mapping_json: Annotated[str | None, Form(alias="mapping")] = None,
) -> ObjectImportJobRead:
    await _authorize(session, actor, "import", entity_code)
    content = await _xlsx_content(file)
    mapping = _mapping(mapping_json)
    if mapping is None:
        try:
            preview = await ExcelService(session).preview(entity_code, content, None, 1)
            mapping = ExcelMapping(
                sheet_name=preview.selected_sheet,
                header_row=preview.header_row,
                columns=preview.proposed_mapping,
            )
        except (ExcelValidationError, RuntimeEntityNotFound) as error:
            raise _http_error(error) from error
        await session.rollback()
    try:
        return await ObjectImportService(session, get_minio_client()).enqueue(
            entity_code=entity_code,
            source_name=(file.filename or "import.xlsx").strip(),
            content=content,
            total_rows=0,
            actor=actor,
            source_format="xlsx",
            mapping=mapping.model_dump(mode="json", by_alias=True),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except (ExcelValidationError, RuntimeEntityNotFound) as error:
        raise _http_error(error) from error


@router.get(
    "/entities/{entityCode}/excel/export",
    summary="Выгрузить объекты в XLSX",
    description="Добавляет стабильные ID объекта, ревизии, версии схемы и UUID полей.",
)
async def export_excel(
    entity_code: EntityCode,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    await _authorize(session, actor, "export", entity_code)
    try:
        name, content = await ExcelService(session).export(entity_code)
    except RuntimeEntityNotFound as error:
        raise _http_error(error) from error
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/importProfiles", response_model=ImportProfileRead, status_code=201, summary="Сохранить шаблон импорта")
async def create_import_profile(
    payload: ImportProfileCreate,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ImportProfileRead:
    try:
        return await ImportProfileService(session).create(payload, actor.id)
    except IntegrityError as error:
        raise HTTPException(status_code=409, detail="Шаблон импорта с таким названием уже существует") from error


@router.get("/importProfiles", response_model=list[ImportProfileRead], summary="Получить шаблоны импорта")
async def list_import_profiles(
    entity_id: Annotated[UUID, Query(alias="entityId")],
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ImportProfileRead]:
    return await ImportProfileService(session).list(entity_id, actor.id)


@router.delete(
    "/importProfiles/{profileId}",
    response_model=ImportProfileDeleteRead,
    summary="Удалить шаблон импорта",
)
async def delete_import_profile(
    profile_id: Annotated[UUID, Path(alias="profileId")],
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ImportProfileDeleteRead:
    try:
        await ImportProfileService(session).delete(profile_id, actor.id)
        return ImportProfileDeleteRead(id=profile_id)
    except ExcelValidationError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


async def _xlsx_content(file: UploadFile) -> bytes:
    filename = (file.filename or "").strip().lower()
    if not filename.endswith(".xlsx"):
        raise HTTPException(status_code=422, detail="Поддерживается только формат XLSX без макросов")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="XLSX-файл пуст")
    if len(content) > settings.excel_max_file_size_bytes:
        raise HTTPException(status_code=413, detail="XLSX-файл превышает допустимый размер")
    if not content.startswith(b"PK"):
        raise HTTPException(status_code=422, detail="Содержимое файла не соответствует формату XLSX")
    return content


def _mapping(value: str | None) -> ExcelMapping | None:
    if value is None or not value.strip():
        return None
    try:
        return ExcelMapping.model_validate(json.loads(value))
    except (json.JSONDecodeError, ValidationError) as error:
        raise HTTPException(status_code=422, detail="Некорректное сопоставление колонок") from error


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, RuntimeEntityNotFound):
        return HTTPException(status_code=404, detail="Опубликованная сущность не найдена")
    return HTTPException(status_code=422, detail=str(error))


async def _authorize(
    session: AsyncSession,
    actor,
    action: PermissionAction,
    entity_code: str,
) -> None:
    try:
        await AuthorizationService(session).require_entity_code(actor, action, entity_code)
    except AccessDenied as error:
        raise HTTPException(status_code=403, detail="Недостаточно прав для операции") from error
    except AccessResourceNotFound as error:
        raise HTTPException(status_code=404, detail="Сущность не найдена") from error
    finally:
        await session.rollback()
