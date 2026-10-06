from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.entities.api.schemas import to_camel


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


AuditResourceType = Literal[
    "entity_schema",
    "entity_object",
    "dictionary",
    "user",
    "attachment",
    "import_job",
    "organization",
    "membership",
    "permission_grant",
    "relation",
    "change_set",
    "form",
    "information_request",
    "form_submission",
    "assignment",
    "assignment_execution",
    "interagency_request",
    "interagency_response",
]


class AuditChangeRead(ApiModel):
    """Одно изменённое поле в событии истории."""

    path: str = Field(description="Путь к изменённому полю")
    old_value: Any = Field(default=None, description="Старое значение")
    new_value: Any = Field(default=None, description="Новое значение")


class AuditEventRead(ApiModel):
    """Событие единой истории изменений."""

    id: UUID = Field(description="UUID события")
    resource_type: AuditResourceType = Field(description="Тип изменённого ресурса")
    resource_id: UUID | None = Field(default=None, description="UUID изменённого ресурса")
    resource_code: str | None = Field(default=None, description="Код изменённого ресурса")
    resource_name: str | None = Field(default=None, description="Название изменённого ресурса")
    action: str = Field(description="Выполненное действие")
    actor_id: UUID | None = Field(default=None, description="UUID пользователя Keycloak")
    actor_full_name: str | None = Field(default=None, description="ФИО пользователя на момент изменения")
    actor_email: str | None = Field(default=None, description="Email пользователя на момент изменения")
    occurred_at: datetime = Field(description="Дата и время изменения")
    old_value: dict[str, Any] | None = Field(default=None, description="Состояние до изменения")
    new_value: dict[str, Any] | None = Field(default=None, description="Состояние после изменения")
    changes: list[AuditChangeRead] = Field(description="Список изменённых полей")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Дополнительные сведения")


class AuditEventPage(ApiModel):
    """Страница событий истории изменений."""

    items: list[AuditEventRead] = Field(description="События истории")
    total: int = Field(ge=0, description="Общее количество событий")
    limit: int = Field(ge=1, description="Количество событий на странице")
    offset: int = Field(ge=0, description="Смещение от начала списка")
