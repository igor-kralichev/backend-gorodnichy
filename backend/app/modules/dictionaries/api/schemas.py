from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.entities.api.schemas import to_camel


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class DictionaryItemCreate(ApiModel):
    """Элемент справочника."""

    code: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="Машинный код элемента; если не передан, формируется из названия",
    )
    name: str = Field(min_length=1, max_length=500, description="Отображаемое значение")
    active: bool = Field(default=True, description="Доступен ли элемент для выбора")
    sort_order: int = Field(default=1, ge=1, description="Порядок сортировки")

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Название элемента не может состоять только из пробелов")
        return stripped


class DictionaryCreate(ApiModel):
    """Команда создания справочника сущности."""

    entity_id: UUID = Field(description="UUID сущности, которой принадлежит справочник")
    code: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="Машинный код справочника; если не передан, формируется из названия",
    )
    name: str = Field(min_length=1, max_length=255, description="Название справочника")
    items: list[DictionaryItemCreate] = Field(
        default_factory=list,
        max_length=5000,
        description="Начальные элементы справочника",
    )

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Название справочника не может состоять только из пробелов")
        return stripped

    @model_validator(mode="after")
    def validate_items(self) -> "DictionaryCreate":
        explicit_codes = [item.code for item in self.items if item.code]
        if len(set(explicit_codes)) != len(explicit_codes):
            raise ValueError("Коды элементов справочника должны быть уникальными")
        return self


class DictionaryUpdate(ApiModel):
    """Команда изменения справочника сущности."""

    code: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="Новый машинный код справочника",
    )
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Новое название справочника",
    )
    items: list[DictionaryItemCreate] | None = Field(
        default=None,
        max_length=5000,
        description="Полный новый набор элементов справочника",
    )

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Название справочника не может состоять только из пробелов")
        return stripped

    @model_validator(mode="after")
    def validate_update(self) -> "DictionaryUpdate":
        if not self.model_fields_set:
            raise ValueError("Необходимо передать хотя бы одно изменение")
        if self.items is not None:
            explicit_codes = [item.code for item in self.items if item.code]
            if len(set(explicit_codes)) != len(explicit_codes):
                raise ValueError("Коды элементов справочника должны быть уникальными")
        return self


class DictionaryItemRead(ApiModel):
    """Элемент справочника из базы данных."""

    id: UUID = Field(description="UUID элемента")
    code: str = Field(description="Машинный код элемента")
    name: str = Field(description="Отображаемое значение")
    active: bool = Field(description="Доступен ли элемент для выбора")
    sort_order: int = Field(description="Порядок сортировки")
    created_at: datetime = Field(description="Дата создания")
    updated_at: datetime = Field(description="Дата изменения")


class DictionaryRead(ApiModel):
    """Справочник сущности."""

    id: UUID = Field(description="UUID справочника")
    entity_id: UUID = Field(description="UUID сущности")
    code: str = Field(description="Машинный код справочника")
    name: str = Field(description="Название справочника")
    active: bool = Field(description="Доступен ли справочник")
    items: list[DictionaryItemRead] = Field(description="Элементы справочника")
    created_at: datetime = Field(description="Дата создания")
    updated_at: datetime = Field(description="Дата изменения")


class DictionaryListRead(ApiModel):
    """Страница справочников."""

    items: list[DictionaryRead] = Field(description="Справочники")
    total: int = Field(ge=0, description="Общее количество справочников")
    limit: int = Field(ge=1, description="Количество справочников на странице")
    offset: int = Field(ge=0, description="Смещение от начала списка")


class DictionaryStatusRead(ApiModel):
    """Результат изменения статуса справочника."""

    id: UUID = Field(description="UUID справочника")
    active: bool = Field(description="Доступен ли справочник")


class DictionaryDeleteRead(ApiModel):
    """Результат удаления справочника."""

    id: UUID = Field(description="UUID справочника")
    deleted: bool = Field(description="Справочник удалён")
