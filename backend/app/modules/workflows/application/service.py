from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import ActorContext
from app.modules.access.application.service import AccessDenied, AuthorizationService
from app.modules.entities.infrastructure.models import (
    EntityFieldModel,
    EntitySchemaModel,
    EntitySchemaVersionModel,
)
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
    RequestRecipientRead,
    SubmissionCreate,
    SubmissionRead,
    SubmissionReview,
    WorkflowAction,
)
from app.shared.db.models import (
    AssignmentExecutionModel,
    AssignmentModel,
    AuditEventModel,
    EntityObjectModel,
    FormDefinitionModel,
    FormSubmissionModel,
    FormVersionModel,
    InformationRequestModel,
    InteragencyRequestModel,
    InteragencyResponseVersionModel,
    OrganizationModel,
    OutboxEventModel,
    RequestRecipientModel,
)


class WorkflowNotFound(Exception):
    """Предметный процесс или связанный ресурс не найден."""


class WorkflowConflict(Exception):
    """Операция конфликтует с текущим состоянием процесса."""


class WorkflowValidationError(Exception):
    """Данные процесса не прошли предметную проверку."""


class WorkflowService:
    """Управляет версионируемыми формами и предметными процессами."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_form(
        self, payload: FormDefinitionCreate, actor: ActorContext
    ) -> FormDefinitionRead:
        async with self._session.begin():
            schema = await self._entity(payload.entity_schema_id)
            await AuthorizationService(self._session).require(
                actor,
                "request",
                organization_id=payload.owner_organization_id or schema.owner_organization_id,
                entity_schema_id=schema.id,
            )
            await self._organization(payload.owner_organization_id)
            schema_version_id = await self._schema_version_id(schema.id)
            data = payload.model_dump()
            data["name"] = payload.name.strip()
            model = FormDefinitionModel(
                **data,
                status="draft",
                current_version=1,
                created_by=actor.id,
            )
            self._session.add(model)
            await self._session.flush()
            self._session.add(
                FormVersionModel(
                    form_id=model.id,
                    schema_version_id=schema_version_id,
                    version=1,
                    snapshot=self._form_snapshot(model),
                    created_by=actor.id,
                )
            )
            self._track(actor, "form", model.id, "created", None, self._form_snapshot(model))
        return self._form(model)

    async def list_forms(
        self,
        entity_schema_id: UUID | None,
        include_archived: bool,
        actor: ActorContext,
    ) -> list[FormDefinitionRead]:
        conditions = []
        if entity_schema_id is not None:
            conditions.append(FormDefinitionModel.entity_schema_id == entity_schema_id)
        if not include_archived:
            conditions.append(FormDefinitionModel.status != "archived")
        rows = (
            await self._session.scalars(
                select(FormDefinitionModel)
                .where(*conditions)
                .order_by(FormDefinitionModel.updated_at.desc())
            )
        ).all()
        result = []
        for row in rows:
            if await self._allowed(
                actor,
                "request",
                organization_id=row.owner_organization_id,
                entity_schema_id=row.entity_schema_id,
            ):
                result.append(self._form(row))
        return result

    async def update_form(
        self, form_id: UUID, payload: FormDefinitionUpdate, actor: ActorContext
    ) -> FormDefinitionRead:
        async with self._session.begin():
            model = await self._form_model(form_id, for_update=True)
            if model.status == "archived":
                raise WorkflowConflict("Архивную форму сначала нужно восстановить")
            await AuthorizationService(self._session).require(
                actor,
                "request",
                organization_id=model.owner_organization_id,
                entity_schema_id=model.entity_schema_id,
            )
            before = self._form_snapshot(model)
            await self._organization(payload.owner_organization_id)
            for field in payload.model_fields_set:
                value = getattr(payload, field)
                if field == "name" and value is not None:
                    value = value.strip()
                setattr(model, field, value)
            if "definition" in payload.model_fields_set:
                model.current_version += 1
                self._session.add(
                    FormVersionModel(
                        form_id=model.id,
                        schema_version_id=await self._schema_version_id(model.entity_schema_id),
                        version=model.current_version,
                        snapshot=self._form_snapshot(model),
                        created_by=actor.id,
                    )
                )
            await self._session.flush()
            await self._session.refresh(model)
            self._track(actor, "form", model.id, "updated", before, self._form_snapshot(model))
        return self._form(model)

    async def set_form_status(
        self, form_id: UUID, status: str, actor: ActorContext
    ) -> FormDefinitionRead:
        if status not in {"published", "archived", "draft"}:
            raise WorkflowValidationError("Недопустимый статус формы")
        async with self._session.begin():
            model = await self._form_model(form_id, for_update=True)
            await AuthorizationService(self._session).require(
                actor,
                "request",
                organization_id=model.owner_organization_id,
                entity_schema_id=model.entity_schema_id,
            )
            before = self._form_snapshot(model)
            model.status = status
            await self._session.flush()
            await self._session.refresh(model)
            self._track(actor, "form", model.id, f"status.{status}", before, self._form_snapshot(model))
        return self._form(model)

    async def create_information_request(
        self, payload: InformationRequestCreate, actor: ActorContext
    ) -> InformationRequestRead:
        async with self._session.begin():
            schema = await self._entity(payload.entity_schema_id)
            form = await self._form_model(payload.form_id)
            if form.entity_schema_id != schema.id or form.status != "published":
                raise WorkflowValidationError("Нужна опубликованная форма выбранной сущности")
            await self._organization(payload.owner_organization_id)
            await AuthorizationService(self._session).require(
                actor,
                "request",
                organization_id=payload.owner_organization_id or schema.owner_organization_id,
                entity_schema_id=schema.id,
            )
            await self._validate_field_codes(schema.id, payload.selected_fields)
            form_version = await self._current_form_version(form)
            data = payload.model_dump(exclude={"recipients"})
            data["name"] = payload.name.strip()
            model = InformationRequestModel(
                **data,
                form_version_id=form_version.id,
                status="draft",
                created_by=actor.id,
            )
            self._session.add(model)
            await self._session.flush()
            for recipient in payload.recipients:
                await self._organization(recipient.organization_id, required=True)
                await self._validate_object_ids(schema.id, recipient.object_ids)
                await self._validate_field_codes(schema.id, recipient.field_codes)
                self._session.add(
                    RequestRecipientModel(
                        request_id=model.id,
                        organization_id=recipient.organization_id,
                        object_ids=[str(value) for value in recipient.object_ids],
                        field_codes=recipient.field_codes,
                        contact_email=recipient.contact_email,
                    )
                )
            await self._session.flush()
            self._track(
                actor,
                "information_request",
                model.id,
                "created",
                None,
                {"name": model.name, "type": model.request_type, "status": model.status},
            )
        return await self.get_information_request(model.id, actor)

    async def list_information_requests(
        self,
        request_type: str | None,
        status: str | None,
        actor: ActorContext,
    ) -> list[InformationRequestRead]:
        conditions = []
        if request_type is not None:
            conditions.append(InformationRequestModel.request_type == request_type)
        if status is not None:
            conditions.append(InformationRequestModel.status == status)
        rows = (
            await self._session.scalars(
                select(InformationRequestModel)
                .where(*conditions)
                .order_by(InformationRequestModel.created_at.desc())
            )
        ).all()
        result = []
        for row in rows:
            if await self._can_access_information_request(row, actor):
                result.append(await self._information_request(row))
        return result

    async def get_information_request(
        self,
        request_id: UUID,
        actor: ActorContext,
    ) -> InformationRequestRead:
        model = await self._session.get(InformationRequestModel, request_id)
        if model is None or not await self._can_access_information_request(model, actor):
            raise WorkflowNotFound
        return await self._information_request(model)

    async def act_on_information_request(
        self, request_id: UUID, payload: WorkflowAction, actor: ActorContext
    ) -> InformationRequestRead:
        transitions = {
            "open": ({"draft"}, "open"),
            "close": ({"open"}, "closed"),
            "cancel": ({"draft", "open"}, "cancelled"),
        }
        allowed, target = transitions[payload.action]
        async with self._session.begin():
            model = await self._information_request_model(request_id, for_update=True)
            await AuthorizationService(self._session).require(
                actor,
                "request",
                organization_id=model.owner_organization_id,
                entity_schema_id=model.entity_schema_id,
            )
            if model.status not in allowed:
                raise WorkflowConflict("Действие недоступно в текущем статусе")
            old_status = model.status
            model.status = target
            self._track(
                actor,
                "information_request",
                model.id,
                payload.action,
                {"status": old_status},
                {"status": target},
            )
        return await self.get_information_request(request_id, actor)

    async def save_submission(
        self, recipient_id: UUID, payload: SubmissionCreate, actor: ActorContext
    ) -> SubmissionRead:
        async with self._session.begin():
            recipient = await self._recipient_model(recipient_id, for_update=True)
            request = await self._information_request_model(recipient.request_id)
            await AuthorizationService(self._session).require(
                actor,
                "request",
                organization_id=recipient.organization_id,
                entity_schema_id=request.entity_schema_id,
            )
            if request.status != "open":
                raise WorkflowConflict("Сбор данных не открыт")
            if recipient.status == "accepted":
                raise WorkflowConflict("Принятый ответ нельзя заменить без возврата на доработку")
            latest = await self._session.scalar(
                select(func.max(FormSubmissionModel.version)).where(
                    FormSubmissionModel.recipient_id == recipient.id
                )
            )
            version = int(latest or 0) + 1
            await self._replace_previous_submissions(recipient.id)
            submission = FormSubmissionModel(
                recipient_id=recipient.id,
                form_version_id=request.form_version_id,
                version=version,
                status="submitted" if payload.submit else "draft",
                payload=payload.payload,
                source_snapshot=await self._source_snapshot(recipient),
                submitted_by=actor.id if payload.submit else None,
                submitted_at=datetime.now(UTC) if payload.submit else None,
            )
            self._session.add(submission)
            await self._session.flush()
            recipient.last_submission_id = submission.id
            recipient.status = "review" if payload.submit else "draft"
            self._track(
                actor,
                "form_submission",
                submission.id,
                "submitted" if payload.submit else "draft.saved",
                None,
                {"recipientId": str(recipient.id), "version": version},
            )
        return self._submission(submission)

    async def review_submission(
        self, submission_id: UUID, payload: SubmissionReview, actor: ActorContext
    ) -> SubmissionRead:
        async with self._session.begin():
            model = await self._submission_model(submission_id, for_update=True)
            if model.status != "submitted":
                raise WorkflowConflict("На проверку можно принять только отправленный ответ")
            recipient = await self._recipient_model(model.recipient_id, for_update=True)
            request = await self._information_request_model(recipient.request_id)
            if request.reviewer_id != actor.id:
                await AuthorizationService(self._session).require(
                    actor,
                    "review",
                    organization_id=request.owner_organization_id,
                    entity_schema_id=request.entity_schema_id,
                )
            target = "accepted" if payload.action == "accept" else "rework"
            model.status = target
            model.reviewed_by = actor.id
            model.reviewed_at = datetime.now(UTC)
            model.review_comment = payload.comment
            recipient.status = target
            await self._session.flush()
            await self._session.refresh(model)
            self._track(
                actor,
                "form_submission",
                model.id,
                payload.action,
                {"status": "submitted"},
                {"status": target, "comment": payload.comment},
            )
        return self._submission(model)

    async def create_assignment(
        self, payload: AssignmentCreate, actor: ActorContext
    ) -> AssignmentRead:
        async with self._session.begin():
            await self._organization(payload.owner_organization_id)
            await AuthorizationService(self._session).require(
                actor,
                "request",
                organization_id=payload.owner_organization_id,
            )
            if payload.related_request_id is not None:
                await self._information_request_model(payload.related_request_id)
            data = payload.model_dump(exclude={"executions"})
            data["title"] = payload.title.strip()
            model = AssignmentModel(
                **data,
                created_by=actor.id,
                status="draft",
            )
            self._session.add(model)
            await self._session.flush()
            for execution in payload.executions:
                await self._organization(execution.assignee_organization_id)
                self._session.add(
                    AssignmentExecutionModel(
                        assignment_id=model.id,
                        assignee_id=execution.assignee_id,
                        assignee_organization_id=execution.assignee_organization_id,
                        object_ids=[str(value) for value in execution.object_ids],
                    )
                )
            await self._session.flush()
            self._track(actor, "assignment", model.id, "created", None, {"title": model.title})
        return await self.get_assignment(model.id, actor)

    async def list_assignments(
        self,
        status: str | None,
        actor: ActorContext,
    ) -> list[AssignmentRead]:
        conditions = [] if status is None else [AssignmentModel.status == status]
        rows = (
            await self._session.scalars(
                select(AssignmentModel)
                .where(*conditions)
                .order_by(AssignmentModel.created_at.desc())
            )
        ).all()
        result = []
        for row in rows:
            if await self._can_access_assignment(row, actor):
                result.append(await self._assignment(row))
        return result

    async def get_assignment(
        self,
        assignment_id: UUID,
        actor: ActorContext,
    ) -> AssignmentRead:
        model = await self._session.get(AssignmentModel, assignment_id)
        if model is None or not await self._can_access_assignment(model, actor):
            raise WorkflowNotFound
        return await self._assignment(model)

    async def act_on_assignment(
        self, assignment_id: UUID, payload: AssignmentAction, actor: ActorContext
    ) -> AssignmentRead:
        async with self._session.begin():
            model = await self._assignment_model(assignment_id, for_update=True)
            await AuthorizationService(self._session).require(
                actor,
                "request",
                organization_id=model.owner_organization_id,
            )
            if payload.action == "start":
                if model.status != "draft":
                    raise WorkflowConflict("Запустить можно только черновик поручения")
                model.status = "active"
            else:
                if model.status not in {"draft", "active"}:
                    raise WorkflowConflict("Поручение уже завершено")
                model.status = "cancelled"
            self._track(actor, "assignment", model.id, payload.action, None, {"status": model.status})
        return await self.get_assignment(assignment_id, actor)

    async def update_execution(
        self, execution_id: UUID, payload: ExecutionUpdate, actor: ActorContext
    ) -> AssignmentExecutionRead:
        transitions = {
            "start": ({"not_started", "rework"}, "in_progress"),
            "submit": ({"in_progress", "rework"}, "review"),
            "accept": ({"review"}, "completed"),
            "rework": ({"review"}, "rework"),
            "cancel": ({"not_started", "in_progress", "rework"}, "cancelled"),
        }
        allowed, target = transitions[payload.action]
        async with self._session.begin():
            model = await self._execution_model(execution_id, for_update=True)
            assignment = await self._assignment_model(model.assignment_id, for_update=True)
            if not (
                model.assignee_id == actor.id
                and payload.action in {"start", "submit", "cancel"}
            ):
                await AuthorizationService(self._session).require(
                    actor,
                    "review" if payload.action in {"accept", "rework"} else "update",
                    organization_id=(
                        assignment.owner_organization_id
                        if payload.action in {"accept", "rework"}
                        else model.assignee_organization_id
                    ),
                )
            if assignment.status != "active":
                raise WorkflowConflict("Поручение не активно")
            if model.status not in allowed:
                raise WorkflowConflict("Действие недоступно в текущем статусе исполнения")
            model.status = target
            if payload.result is not None:
                model.result = payload.result
            if payload.change_set_id is not None:
                model.change_set_id = payload.change_set_id
            model.review_comment = payload.comment
            now = datetime.now(UTC)
            if payload.action == "submit":
                model.submitted_at = now
            if payload.action == "accept":
                model.completed_at = now
                model.completion_late = assignment.due_at is not None and now > assignment.due_at
            await self._session.flush()
            remaining = int(
                await self._session.scalar(
                    select(func.count())
                    .select_from(AssignmentExecutionModel)
                    .where(
                        AssignmentExecutionModel.assignment_id == assignment.id,
                        AssignmentExecutionModel.status.not_in({"completed", "cancelled"}),
                    )
                )
                or 0
            )
            if remaining == 0:
                assignment.status = "completed"
            await self._session.flush()
            await self._session.refresh(model)
            self._track(
                actor,
                "assignment_execution",
                model.id,
                payload.action,
                None,
                {"status": model.status},
            )
        return self._execution(model)

    async def create_interagency_request(
        self, payload: InteragencyRequestCreate, actor: ActorContext
    ) -> InteragencyRequestRead:
        async with self._session.begin():
            await self._organization(payload.sender_organization_id, required=True)
            await self._organization(payload.recipient_organization_id, required=True)
            await AuthorizationService(self._session).require(
                actor,
                "request",
                organization_id=payload.sender_organization_id,
            )
            if payload.form_id is not None:
                form = await self._form_model(payload.form_id)
                if form.status != "published":
                    raise WorkflowValidationError("Форма межведомственного запроса не опубликована")
            model = InteragencyRequestModel(
                sender_organization_id=payload.sender_organization_id,
                recipient_organization_id=payload.recipient_organization_id,
                purpose=payload.purpose.strip(),
                object_ids=[str(value) for value in payload.object_ids],
                field_codes=payload.field_codes,
                form_id=payload.form_id,
                due_at=payload.due_at,
                expected_format=payload.expected_format,
                status="draft",
                created_by=actor.id,
            )
            self._session.add(model)
            await self._session.flush()
            self._track(
                actor,
                "interagency_request",
                model.id,
                "created",
                None,
                {"purpose": model.purpose, "status": model.status},
            )
        return await self.get_interagency_request(model.id, actor)

    async def list_interagency_requests(
        self,
        status: str | None,
        actor: ActorContext,
    ) -> list[InteragencyRequestRead]:
        conditions = [] if status is None else [InteragencyRequestModel.status == status]
        rows = (
            await self._session.scalars(
                select(InteragencyRequestModel)
                .where(*conditions)
                .order_by(InteragencyRequestModel.created_at.desc())
            )
        ).all()
        result = []
        for row in rows:
            if await self._can_access_interagency_request(row, actor):
                result.append(await self._interagency_request(row))
        return result

    async def get_interagency_request(
        self,
        request_id: UUID,
        actor: ActorContext,
    ) -> InteragencyRequestRead:
        model = await self._session.get(InteragencyRequestModel, request_id)
        if model is None or not await self._can_access_interagency_request(model, actor):
            raise WorkflowNotFound
        return await self._interagency_request(model)

    async def act_on_interagency_request(
        self, request_id: UUID, payload: InteragencyAction, actor: ActorContext
    ) -> InteragencyRequestRead:
        transitions = {
            "send": ({"draft"}, "sent"),
            "start": ({"sent", "clarification"}, "in_progress"),
            "requestClarification": ({"response_received"}, "clarification"),
            "complete": ({"response_received"}, "completed"),
            "cancel": ({"draft", "sent", "in_progress", "clarification"}, "cancelled"),
        }
        allowed, target = transitions[payload.action]
        async with self._session.begin():
            model = await self._interagency_model(request_id, for_update=True)
            await AuthorizationService(self._session).require(
                actor,
                "review" if payload.action in {"complete", "requestClarification"} else "request",
                organization_id=(
                    model.sender_organization_id
                    if payload.action in {"complete", "requestClarification", "cancel"}
                    else model.recipient_organization_id
                ),
            )
            if model.status not in allowed:
                raise WorkflowConflict("Действие недоступно в текущем статусе запроса")
            before = model.status
            model.status = target
            if payload.action == "complete":
                latest = await self._latest_interagency_response(model.id, for_update=True)
                if latest is None:
                    raise WorkflowConflict("Нельзя завершить запрос без ответа")
                latest.accepted_by = actor.id
                latest.accepted_at = datetime.now(UTC)
            self._track(
                actor,
                "interagency_request",
                model.id,
                payload.action,
                {"status": before},
                {"status": target},
            )
        return await self.get_interagency_request(request_id, actor)

    async def add_interagency_response(
        self, request_id: UUID, payload: InteragencyResponseCreate, actor: ActorContext
    ) -> InteragencyResponseRead:
        async with self._session.begin():
            request = await self._interagency_model(request_id, for_update=True)
            await AuthorizationService(self._session).require(
                actor,
                "update",
                organization_id=request.recipient_organization_id,
            )
            if request.status not in {"sent", "in_progress", "clarification"}:
                raise WorkflowConflict("Ответ нельзя добавить в текущем статусе запроса")
            version = int(
                await self._session.scalar(
                    select(func.max(InteragencyResponseVersionModel.version)).where(
                        InteragencyResponseVersionModel.request_id == request.id
                    )
                )
                or 0
            ) + 1
            response = InteragencyResponseVersionModel(
                request_id=request.id,
                version=version,
                payload=payload.payload,
                change_set_id=payload.change_set_id,
                submitted_by=actor.id,
            )
            self._session.add(response)
            request.status = "response_received"
            await self._session.flush()
            self._track(
                actor,
                "interagency_response",
                response.id,
                "submitted",
                None,
                {"requestId": str(request.id), "version": version},
            )
        return self._interagency_response(response)

    async def _can_access_information_request(
        self,
        model: InformationRequestModel,
        actor: ActorContext,
    ) -> bool:
        if model.reviewer_id == actor.id:
            return True
        if await self._allowed(
            actor,
            "request",
            organization_id=model.owner_organization_id,
            entity_schema_id=model.entity_schema_id,
        ):
            return True
        recipient_organizations = list(
            await self._session.scalars(
                select(RequestRecipientModel.organization_id).where(
                    RequestRecipientModel.request_id == model.id
                )
            )
        )
        for organization_id in recipient_organizations:
            if await self._allowed(
                actor,
                "request",
                organization_id=organization_id,
                entity_schema_id=model.entity_schema_id,
            ):
                return True
        return False

    async def _can_access_assignment(
        self,
        model: AssignmentModel,
        actor: ActorContext,
    ) -> bool:
        if model.reviewer_id == actor.id or await self._allowed(
            actor,
            "request",
            organization_id=model.owner_organization_id,
        ):
            return True
        executions = list(
            await self._session.scalars(
                select(AssignmentExecutionModel).where(
                    AssignmentExecutionModel.assignment_id == model.id
                )
            )
        )
        for execution in executions:
            if execution.assignee_id == actor.id:
                return True
            if await self._allowed(
                actor,
                "update",
                organization_id=execution.assignee_organization_id,
            ):
                return True
        return False

    async def _can_access_interagency_request(
        self,
        model: InteragencyRequestModel,
        actor: ActorContext,
    ) -> bool:
        return await self._allowed(
            actor,
            "request",
            organization_id=model.sender_organization_id,
        ) or await self._allowed(
            actor,
            "update",
            organization_id=model.recipient_organization_id,
        )

    async def _allowed(
        self,
        actor: ActorContext,
        action: str,
        *,
        organization_id: UUID | None,
        entity_schema_id: UUID | None = None,
    ) -> bool:
        try:
            await AuthorizationService(self._session).require(
                actor,
                action,
                organization_id=organization_id,
                entity_schema_id=entity_schema_id,
            )
            return True
        except AccessDenied:
            return False

    async def _entity(self, entity_id: UUID) -> EntitySchemaModel:
        model = await self._session.get(EntitySchemaModel, entity_id)
        if model is None:
            raise WorkflowNotFound
        return model

    async def _schema_version_id(self, entity_id: UUID) -> UUID:
        value = await self._session.scalar(
            select(EntitySchemaVersionModel.id)
            .where(EntitySchemaVersionModel.entity_schema_id == entity_id)
            .order_by(EntitySchemaVersionModel.version.desc())
            .limit(1)
        )
        if value is None:
            raise WorkflowValidationError("У сущности отсутствует версия схемы")
        return value

    async def _organization(self, organization_id: UUID | None, *, required: bool = False) -> None:
        if organization_id is None:
            if required:
                raise WorkflowValidationError("Организация обязательна")
            return
        exists = await self._session.scalar(
            select(OrganizationModel.id).where(
                OrganizationModel.id == organization_id,
                OrganizationModel.active.is_(True),
            )
        )
        if exists is None:
            raise WorkflowValidationError("Активная организация не найдена")

    async def _validate_field_codes(self, entity_id: UUID, codes: list[str]) -> None:
        if not codes:
            return
        existing = set(
            await self._session.scalars(
                select(EntityFieldModel.code).where(
                    EntityFieldModel.entity_schema_id == entity_id,
                    EntityFieldModel.code.in_(codes),
                    EntityFieldModel.archived.is_(False),
                )
            )
        )
        missing = sorted(set(codes) - existing)
        if missing:
            raise WorkflowValidationError(
                f"В сущности отсутствуют активные поля: {', '.join(missing)}"
            )

    async def _validate_object_ids(self, entity_id: UUID, object_ids: list[UUID]) -> None:
        if not object_ids:
            return
        count = int(
            await self._session.scalar(
                select(func.count())
                .select_from(EntityObjectModel)
                .where(
                    EntityObjectModel.entity_schema_id == entity_id,
                    EntityObjectModel.id.in_(object_ids),
                )
            )
            or 0
        )
        if count != len(set(object_ids)):
            raise WorkflowValidationError("Часть выбранных объектов не относится к сущности")

    async def _form_model(self, form_id: UUID, *, for_update: bool = False) -> FormDefinitionModel:
        statement = select(FormDefinitionModel).where(FormDefinitionModel.id == form_id)
        if for_update:
            statement = statement.with_for_update()
        model = await self._session.scalar(statement)
        if model is None:
            raise WorkflowNotFound
        return model

    async def _current_form_version(self, form: FormDefinitionModel) -> FormVersionModel:
        model = await self._session.scalar(
            select(FormVersionModel).where(
                FormVersionModel.form_id == form.id,
                FormVersionModel.version == form.current_version,
            )
        )
        if model is None:
            raise WorkflowValidationError("Текущая версия формы отсутствует")
        return model

    async def _information_request_model(
        self, request_id: UUID, *, for_update: bool = False
    ) -> InformationRequestModel:
        statement = select(InformationRequestModel).where(InformationRequestModel.id == request_id)
        if for_update:
            statement = statement.with_for_update()
        model = await self._session.scalar(statement)
        if model is None:
            raise WorkflowNotFound
        return model

    async def _recipient_model(
        self, recipient_id: UUID, *, for_update: bool = False
    ) -> RequestRecipientModel:
        statement = select(RequestRecipientModel).where(RequestRecipientModel.id == recipient_id)
        if for_update:
            statement = statement.with_for_update()
        model = await self._session.scalar(statement)
        if model is None:
            raise WorkflowNotFound
        return model

    async def _submission_model(
        self, submission_id: UUID, *, for_update: bool = False
    ) -> FormSubmissionModel:
        statement = select(FormSubmissionModel).where(FormSubmissionModel.id == submission_id)
        if for_update:
            statement = statement.with_for_update()
        model = await self._session.scalar(statement)
        if model is None:
            raise WorkflowNotFound
        return model

    async def _replace_previous_submissions(self, recipient_id: UUID) -> None:
        previous = list(
            await self._session.scalars(
                select(FormSubmissionModel)
                .where(
                    FormSubmissionModel.recipient_id == recipient_id,
                    FormSubmissionModel.status.in_({"draft", "submitted", "rework"}),
                )
                .with_for_update()
            )
        )
        for model in previous:
            model.status = "replaced"

    async def _source_snapshot(self, recipient: RequestRecipientModel) -> dict[str, Any]:
        if not recipient.object_ids:
            return {}
        object_ids = [UUID(item) if isinstance(item, str) else item for item in recipient.object_ids]
        rows = (
            await self._session.scalars(
                select(EntityObjectModel).where(EntityObjectModel.id.in_(object_ids))
            )
        ).all()
        return {
            str(row.id): {"revision": row.revision, "values": row.values}
            for row in rows
        }

    async def _assignment_model(
        self, assignment_id: UUID, *, for_update: bool = False
    ) -> AssignmentModel:
        statement = select(AssignmentModel).where(AssignmentModel.id == assignment_id)
        if for_update:
            statement = statement.with_for_update()
        model = await self._session.scalar(statement)
        if model is None:
            raise WorkflowNotFound
        return model

    async def _execution_model(
        self, execution_id: UUID, *, for_update: bool = False
    ) -> AssignmentExecutionModel:
        statement = select(AssignmentExecutionModel).where(AssignmentExecutionModel.id == execution_id)
        if for_update:
            statement = statement.with_for_update()
        model = await self._session.scalar(statement)
        if model is None:
            raise WorkflowNotFound
        return model

    async def _interagency_model(
        self, request_id: UUID, *, for_update: bool = False
    ) -> InteragencyRequestModel:
        statement = select(InteragencyRequestModel).where(InteragencyRequestModel.id == request_id)
        if for_update:
            statement = statement.with_for_update()
        model = await self._session.scalar(statement)
        if model is None:
            raise WorkflowNotFound
        return model

    async def _latest_interagency_response(
        self, request_id: UUID, *, for_update: bool = False
    ) -> InteragencyResponseVersionModel | None:
        statement = (
            select(InteragencyResponseVersionModel)
            .where(InteragencyResponseVersionModel.request_id == request_id)
            .order_by(InteragencyResponseVersionModel.version.desc())
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def _information_request(self, model: InformationRequestModel) -> InformationRequestRead:
        recipients = (
            await self._session.scalars(
                select(RequestRecipientModel)
                .where(RequestRecipientModel.request_id == model.id)
                .order_by(RequestRecipientModel.created_at)
            )
        ).all()
        return InformationRequestRead(
            id=model.id,
            request_type=model.request_type,
            entity_schema_id=model.entity_schema_id,
            form_id=model.form_id,
            form_version_id=model.form_version_id,
            owner_organization_id=model.owner_organization_id,
            name=model.name,
            description=model.description,
            period_start=model.period_start,
            period_end=model.period_end,
            due_at=model.due_at,
            reviewer_id=model.reviewer_id,
            selected_fields=model.selected_fields,
            correction_rules=model.correction_rules,
            status=model.status,
            created_by=model.created_by,
            recipients=[self._recipient(row) for row in recipients],
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    async def _assignment(self, model: AssignmentModel) -> AssignmentRead:
        executions = (
            await self._session.scalars(
                select(AssignmentExecutionModel)
                .where(AssignmentExecutionModel.assignment_id == model.id)
                .order_by(AssignmentExecutionModel.created_at)
            )
        ).all()
        return AssignmentRead(
            id=model.id,
            title=model.title,
            description=model.description,
            owner_organization_id=model.owner_organization_id,
            created_by=model.created_by,
            reviewer_id=model.reviewer_id,
            due_at=model.due_at,
            priority=model.priority,
            expected_result=model.expected_result,
            related_request_id=model.related_request_id,
            status=model.status,
            executions=[self._execution(row) for row in executions],
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    async def _interagency_request(self, model: InteragencyRequestModel) -> InteragencyRequestRead:
        responses = (
            await self._session.scalars(
                select(InteragencyResponseVersionModel)
                .where(InteragencyResponseVersionModel.request_id == model.id)
                .order_by(InteragencyResponseVersionModel.version)
            )
        ).all()
        return InteragencyRequestRead(
            id=model.id,
            sender_organization_id=model.sender_organization_id,
            recipient_organization_id=model.recipient_organization_id,
            purpose=model.purpose,
            object_ids=model.object_ids,
            field_codes=model.field_codes,
            form_id=model.form_id,
            due_at=model.due_at,
            expected_format=model.expected_format,
            status=model.status,
            created_by=model.created_by,
            responses=[self._interagency_response(row) for row in responses],
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    @staticmethod
    def _form(model: FormDefinitionModel) -> FormDefinitionRead:
        return FormDefinitionRead.model_validate(model, from_attributes=True)

    @staticmethod
    def _recipient(model: RequestRecipientModel) -> RequestRecipientRead:
        return RequestRecipientRead.model_validate(model, from_attributes=True)

    @staticmethod
    def _submission(model: FormSubmissionModel) -> SubmissionRead:
        return SubmissionRead.model_validate(model, from_attributes=True)

    @staticmethod
    def _execution(model: AssignmentExecutionModel) -> AssignmentExecutionRead:
        return AssignmentExecutionRead.model_validate(model, from_attributes=True)

    @staticmethod
    def _interagency_response(model: InteragencyResponseVersionModel) -> InteragencyResponseRead:
        return InteragencyResponseRead.model_validate(model, from_attributes=True)

    @staticmethod
    def _form_snapshot(model: FormDefinitionModel) -> dict[str, Any]:
        return {
            "name": model.name,
            "description": model.description,
            "entitySchemaId": str(model.entity_schema_id),
            "ownerOrganizationId": (
                str(model.owner_organization_id) if model.owner_organization_id else None
            ),
            "definition": model.definition,
            "version": model.current_version,
        }

    def _track(
        self,
        actor: ActorContext,
        resource_type: str,
        resource_id: UUID,
        action: str,
        old_value: dict[str, Any] | None,
        new_value: dict[str, Any] | None,
    ) -> None:
        changes = []
        for key in sorted(set(old_value or {}) | set(new_value or {})):
            old = (old_value or {}).get(key)
            new = (new_value or {}).get(key)
            if old != new:
                changes.append({"path": key, "oldValue": old, "newValue": new})
        self._session.add(
            AuditEventModel(
                resource_type=resource_type,
                resource_id=resource_id,
                action=action,
                actor_id=actor.id,
                actor_full_name=actor.display_name or actor.username or actor.email,
                actor_email=actor.email,
                old_value=old_value,
                new_value=new_value,
                changes=changes,
                metadata_json={},
            )
        )
        self._session.add(
            OutboxEventModel(
                aggregate_type=resource_type,
                aggregate_id=resource_id,
                event_type=f"{resource_type}.{action}",
                payload={
                    "resourceId": str(resource_id),
                    "actorId": str(actor.id),
                    "occurredAt": datetime.now(UTC).isoformat(),
                },
            )
        )


def confirmation_hash(value: Any) -> str:
    """Вернуть стабильный хеш подтверждённого значения поля."""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()
