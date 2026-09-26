from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.modules.entities.api.schemas import ApiModel, to_camel

ObjectValue = str | int | float | bool | list[str] | None
DisplayValue = str | list[str] | None


class GeoJsonGeometry(ApiModel):
    """Геометрия в формате GeoJSON и системе координат WGS 84 (EPSG:4326)."""

    type: Literal["Point", "LineString", "Polygon", "GeometryCollection"] = Field(
        description="Тип геометрии GeoJSON"
    )
    coordinates: list[Any] | None = Field(
        default=None,
        description="Координаты в порядке долгота, широта; не используется для GeometryCollection",
    )
    geometries: list[GeoJsonGeometry] | None = Field(
        default=None,
        description="Вложенные геометрии; используется только для GeometryCollection",
    )

    @model_validator(mode="after")
    def validate_geometry_shape(self) -> "GeoJsonGeometry":
        if self.type == "GeometryCollection":
            if not self.geometries:
                raise ValueError("GeometryCollection должен содержать хотя бы одну геометрию")
            if self.coordinates is not None:
                raise ValueError("GeometryCollection не должен содержать coordinates")
            return self
        if self.coordinates is None:
            raise ValueError(f"Для геометрии {self.type} необходимо передать coordinates")
        if self.geometries is not None:
            raise ValueError(f"Для геометрии {self.type} не нужно передавать geometries")
        return self


class EntityObjectCreate(ApiModel):
    """Команда создания объекта опубликованной сущности."""

    model_config = {
        "alias_generator": to_camel,
        "populate_by_name": True,
        "extra": "forbid",
        "title": "Создание объекта сущности",
        "json_schema_extra": {
            "examples": [
                {
                    "values": {
                        "address": "г. Нижний Новгород, пр. Кирова, д. 29а",
                        "nazvanie": "МАОУ Лицей № 36",
                    },
                    "geometry": {
                        "type": "GeometryCollection",
                        "geometries": [
                            {"type": "Point", "coordinates": [43.87, 56.24]},
                            {
                                "type": "Polygon",
                                "coordinates": [
                                    [
                                        [43.86, 56.24],
                                        [43.87, 56.24],
                                        [43.87, 56.25],
                                        [43.86, 56.24],
                                    ]
                                ],
                            },
                        ],
                    },
                }
            ]
        },
    }

    values: dict[str, ObjectValue] = Field(
        default_factory=dict,
        description="Значения динамических полей по их кодам",
    )
    parent_object_id: UUID | None = Field(
        default=None,
        description=(
            "UUID родительского объекта. Обязателен для объектов дочерних "
            "сущностей и запрещён для корневых сущностей."
        ),
    )
    geometry: GeoJsonGeometry | None = Field(
        default=None,
        description="Геометрия объекта; обязательность задаёт схема",
    )


class EntityObjectPatch(ApiModel):
    """Команда частичного изменения объекта."""

    values: dict[str, ObjectValue] | None = Field(
        default=None,
        description="Изменяемые значения динамических полей",
    )
    geometry: GeoJsonGeometry | None = Field(
        default=None,
        description="Новая геометрия или null для её удаления",
    )
    revision: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Ожидаемая текущая ревизия "
                "для защиты от потери изменений"
            ),
        ),
    ] | None = None

    @model_validator(mode="after")
    def require_change(self) -> "EntityObjectPatch":
        if self.values is None and "geometry" not in self.model_fields_set:
            raise ValueError("Необходимо передать values или geometry")
        return self


class ObjectFilter(ApiModel):
    """Одно условие фильтрации объектов."""

    field: str = Field(
        min_length=1,
        max_length=120,
        description="Код динамического или системного поля",
    )
    operator: Literal[
        "equals",
        "notEquals",
        "contains",
        "startsWith",
        "endsWith",
        "greaterThan",
        "greaterOrEqual",
        "lessThan",
        "lessOrEqual",
        "in",
        "notIn",
        "filled",
        "empty",
        "today",
        "beforeToday",
        "afterToday",
    ] = Field(description="Оператор сравнения")
    value: str | None = Field(default=None, description="Значение для сравнения")


class ObjectSearch(ApiModel):
    """Параметры расширенного поиска объектов."""

    logic: Literal["and", "or"] = Field(default="and", description="Логика объединения условий")
    filters: list[ObjectFilter] = Field(
        default_factory=list,
        max_length=50,
        description="Условия фильтрации",
    )
    sort: str | None = Field(
        default=None,
        max_length=130,
        description="Код поля сортировки; - означает убывание",
    )
    parent_object_id: UUID | None = Field(
        default=None,
        description="Ограничить поиск дочерними объектами указанного родителя",
    )
    limit: int = Field(
        default=25,
        ge=1,
        le=1000,
        description="Количество объектов на странице",
    )
    offset: int = Field(default=0, ge=0, description="Смещение от начала выборки")


class ValidationIssue(ApiModel):
    """Ошибка полноты или корректности данных объекта."""

    field_code: str | None = None
    code: str
    message: str


class EntityObjectRead(ApiModel):
    """Полное представление объекта динамической сущности."""

    id: UUID
    entity_id: UUID
    entity_code: str
    parent_object_id: UUID | None
    values: dict[str, ObjectValue]
    display_values: dict[str, DisplayValue] = Field(
        default_factory=dict,
        description=(
            "Человекочитаемые значения динамических полей. "
            "Для enum-полей содержит названия элементов справочника."
        ),
    )
    geometry: GeoJsonGeometry | None
    attachment_paths: list[str] = Field(
        default_factory=list,
        description="Технические ключи файлов объекта в MinIO",
    )
    status: str
    data_quality: str
    validation_errors: list[ValidationIssue]
    revision: int
    created_at: datetime
    updated_at: datetime
    created_by: UUID | None
    updated_by: UUID | None


class EntityObjectPage(ApiModel):
    """Страница объектов с общим количеством записей."""

    total: int
    returned: int
    offset: int
    limit: int
    data: list[EntityObjectRead]


class EntityObjectBulkCreateRead(ApiModel):
    """Результат массового создания объектов одной сущности."""

    created: int
    published: int
    draft: int
    data: list[EntityObjectRead]


class ObjectImportJobRead(ApiModel):
    """Состояние фоновой загрузки объектов из JSON-файла."""

    id: UUID
    entity_id: UUID
    status: Literal["queued", "running", "paused", "completed", "failed", "cancelled"]
    source_name: str
    total_rows: int
    processed_rows: int
    error_rows: int
    created_at: datetime
    updated_at: datetime


class ObjectImportJobCommand(ApiModel):
    """Команда управления фоновой загрузкой."""

    command: Literal["pause", "resume", "cancel"] = Field(
        description="Команда: pause — поставить на паузу, resume — продолжить, cancel — отменить",
    )


class EntityObjectDeleteRead(ApiModel):
    """Результат полного удаления объекта."""

    id: UUID
    deleted: bool


class EntityObjectStatusRead(ApiModel):
    """Результат изменения статуса объекта."""

    id: UUID
    status: str
    data_quality: str
    revision: int
