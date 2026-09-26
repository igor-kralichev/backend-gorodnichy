from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.modules.entities.api.schemas import ApiModel


class ObjectSuggestionRead(ApiModel):
    """Подсказка по уже сохранённому объекту."""

    entity_id: UUID
    entity_code: str
    entity_name: str
    object_id: UUID
    field_code: str
    value: str
    updated_at: datetime = Field(description="Дата последнего изменения объекта")


class ObjectSuggestionPage(ApiModel):
    """Список подсказок по объектам."""

    query: str
    returned: int
    data: list[ObjectSuggestionRead]
