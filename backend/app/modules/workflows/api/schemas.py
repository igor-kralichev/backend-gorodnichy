from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.modules.entities.api.schemas import ApiModel


FormStatus = Literal["draft", "published", "archived"]
RequestType = Literal["collection", "actualization"]
RequestStatus = Literal["draft", "open", "closed", "cancelled"]
RecipientStatus = Literal["not_started", "draft", "review", "rework", "accepted"]


class FormDefinitionCreate(ApiModel):
    entity_schema_id: UUID
    owner_organization_id: UUID | None = None
    name: str = Field(min_length=1, max_length=500)
    description: str | None = None
    definition: dict[str, Any] = Field(default_factory=dict)


class FormDefinitionUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None
    owner_organization_id: UUID | None = None
    definition: dict[str, Any] | None = None


class FormDefinitionRead(ApiModel):
    id: UUID
    entity_schema_id: UUID
    owner_organization_id: UUID | None
    name: str
    description: str | None
    status: FormStatus
    current_version: int
    definition: dict[str, Any]
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime


class RequestRecipientCreate(ApiModel):
    organization_id: UUID
    object_ids: list[UUID] = Field(default_factory=list, max_length=50_000)
    field_codes: list[str] = Field(default_factory=list, max_length=1_000)
    contact_email: str | None = Field(default=None, max_length=320)


class InformationRequestCreate(ApiModel):
    request_type: RequestType
    entity_schema_id: UUID
    form_id: UUID
    owner_organization_id: UUID | None = None
    name: str = Field(min_length=1, max_length=500)
    description: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    due_at: datetime | None = None
    reviewer_id: UUID | None = None
    selected_fields: list[str] = Field(default_factory=list, max_length=1_000)
    correction_rules: dict[str, Any] = Field(default_factory=dict)
    recipients: list[RequestRecipientCreate] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def validate_period_and_recipients(self) -> "InformationRequestCreate":
        if self.period_start and self.period_end and self.period_start > self.period_end:
            raise ValueError("Дата начала периода не может быть позже даты окончания")
        organization_ids = [item.organization_id for item in self.recipients]
        if len(organization_ids) != len(set(organization_ids)):
            raise ValueError("Организация не должна повторяться в списке получателей")
        return self


class RequestRecipientRead(ApiModel):
    id: UUID
    organization_id: UUID
    object_ids: list[UUID]
    field_codes: list[str]
    contact_email: str | None
    status: RecipientStatus
    reopened_until: datetime | None
    reopen_reason: str | None
    last_submission_id: UUID | None
    created_at: datetime
    updated_at: datetime


class InformationRequestRead(ApiModel):
    id: UUID
    request_type: RequestType
    entity_schema_id: UUID
    form_id: UUID
    form_version_id: UUID | None
    owner_organization_id: UUID | None
    name: str
    description: str | None
    period_start: date | None
    period_end: date | None
    due_at: datetime | None
    reviewer_id: UUID | None
    selected_fields: list[str]
    correction_rules: dict[str, Any]
    status: RequestStatus
    created_by: UUID | None
    recipients: list[RequestRecipientRead]
    created_at: datetime
    updated_at: datetime


class WorkflowAction(ApiModel):
    action: Literal["open", "close", "cancel"]


class SubmissionCreate(ApiModel):
    payload: dict[str, Any] = Field(default_factory=dict)
    submit: bool = False


class SubmissionReview(ApiModel):
    action: Literal["accept", "rework"]
    comment: str | None = Field(default=None, max_length=4_000)


class SubmissionRead(ApiModel):
    id: UUID
    recipient_id: UUID
    form_version_id: UUID
    version: int
    status: str
    payload: dict[str, Any]
    source_snapshot: dict[str, Any]
    change_set_id: UUID | None
    submitted_by: UUID | None
    submitted_at: datetime | None
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    review_comment: str | None
    created_at: datetime
    updated_at: datetime


class AssignmentExecutionCreate(ApiModel):
    assignee_id: UUID | None = None
    assignee_organization_id: UUID | None = None
    object_ids: list[UUID] = Field(default_factory=list, max_length=50_000)

    @model_validator(mode="after")
    def require_assignee(self) -> "AssignmentExecutionCreate":
        if self.assignee_id is None and self.assignee_organization_id is None:
            raise ValueError("Нужно указать исполнителя или организацию-исполнителя")
        return self


class AssignmentCreate(ApiModel):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    owner_organization_id: UUID | None = None
    reviewer_id: UUID | None = None
    due_at: datetime | None = None
    priority: Literal["low", "normal", "high", "critical"] = "normal"
    expected_result: dict[str, Any] = Field(default_factory=dict)
    related_request_id: UUID | None = None
    executions: list[AssignmentExecutionCreate] = Field(min_length=1, max_length=10_000)


class AssignmentExecutionRead(ApiModel):
    id: UUID
    assignee_id: UUID | None
    assignee_organization_id: UUID | None
    object_ids: list[UUID]
    status: str
    result: dict[str, Any]
    change_set_id: UUID | None
    submitted_at: datetime | None
    completed_at: datetime | None
    completion_late: bool
    review_comment: str | None
    created_at: datetime
    updated_at: datetime


class AssignmentRead(ApiModel):
    id: UUID
    title: str
    description: str | None
    owner_organization_id: UUID | None
    created_by: UUID | None
    reviewer_id: UUID | None
    due_at: datetime | None
    priority: str
    expected_result: dict[str, Any]
    related_request_id: UUID | None
    status: str
    executions: list[AssignmentExecutionRead]
    created_at: datetime
    updated_at: datetime


class AssignmentAction(ApiModel):
    action: Literal["start", "cancel"]


class ExecutionUpdate(ApiModel):
    action: Literal["start", "submit", "accept", "rework", "cancel"]
    result: dict[str, Any] | None = None
    comment: str | None = Field(default=None, max_length=4_000)
    change_set_id: UUID | None = None


class InteragencyRequestCreate(ApiModel):
    sender_organization_id: UUID
    recipient_organization_id: UUID
    purpose: str = Field(min_length=1, max_length=10_000)
    object_ids: list[UUID] = Field(default_factory=list, max_length=50_000)
    field_codes: list[str] = Field(default_factory=list, max_length=1_000)
    form_id: UUID | None = None
    due_at: datetime | None = None
    expected_format: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def different_organizations(self) -> "InteragencyRequestCreate":
        if self.sender_organization_id == self.recipient_organization_id:
            raise ValueError("Отправитель и получатель должны различаться")
        return self


class InteragencyAction(ApiModel):
    action: Literal["send", "start", "requestClarification", "complete", "cancel"]


class InteragencyResponseCreate(ApiModel):
    payload: dict[str, Any] = Field(default_factory=dict)
    change_set_id: UUID | None = None


class InteragencyResponseRead(ApiModel):
    id: UUID
    version: int
    payload: dict[str, Any]
    change_set_id: UUID | None
    submitted_by: UUID | None
    submitted_at: datetime
    accepted_by: UUID | None
    accepted_at: datetime | None


class InteragencyRequestRead(ApiModel):
    id: UUID
    sender_organization_id: UUID
    recipient_organization_id: UUID
    purpose: str
    object_ids: list[UUID]
    field_codes: list[str]
    form_id: UUID | None
    due_at: datetime | None
    expected_format: str | None
    status: str
    created_by: UUID | None
    responses: list[InteragencyResponseRead]
    created_at: datetime
    updated_at: datetime
