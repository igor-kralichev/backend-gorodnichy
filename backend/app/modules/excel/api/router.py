import json
from typing import Annotated, Literal
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
    ExcelExportJobCommand,
    ExcelExportJobPage,
    ExcelExportJobRead,
    ExcelExportRequest,
    ExcelImportPlanDecision,
    ExcelImportPlanRead,
    ExcelPreviewRead,
    ImportProfileCreate,
    ImportProfileDeleteRead,
    ImportProfileRead,
)
from app.modules.excel.application.service import (
    ExcelImportPlanService,
    ExcelService,
    ExcelValidationError,
    ImportProfileService,
)
from app.modules.excel.application.exports import (
    ExcelExportJobNotFound,
    ExcelExportJobService,
    ExcelExportNotReady,
    ExcelExportQueueUnavailable,
)
from app.modules.objects.api.schemas import ObjectFilter, ObjectImportJobRead, RegistryTreeSearch
from app.modules.objects.application.imports import ObjectImportService
from app.modules.objects.application.service import (
    RuntimeEntityNotFound,
    RuntimeObjectService,
    RuntimeValidationError,
)

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


@router.post(
    "/entities/{entityCode}/excel/plans",
    response_model=ExcelImportPlanRead,
    response_model_by_alias=True,
    status_code=201,
    summary="Подготовить план изменений из XLSX",
    description="Полностью проверяет файл и возвращает diff без изменения объектов.",
)
async def create_excel_plan(
    entity_code: EntityCode,
    file: Annotated[UploadFile, File(description="Книга XLSX без макросов")],
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    mapping_json: Annotated[str | None, Form(alias="mapping")] = None,
    idempotency_key: Annotated[str | None, Form(alias="idempotencyKey", max_length=255)] = None,
) -> ExcelImportPlanRead:
    await _authorize(session, actor, "import", entity_code)
    content = await _xlsx_content(file)
    mapping = _mapping(mapping_json)
    try:
        if mapping is None:
            preview = await ExcelService(session).preview(entity_code, content, None, 1)
            mapping = ExcelMapping(
                sheet_name=preview.selected_sheet,
                header_row=preview.header_row,
                columns=preview.proposed_mapping,
            )
            await session.rollback()
        return await ExcelImportPlanService(session).create(
            entity_code,
            (file.filename or "import.xlsx").strip(),
            content,
            mapping,
            actor,
            idempotency_key=idempotency_key,
        )
    except (ExcelValidationError, RuntimeEntityNotFound) as error:
        raise _http_error(error) from error
    except AccessDenied as error:
        raise HTTPException(
            status_code=403,
            detail="Недостаточно прав для операций, указанных в плане Excel",
        ) from error
    except AccessResourceNotFound as error:
        raise HTTPException(
            status_code=404,
            detail="Объект, родитель или сущность из плана Excel не найдены",
        ) from error


