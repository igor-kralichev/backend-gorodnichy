from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.modules.entities.api.schemas import ApiModel


class NotificationRead(ApiModel):
    id: UUID
    type: str
    title: str
    message: str
    resource_type: str | None
    resource_id: UUID | None
    metadata: dict = Field(default_factory=dict)
    read_at: datetime | None
    created_at: datetime


class NotificationPage(ApiModel):
    items: list[NotificationRead]
    total: int
    unread: int
    limit: int
    offset: int


class NotificationReadState(ApiModel):
    read: bool
