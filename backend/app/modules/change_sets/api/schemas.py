from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.modules.entities.api.schemas import ApiModel
from app.modules.objects.api.schemas import GeoJsonGeometry, ValidationIssue


class ChangeSetItemCreate(ApiModel):
    object_id: UUID | None = None
    operation: Literal["create", "update", "archive", "confirm"]
    base_revision: int | None = Field(default=None, ge=1)
    values: dict[str, Any] = Field(default_factory=dict)
    geometry: GeoJsonGeometry | None = None

    @model_validator(mode="after")
    def validate_target(self) -> "ChangeSetItemCreate":
        if self.operation == "create" and self.object_id is not None:
            raise ValueError("Для создания objectId не передаётся")
        if self.operation != "create" and self.object_id is None:
            raise ValueError("Для изменения необходимо передать objectId")
        if self.operation == "update" and self.base_revision is None:
            raise ValueError("Для изменения необходимо передать baseRevision")
        return self


class ChangeSetCreate(ApiModel):
    entity_code: str
    source: Literal["ui", "excel", "form", "api", "actualization"] = "ui"
    idempotency_key: str | None = Field(default=None, max_length=255)
    items: list[ChangeSetItemCreate] = Field(min_length=1, max_length=50_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChangeSetItemRead(ApiModel):
    id: UUID
    object_id: UUID | None
    operation: str
    base_revision: int | None
    proposed_values: dict[str, Any]
    validation_errors: list[ValidationIssue]
    status: str
    result_object_id: UUID | None


class ChangeSetRead(ApiModel):
    id: UUID
    entity_id: UUID
    schema_version_id: UUID
    source: str
    status: str
    idempotency_key: str | None
    created_by: UUID | None
    decided_by: UUID | None
    decided_at: datetime | None
    metadata: dict[str, Any]
    items: list[ChangeSetItemRead]
    created_at: datetime
    updated_at: datetime


class ChangeSetDecision(ApiModel):
    action: Literal["apply", "reject", "cancel"]