@router.get(
    "/excel/plans/{planId}",
    response_model=ExcelImportPlanRead,
    response_model_by_alias=True,
    summary="Получить строки и diff плана XLSX",
)
async def get_excel_plan(
    plan_id: Annotated[UUID, Path(alias="planId")],
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ExcelImportPlanRead:
    try:
        return await ExcelImportPlanService(session).get(
            plan_id,
            actor.id,
            limit=limit,
            offset=offset,
        )
    except ExcelValidationError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post(
    "/excel/plans/{planId}/decision",
    response_model=ExcelImportPlanRead,
    response_model_by_alias=True,
    summary="Применить или отклонить план XLSX",
)
async def decide_excel_plan(
    plan_id: Annotated[UUID, Path(alias="planId")],
    payload: ExcelImportPlanDecision,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ExcelImportPlanRead:
    try:
        return await ExcelImportPlanService(session).decide(plan_id, payload, actor)
    except ExcelValidationError as error:
        status_code = 409 if "измен" in str(error).casefold() else 422
        raise HTTPException(status_code=status_code, detail=str(error)) from error
    except AccessDenied as error:
        raise HTTPException(
            status_code=403,
            detail="Недостаточно прав для применения плана Excel",
        ) from error
    except AccessResourceNotFound as error:
        raise HTTPException(
            status_code=404,
            detail="Объект или сущность из плана Excel не найдены",
        ) from error


@router.get(
    "/entities/{entityCode}/excel/export",
    summary="Выгрузить объекты в XLSX",
    description=(
        "Формирует пользовательскую XLSX-выгрузку: первая строка содержит "
        "понятные заголовки, последующие строки — данные объектов."
    ),
)
async def export_excel(
    entity_code: EntityCode,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    await _authorize(session, actor, "export", entity_code)
    try:
        name, content = await ExcelService(session).export(entity_code, actor=actor)
    except (ExcelValidationError, RuntimeEntityNotFound) as error:
        raise _http_error(error) from error
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post(
    "/entities/{entityCode}/excel/exportSelection",
    summary="Выгрузить выбранные колонки реестра и подреестров",
    description=(
        "Создаёт XLSX: основной реестр помещается на первый лист, каждый "
        "подреестр — на отдельный лист вместе с выбранными колонками родителя. "
        "Фильтры, сортировка и набор колонок задаются отдельно для каждого реестра."
    ),
)
async def export_excel_selection(
    entity_code: EntityCode,
    payload: RegistryTreeSearch,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    await _authorize(session, actor, "export", entity_code)
    await _authorize_filter_dependencies(session, actor, entity_code, payload.filters)
    for child in payload.children:
        await _authorize(session, actor, "export", child.entity_code)
        await _authorize_filter_dependencies(
            session,
            actor,
            child.entity_code,
            child.filters,
        )
    try:
        name, content, _row_count = await ExcelService(session).export_tree(
            entity_code,
            payload,
            actor=actor,
        )
    except (ExcelValidationError, RuntimeEntityNotFound, RuntimeValidationError) as error:
        raise _http_error(error) from error
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post(
    "/entities/{entityCode}/excel/exportJobs",
    response_model=ExcelExportJobRead,
    response_model_by_alias=True,
    status_code=202,
    summary="Поставить XLSX-выгрузку в очередь",
    description=(
        "Сохраняет снимок колонок, фильтров и подреестров, затем "
        "формирует книгу в export-worker. Готовый файл хранится в MinIO."
    ),
)
async def enqueue_excel_export(
    entity_code: EntityCode,
    payload: ExcelExportRequest,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ExcelExportJobRead:
    await _authorize(session, actor, "export", entity_code)
    await _authorize_filter_dependencies(session, actor, entity_code, payload.filters)
    for child in payload.children:
        await _authorize(session, actor, "export", child.entity_code)
        await _authorize_filter_dependencies(
            session,
            actor,
            child.entity_code,
            child.filters,
        )
    try:
        return await ExcelExportJobService(
            session,
            get_minio_client(),
        ).enqueue(
            entity_code=entity_code,
            request=payload,
            actor=actor,
        )
    except RuntimeEntityNotFound as error:
        raise HTTPException(status_code=404, detail="Сущность не найдена") from error
    except ExcelExportQueueUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.get(
    "/excel/exportJobs",
    response_model=ExcelExportJobPage,
    response_model_by_alias=True,
    summary="Получить свои задачи XLSX-экспорта",
)
async def list_excel_export_jobs(
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    job_status: Annotated[
        Literal["queued", "running", "completed", "failed", "cancelled"] | None,
        Query(alias="status"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ExcelExportJobPage:
    return await ExcelExportJobService(
        session,
        get_minio_client(),
    ).list_page(
        actor_id=actor.id,
        status=job_status,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/excel/exportJobs/{jobId}",
    response_model=ExcelExportJobRead,
    response_model_by_alias=True,
    summary="Получить состояние XLSX-экспорта",
)
async def get_excel_export_job(
    job_id: Annotated[UUID, Path(alias="jobId")],
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ExcelExportJobRead:
    try:
        return await ExcelExportJobService(
            session,
            get_minio_client(),
        ).get(job_id, actor.id)
    except ExcelExportJobNotFound as error:
        raise HTTPException(status_code=404, detail="Задача экспорта не найдена") from error


@router.post(
    "/excel/exportJobs/{jobId}/command",
    response_model=ExcelExportJobRead,
    response_model_by_alias=True,
    summary="Отменить XLSX-экспорт",
)
async def command_excel_export_job(
    job_id: Annotated[UUID, Path(alias="jobId")],
    payload: ExcelExportJobCommand,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ExcelExportJobRead:
    try:
        return await ExcelExportJobService(
            session,
            get_minio_client(),
        ).cancel(job_id, actor.id)
    except ExcelExportJobNotFound as error:
        raise HTTPException(status_code=404, detail="Задача экспорта не найдена") from error


@router.get(
    "/excel/exportJobs/{jobId}/download",
    summary="Скачать готовую XLSX-выгрузку",
)
async def download_excel_export(
    job_id: Annotated[UUID, Path(alias="jobId")],
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    service = ExcelExportJobService(session, get_minio_client())
    try:
        entity_codes = await service.entity_codes(job_id, actor.id)
        for entity_code in entity_codes:
            await _authorize(session, actor, "export", entity_code)
        filename, content = await service.download(job_id, actor.id)
    except ExcelExportJobNotFound as error:
        raise HTTPException(status_code=404, detail="Задача экспорта не найдена") from error
    except ExcelExportNotReady as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
    if isinstance(error, RuntimeValidationError):
        return HTTPException(status_code=422, detail=error.issues)
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


async def _authorize_filter_dependencies(
    session: AsyncSession,
    actor,
    entity_code: str,
    filters: list[ObjectFilter],
) -> None:
    try:
        related_codes = await RuntimeObjectService(session).filter_dependency_entity_codes(
            entity_code,
            filters,
        )
        authorization = AuthorizationService(session)
        for related_code in related_codes:
            await authorization.require_entity_code(actor, "read", related_code)
    except AccessDenied as error:
        raise HTTPException(
            status_code=403,
            detail="Недостаточно прав для фильтрации по связанной сущности",
        ) from error
    except (AccessResourceNotFound, RuntimeEntityNotFound) as error:
        raise HTTPException(status_code=404, detail="Связанная сущность не найдена") from error
    finally:
        await session.rollback()
