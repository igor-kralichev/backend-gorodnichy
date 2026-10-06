from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.modules.entities.api.schemas import ApiModel


PermissionAction = Literal[
    "read",
    "create",
    "update",
    "archive",
    "delete",
    "manage_schema",
    "manage_access",
    "export",
    "import",
    "request",
    "review",
    "confirm",
]


class OrganizationCreate(ApiModel):
    """Команда создания организации."""

    code: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=500)
    parent_id: UUID | None = None

    @field_validator("code", "name")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class OrganizationUpdate(ApiModel):
    """Команда изменения организации."""

    name: str | None = Field(default=None, min_length=1, max_length=500)
    parent_id: UUID | None = None
    active: bool | None = None


class OrganizationRead(ApiModel):
    """Организация и её положение в иерархии."""

    id: UUID
    code: str
    name: str
    parent_id: UUID | None
    active: bool
    created_at: datetime
    updated_at: datetime


class OrganizationPage(ApiModel):
    items: list[OrganizationRead]
    total: int
    limit: int
    offset: int


class MembershipCreate(ApiModel):
    """Назначение пользователю роли внутри организации."""

    user_id: UUID
    organization_id: UUID
    role_code: str = Field(min_length=1, max_length=120)


class MembershipUpdate(ApiModel):
    active: bool


class MembershipRead(ApiModel):
    id: UUID
    user_id: UUID
    organization_id: UUID
    role_code: str
    active: bool
    created_at: datetime
    updated_at: datetime


class PermissionGrantCreate(ApiModel):
    """Грант прав пользователю или realm-роли Keycloak."""

    user_id: UUID | None = None
    role_code: str | None = Field(default=None, max_length=120)
    organization_id: UUID | None = None
    entity_schema_id: UUID | None = None
    entity_field_id: UUID | None = None
    entity_object_id: UUID | None = None
    actions: list[PermissionAction] = Field(min_length=1)
    conditions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_subject_and_scope(self) -> "PermissionGrantCreate":
        if self.user_id is None and not self.role_code:
            raise ValueError("Нужно указать userId или roleCode")
        if self.entity_field_id is not None and self.entity_schema_id is None:
            raise ValueError("Для права на поле необходимо указать entitySchemaId")
        self.actions = list(dict.fromkeys(self.actions))
        return self


class PermissionGrantRead(PermissionGrantCreate):
    id: UUID
    created_by: UUID | None
    created_at: datetime


class SavedViewCreate(ApiModel):
    """Пользовательское сохранённое представление реестра."""

    entity_schema_id: UUID
    name: str = Field(min_length=1, max_length=255)
    configuration: dict[str, Any]
    shared: bool = False

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return value.strip()


class SavedViewUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    configuration: dict[str, Any] | None = None
    shared: bool | None = None


class SavedViewRead(ApiModel):
    id: UUID
    entity_schema_id: UUID
    owner_id: UUID
    name: str
    configuration: dict[str, Any]
    shared: bool
    created_at: datetime
    updated_at: datetime


class DeleteRead(ApiModel):
    id: UUID
    deleted: bool = True
