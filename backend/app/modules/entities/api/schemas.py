from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.entities.domain.enums import (
    EntityStatus,
    FieldType,
    GeometryType,
    MapGeometryType,
    MapRuleOperator,
)


def to_camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(word.capitalize() for word in rest)


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


HexColor = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]


class MapStyle(ApiModel):
    """Визуальный стиль геометрии на карте."""

    fill: HexColor = Field(description="Цвет заливки в формате HEX")
    stroke: HexColor = Field(description="Цвет контура в формате HEX")
    stroke_width: float = Field(gt=0, le=20, description="Толщина контура")
    point_size: float = Field(gt=0, le=100, description="Размер точечного маркера")
    opacity: float = Field(ge=0, le=1, description="Прозрачность от 0 до 1")


class MapStylesCreate(ApiModel):
    """Переопределения стандартных стилей карты."""

    point: MapStyle | None = Field(default=None, description="Стиль точек")
    line_string: MapStyle | None = Field(default=None, alias="lineString", description="Стиль линий")
    polygon: MapStyle | None = Field(default=None, description="Стиль полигонов")

    def as_dict(self) -> dict[MapGeometryType, MapStyle]:
        result: dict[MapGeometryType, MapStyle] = {}
        if self.point is not None:
            result[MapGeometryType.POINT] = self.point
        if self.line_string is not None:
            result[MapGeometryType.LINE_STRING] = self.line_string
        if self.polygon is not None:
            result[MapGeometryType.POLYGON] = self.polygon
        return result


class MapStylesRead(ApiModel):
    """Полный набор стилей карты."""

    point: MapStyle
    line_string: MapStyle = Field(alias="lineString")
    polygon: MapStyle


class MapColorRuleCreate(ApiModel):
    """Правило окрашивания объекта по значению поля."""

    name: str = Field(min_length=1, max_length=255, description="Название правила")
    field_code: str = Field(min_length=1, max_length=120, description="Код поля из этой сущности")
    operator: MapRuleOperator = Field(description="Оператор сравнения")
    value: str = Field(default="", max_length=2000, description="Сравниваемое значение")
    color: HexColor = Field(description="Цвет результата в формате HEX")


class MapSettingsCreate(ApiModel):
    """Настройки карты создаваемой сущности."""

    enabled_geometry_types: list[MapGeometryType] = Field(
        default_factory=list,
        description=(
            "Разрешённые типы геометрии; "
            "для сущности без карты передайте пустой список"
        ),
    )
    clustering_enabled: bool = Field(
        default=False,
        description="Объединять близкие точки в кластеры",
    )
    styles: MapStylesCreate = Field(default_factory=MapStylesCreate, description="Стили геометрий")
    color_rules: list[MapColorRuleCreate] = Field(
        default_factory=list,
        description="Правила окрашивания",
    )
    selectable: bool = Field(
        default=True,
        description="Разрешить выбор объектов слоя на карте",
    )
    visible_by_default: bool = Field(
        default=True,
        description="Показывать слой при первом открытии карты",
    )

    @model_validator(mode="after")
    def reject_none_geometry(self) -> "MapSettingsCreate":
        if len(set(self.enabled_geometry_types)) != len(self.enabled_geometry_types):
            raise ValueError(
                "enabledGeometryTypes не должен содержать повторяющиеся значения"
            )
        return self


