from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.entities.api.schemas import to_camel


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


AttachmentKind = Literal["photo", "document"]


class AttachmentRead(ApiModel):
    """Метаданные файла объекта."""

    id: UUID = Field(description="UUID файла в платформе")
    entity_id: UUID = Field(description="UUID сущности")
    entity_code: str = Field(description="Код сущности")
    object_id: UUID = Field(description="UUID объекта")
    kind: AttachmentKind = Field(description="Тип файла: фото или документ")
    original_name: str = Field(description="Имя файла, которое загрузил пользователь")
    storage_key: str = Field(description="Технический ключ файла в MinIO")
    mime_type: str = Field(description="MIME-тип файла")
    size_bytes: int = Field(ge=0, description="Размер файла в байтах")
    checksum_sha256: str = Field(description="SHA-256 содержимого файла")
    uploaded_by: UUID | None = Field(default=None, description="UUID пользователя, загрузившего файл")
    created_at: datetime = Field(description="Дата загрузки")
    updated_at: datetime = Field(description="Дата изменения")


class AttachmentPage(ApiModel):
    """Страница файлов объекта."""

    items: list[AttachmentRead] = Field(description="Файлы объекта")
    total: int = Field(ge=0, description="Общее количество файлов")
    limit: int = Field(ge=1, description="Количество файлов на странице")
    offset: int = Field(ge=0, description="Смещение от начала списка")


class AttachmentUpdate(ApiModel):
    """Команда изменения метаданных файла."""

    original_name: str | None = Field(default=None, min_length=1, max_length=1024, description="Новое имя файла для UI")
    kind: AttachmentKind | None = Field(default=None, description="Новый тип файла")

    @field_validator("original_name")
    @classmethod
    def strip_original_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Имя файла не может состоять только из пробелов")
        return stripped


class AttachmentDeleteRead(ApiModel):
    """Результат удаления файла."""

    id: UUID = Field(description="UUID удалённого файла")
    deleted: bool = Field(description="Файл удалён")
