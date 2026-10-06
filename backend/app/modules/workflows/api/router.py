from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.security import AdminActor, CurrentActor
from app.modules.workflows.api.schemas import (
    AssignmentAction,
    AssignmentCreate,
    AssignmentExecutionRead,
    AssignmentRead,
    ExecutionUpdate,
    FormDefinitionCreate,
    FormDefinitionRead,
    FormDefinitionUpdate,
    InformationRequestCreate,
    InformationRequestRead,
    InteragencyAction,
    InteragencyRequestCreate,
    InteragencyRequestRead,
    InteragencyResponseCreate,
    InteragencyResponseRead,
    SubmissionCreate,
    SubmissionRead,
    SubmissionReview,
    WorkflowAction,
)
from app.modules.workflows.application.service import (
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowService,
    WorkflowValidationError,
)
from app.modules.access.application.service import AccessDenied

router = APIRouter(tags=["Формы и процессы"])


@router.post("/forms", response_model=FormDefinitionRead, status_code=201, summary="Создать форму")
async def create_form(
    payload: FormDefinitionCreate,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FormDefinitionRead:
    return await _execute(WorkflowService(session).create_form(payload, actor))


@router.get("/forms", response_model=list[FormDefinitionRead], summary="Получить формы")
async def list_forms(
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    entity_schema_id: Annotated[UUID | None, Query(alias="entitySchemaId")] = None,
    include_archived: Annotated[bool, Query(alias="includeArchived")] = False,
) -> list[FormDefinitionRead]:
    return await WorkflowService(session).list_forms(
        entity_schema_id,
        include_archived,
        actor,
    )


@router.patch("/forms/{formId}", response_model=FormDefinitionRead, summary="Изменить форму")
async def update_form(
    form_id: Annotated[UUID, Path(alias="formId")],
    payload: FormDefinitionUpdate,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FormDefinitionRead:
    return await _execute(WorkflowService(session).update_form(form_id, payload, actor))


@router.post(
    "/forms/{formId}/publish",
    response_model=FormDefinitionRead,
    summary="Опубликовать форму",
)
async def publish_form(
    form_id: Annotated[UUID, Path(alias="formId")],
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FormDefinitionRead:
    return await _execute(WorkflowService(session).set_form_status(form_id, "published", actor))


@router.post(
    "/forms/{formId}/archive",
    response_model=FormDefinitionRead,
    summary="Архивировать форму",
)
async def archive_form(
    form_id: Annotated[UUID, Path(alias="formId")],
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FormDefinitionRead:
    return await _execute(WorkflowService(session).set_form_status(form_id, "archived", actor))


@router.post(
    "/forms/{formId}/restore",
    response_model=FormDefinitionRead,
    summary="Восстановить форму из архива",
)
async def restore_form(
    form_id: Annotated[UUID, Path(alias="formId")],
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FormDefinitionRead:
    return await _execute(WorkflowService(session).set_form_status(form_id, "draft", actor))


@router.post(
    "/informationRequests",
    response_model=InformationRequestRead,
    status_code=201,
    summary="Создать сбор или актуализацию данных",
)
async def create_information_request(
    payload: InformationRequestCreate,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> InformationRequestRead:
    return await _execute(WorkflowService(session).create_information_request(payload, actor))


@router.get(
    "/informationRequests",
    response_model=list[InformationRequestRead],
    summary="Получить сборы и актуализации",
)
async def list_information_requests(
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    request_type: Annotated[str | None, Query(alias="requestType")] = None,
    workflow_status: Annotated[str | None, Query(alias="status")] = None,
) -> list[InformationRequestRead]:
    return await WorkflowService(session).list_information_requests(
        request_type,
        workflow_status,
        actor,
    )


@router.get(
    "/informationRequests/{requestId}",
    response_model=InformationRequestRead,
    summary="Получить сбор или актуализацию",
)
async def get_information_request(
    request_id: Annotated[UUID, Path(alias="requestId")],
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> InformationRequestRead:
    return await _execute(
        WorkflowService(session).get_information_request(request_id, actor)
    )


@router.post(
    "/informationRequests/{requestId}/action",
    response_model=InformationRequestRead,
    summary="Изменить состояние сбора или актуализации",
)
async def act_on_information_request(
    request_id: Annotated[UUID, Path(alias="requestId")],
    payload: WorkflowAction,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> InformationRequestRead:
    return await _execute(
        WorkflowService(session).act_on_information_request(request_id, payload, actor)
    )


@router.post(
    "/requestRecipients/{recipientId}/submissions",
    response_model=SubmissionRead,
    status_code=201,
    summary="Сохранить или отправить ответ по форме",
)
async def save_submission(
    recipient_id: Annotated[UUID, Path(alias="recipientId")],
    payload: SubmissionCreate,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SubmissionRead:
    return await _execute(WorkflowService(session).save_submission(recipient_id, payload, actor))


@router.post(
    "/formSubmissions/{submissionId}/review",
    response_model=SubmissionRead,
    summary="Проверить ответ по форме",
)
async def review_submission(
    submission_id: Annotated[UUID, Path(alias="submissionId")],
    payload: SubmissionReview,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SubmissionRead:
    return await _execute(WorkflowService(session).review_submission(submission_id, payload, actor))


@router.post(
    "/assignments",
    response_model=AssignmentRead,
    status_code=201,
    summary="Создать поручение",
)
async def create_assignment(
    payload: AssignmentCreate,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AssignmentRead:
    return await _execute(WorkflowService(session).create_assignment(payload, actor))


@router.get("/assignments", response_model=list[AssignmentRead], summary="Получить поручения")
async def list_assignments(
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    workflow_status: Annotated[str | None, Query(alias="status")] = None,
) -> list[AssignmentRead]:
    return await WorkflowService(session).list_assignments(workflow_status, actor)


@router.get(
    "/assignments/{assignmentId}", response_model=AssignmentRead, summary="Получить поручение"
)
async def get_assignment(
    assignment_id: Annotated[UUID, Path(alias="assignmentId")],
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AssignmentRead:
    return await _execute(WorkflowService(session).get_assignment(assignment_id, actor))


@router.post(
    "/assignments/{assignmentId}/action",
    response_model=AssignmentRead,
    summary="Запустить или отменить поручение",
)
async def act_on_assignment(
    assignment_id: Annotated[UUID, Path(alias="assignmentId")],
    payload: AssignmentAction,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AssignmentRead:
    return await _execute(WorkflowService(session).act_on_assignment(assignment_id, payload, actor))


@router.post(
    "/assignmentExecutions/{executionId}/action",
    response_model=AssignmentExecutionRead,
    summary="Изменить состояние исполнения поручения",
)
async def update_assignment_execution(
    execution_id: Annotated[UUID, Path(alias="executionId")],
    payload: ExecutionUpdate,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AssignmentExecutionRead:
    return await _execute(WorkflowService(session).update_execution(execution_id, payload, actor))


@router.post(
    "/interagencyRequests",
    response_model=InteragencyRequestRead,
    status_code=201,
    summary="Создать межведомственный запрос",
)
async def create_interagency_request(
    payload: InteragencyRequestCreate,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> InteragencyRequestRead:
    return await _execute(WorkflowService(session).create_interagency_request(payload, actor))


@router.get(
    "/interagencyRequests",
    response_model=list[InteragencyRequestRead],
    summary="Получить межведомственные запросы",
)
async def list_interagency_requests(
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    workflow_status: Annotated[str | None, Query(alias="status")] = None,
) -> list[InteragencyRequestRead]:
    return await WorkflowService(session).list_interagency_requests(
        workflow_status,
        actor,
    )


@router.get(
    "/interagencyRequests/{requestId}",
    response_model=InteragencyRequestRead,
    summary="Получить межведомственный запрос",
)
async def get_interagency_request(
    request_id: Annotated[UUID, Path(alias="requestId")],
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> InteragencyRequestRead:
    return await _execute(
        WorkflowService(session).get_interagency_request(request_id, actor)
    )


@router.post(
    "/interagencyRequests/{requestId}/action",
    response_model=InteragencyRequestRead,
    summary="Изменить состояние межведомственного запроса",
)
async def act_on_interagency_request(
    request_id: Annotated[UUID, Path(alias="requestId")],
    payload: InteragencyAction,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> InteragencyRequestRead:
    return await _execute(
        WorkflowService(session).act_on_interagency_request(request_id, payload, actor)
    )


@router.post(
    "/interagencyRequests/{requestId}/responses",
    response_model=InteragencyResponseRead,
    status_code=201,
    summary="Добавить версию ответа на межведомственный запрос",
)
async def add_interagency_response(
    request_id: Annotated[UUID, Path(alias="requestId")],
    payload: InteragencyResponseCreate,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> InteragencyResponseRead:
    return await _execute(
        WorkflowService(session).add_interagency_response(request_id, payload, actor)
    )


async def _execute(awaitable):
    try:
        return await awaitable
    except WorkflowNotFound as error:
        raise HTTPException(status_code=404, detail="Ресурс процесса не найден") from error
    except WorkflowConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except WorkflowValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except AccessDenied as error:
        raise HTTPException(status_code=403, detail="Недостаточно предметных прав") from error
    except IntegrityError as error:
        raise HTTPException(status_code=409, detail="Нарушена уникальность или связь данных") from error