class EntityFieldCreate(ApiModel):
    """Описание одного пользовательского поля сущности."""

    code: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=r"^[a-z][a-z0-9_]*$",
        description=(
            "Стабильный машинный код; "
            "если не передан, формируется из названия"
        ),
    )
    name: str = Field(min_length=1, max_length=255, description="Отображаемое название поля")
    type: FieldType = Field(description="Тип значения поля")
    required: bool = Field(
        default=False,
        description="Обязательно ли поле для публикации объекта",
    )
    list_visible: bool = Field(default=True, description="Показывать поле в таблице")
    card_visible: bool = Field(default=True, description="Показывать поле в карточке")
    searchable: bool = Field(default=True, description="Участвует ли поле в поиске")
    filterable: bool = Field(default=True, description="Доступно ли поле для фильтрации")
    enum_id: UUID | None = Field(
        default=None,
        description="UUID справочника; только для типа enum",
    )
    reference_entity_id: UUID | None = Field(
        default=None,
        description="UUID связанной сущности; только для типа reference",
    )

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Название поля не может состоять только из пробелов")
        return stripped

    @model_validator(mode="after")
    def validate_reference(self) -> "EntityFieldCreate":
        if self.type == FieldType.ENUM and self.enum_id is None:
            raise ValueError("Для поля типа enum необходимо передать enumId")
        if self.type == FieldType.REFERENCE and self.reference_entity_id is None:
            raise ValueError(
                "Для поля типа reference необходимо передать referenceEntityId"
            )
        if self.type != FieldType.ENUM and self.enum_id is not None:
            raise ValueError("enumId разрешён только для поля типа enum")
        if self.type != FieldType.REFERENCE and self.reference_entity_id is not None:
            raise ValueError("referenceEntityId разрешён только для поля типа reference")
        return self


class EntityCreate(ApiModel):
    """Команда создания черновика схемы сущности."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        title="Создание схемы сущности",
        json_schema_extra={
            "examples": [
                {
                    "name": "Учебные заведения",
                    "description": "Школы, СПО и вузы",
                    "geometryType": "point",
                    "includeAddress": True,
                    "fields": [
                        {
                            "name": "Название",
                            "type": "string",
                            "required": True,
                            "listVisible": True,
                            "cardVisible": True,
                            "searchable": True,
                            "filterable": True,
                        },
                        {
                            "name": "Количество учащихся",
                            "type": "integer",
                            "required": False,
                            "listVisible": True,
                            "cardVisible": True,
                            "searchable": True,
                            "filterable": True,
                        },
                    ],
                    "mapSettings": {
                        "enabledGeometryTypes": ["point", "polygon"],
                        "clusteringEnabled": True,
                        "styles": {},
                        "colorRules": [],
                    },
                }
            ]
        },
    )

    code: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=r"^[a-z][a-z0-9_]*$",
        description=(
            "Уникальный машинный код; "
            "если не передан, формируется из названия"
        ),
    )
    name: str = Field(min_length=1, max_length=255, description="Название сущности")
    description: str | None = Field(
        default=None,
        max_length=5000,
        description="Описание назначения сущности",
    )
    parent_entity_id: UUID | None = Field(
        default=None,
        description=(
            "UUID родительской сущности. Если передан, объекты этой сущности "
            "должны создаваться внутри объекта родительской сущности."
        ),
    )
    geometry_type: GeometryType = Field(
        default=GeometryType.NONE,
        description="Основной тип геометрии",
    )
    include_address: bool = Field(
        default=True,
        description="Автоматически добавить системное поле адреса",
    )
    fields: list[EntityFieldCreate] = Field(
        default_factory=list,
        max_length=200,
        description="Пользовательские поля",
    )
    map_settings: MapSettingsCreate | None = Field(default=None, description="Настройки карты")
    scope_municipality_id: UUID | None = Field(
        default=None,
        description=(
            "UUID существующего муниципалитета; "
            "можно не передавать"
        ),
    )

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Название сущности не может состоять только из пробелов")
        return stripped

    @model_validator(mode="after")
    def validate_fields(self) -> "EntityCreate":
        explicit_codes = [field.code for field in self.fields if field.code]
        if len(set(explicit_codes)) != len(explicit_codes):
            raise ValueError("Коды полей должны быть уникальными")
        if sum(field.type == FieldType.ADDRESS for field in self.fields) > 1:
            raise ValueError("Разрешено только одно поле адреса")
        enabled_geometry_types = (
            self.map_settings.enabled_geometry_types if self.map_settings else []
        )
        if (
            not self.include_address
            and not self.fields
            and self.geometry_type == GeometryType.NONE
            and not enabled_geometry_types
        ):
            raise ValueError(
                "Runtime-сущность должна содержать хотя бы одно поле или геометрию"
            )
        return self


class EntityUpdate(ApiModel):
    """Команда изменения существующей схемы сущности."""

    code: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="Новый уникальный машинный код",
    )
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Новое название сущности",
    )
    description: str | None = Field(
        default=None,
        max_length=5000,
        description="Новое описание; null очищает описание",
    )
    parent_entity_id: UUID | None = Field(
        default=None,
        description=(
            "Новая родительская сущность; null делает сущность корневой"
        ),
    )
    geometry_type: GeometryType | None = Field(
        default=None,
        description="Новый основной тип геометрии",
    )
    include_address: bool | None = Field(
        default=None,
        description="Добавить или убрать системное поле адреса",
    )
    fields: list[EntityFieldCreate] | None = Field(
        default=None,
        max_length=200,
        description="Полный новый набор пользовательских полей",
    )
    map_settings: MapSettingsCreate | None = Field(
        default=None,
        description="Полный набор настроек карты",
    )
    scope_municipality_id: UUID | None = Field(
        default=None,
        description="Новый муниципалитет; null убирает ограничение",
    )

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Название сущности не может состоять только из пробелов")
        return stripped

    @model_validator(mode="after")
    def validate_fields(self) -> "EntityUpdate":
        if not self.model_fields_set:
            raise ValueError("Необходимо передать хотя бы одно изменение")
        if self.fields is not None:
            explicit_codes = [field.code for field in self.fields if field.code]
            if len(set(explicit_codes)) != len(explicit_codes):
                raise ValueError("Коды полей должны быть уникальными")
            if sum(field.type == FieldType.ADDRESS for field in self.fields) > 1:
                raise ValueError("Разрешено только одно поле адреса")
        return self


class EntityDuplicateCreate(ApiModel):
    """Параметры копирования схемы сущности."""

    code: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="Код копии; если не передан, генерируется автоматически",
    )
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Название копии",
    )

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Название копии не может состоять только из пробелов")
        return stripped


class EntityFieldRead(ApiModel):
    """Сохранённое поле схемы сущности."""

    id: UUID
    code: str
    name: str
    type: FieldType
    required: bool
    list_visible: bool
    card_visible: bool
    searchable: bool
    filterable: bool
    order: int
    enum_id: UUID | None = None
    reference_entity_id: UUID | None = None


class MapColorRuleRead(MapColorRuleCreate):
    """Сохранённое правило окрашивания карты."""

    id: UUID


class MapSettingsRead(ApiModel):
    """Сохранённые настройки карты."""

    enabled_geometry_types: list[MapGeometryType]
    clustering_enabled: bool
    styles: MapStylesRead
    color_rules: list[MapColorRuleRead]
    selectable: bool
    visible_by_default: bool


class EntityRead(ApiModel):
    """Полное представление схемы сущности."""

    id: UUID
    code: str
    name: str
    description: str | None
    geometry_type: GeometryType
    parent_entity_id: UUID | None
    map_settings: MapSettingsRead
    fields: list[EntityFieldRead]
    status: EntityStatus
    version: int
    scope_municipality_id: UUID | None
    created_at: datetime
    updated_at: datetime


class EntityListRead(ApiModel):
    """Страница схем сущностей."""

    total: int
    returned: int
    offset: int
    limit: int
    data: list[EntityRead]


class EntityStatusRead(ApiModel):
    """Результат изменения статуса схемы сущности."""

    id: UUID
    code: str
    status: EntityStatus
    updated_at: datetime


class EntityDeleteRead(ApiModel):
    """Результат полного удаления схемы сущности."""

    id: UUID
    code: str
    deleted: bool
