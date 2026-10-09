from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.modules.entities.api.schemas import ApiModel, to_camel

ObjectValue = str | int | float | bool | list[str] | None
DisplayValue = str | list[str] | None


class ObjectFilterOperator(StrEnum):
    """Поддерживаемые операции над системными и динамическими полями."""

    EQUALS = "equals"
    NOT_EQUALS = "notEquals"
    CONTAINS = "contains"
    STARTS_WITH = "startsWith"
    ENDS_WITH = "endsWith"
    GREATER_THAN = "greaterThan"
    GREATER_OR_EQUAL = "greaterOrEqual"
    LESS_THAN = "lessThan"
    LESS_OR_EQUAL = "lessOrEqual"
    IN = "in"
    NOT_IN = "notIn"
    FILLED = "filled"
    EMPTY = "empty"
    TODAY = "today"
    BEFORE_TODAY = "beforeToday"
    AFTER_TODAY = "afterToday"


class ObjectStatusScope(StrEnum):
    """Набор состояний объектов, включаемых в выборку."""

    CURRENT = "current"
    ARCHIVED = "archived"
    ALL = "all"


class GeoJsonGeometry(ApiModel):
    """Геометрия в формате GeoJSON и системе координат WGS 84 (EPSG:4326)."""

    type: Literal[
        "Point",
        "MultiPoint",
        "LineString",
        "MultiLineString",
        "Polygon",
        "MultiPolygon",
        "GeometryCollection",
    ] = Field(
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
    owner_organization_id: UUID | None = Field(
        default=None,
        description="Организация-владелец записи; по умолчанию владелец реестра",
    )
    responsible_id: UUID | None = Field(
        default=None,
        description="UUID ответственного пользователя Keycloak",
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
    owner_organization_id: UUID | None = Field(
        default=None,
        description="Новая организация-владелец или null",
    )
    responsible_id: UUID | None = Field(
        default=None,
        description="Новый ответственный или null",
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
    ]

    @model_validator(mode="after")
    def require_change(self) -> "EntityObjectPatch":
        if self.values is None and not {
            "geometry",
            "owner_organization_id",
            "responsible_id",
        }.intersection(self.model_fields_set):
            raise ValueError("Необходимо передать values, geometry, ownerOrganizationId или responsibleId")
        return self


class ObjectFilter(ApiModel):
    """Одно условие фильтрации объектов."""

    field: str = Field(
        min_length=1,
        max_length=241,
        description=(
            "Код динамического или системного поля. Для родителя: "
            "parent.<кодПоля>; для reference-поля: "
            "<кодReferenceПоля>.<кодПоляСвязаннойСущности>"
        ),
    )
    operator: ObjectFilterOperator = Field(description="Оператор сравнения")
    value: str | None = Field(default=None, description="Значение для сравнения")


class ObjectSearch(ApiModel):
    """Параметры расширенного поиска объектов."""

    logic: Literal["and", "or"] = Field(default="and", description="Логика объединения условий")
    q: str | None = Field(
        default=None,
        min_length=1,
        max_length=300,
        description="Полнотекстовый поиск по полям с searchable=true",
    )
    status: ObjectStatusScope = Field(
        default=ObjectStatusScope.CURRENT,
        description="Текущие, архивные или все доступные объекты",
    )
    filters: list[ObjectFilter] = Field(
        default_factory=list,
        max_length=50,
        description="Условия фильтрации",
    )
    object_ids: list[UUID] = Field(default_factory=list, max_length=5000)
    excluded_ids: list[UUID] = Field(default_factory=list, max_length=5000)
    sort: str | None = Field(
        default=None,
        max_length=130,
        description="Код поля сортировки; - означает убывание",
    )
    parent_object_id: UUID | None = Field(
        default=None,
        description="Ограничить поиск дочерними объектами указанного родителя",
    )
    bbox: tuple[float, float, float, float] | None = Field(
        default=None,
        description="Область карты: minLon, minLat, maxLon, maxLat",
    )
    limit: int = Field(
        default=25,
        ge=1,
        le=1000,
        description="Количество объектов на странице",
    )
    offset: int = Field(default=0, ge=0, description="Смещение от начала выборки")


class RegistryProjection(ApiModel):
    """Колонки, фильтры и сортировка одной сущности в составной выборке."""

    columns: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="Коды выдаваемых полей; пустой список означает поля listVisible",
    )
    q: str | None = Field(default=None, min_length=1, max_length=300)
    status: ObjectStatusScope = ObjectStatusScope.CURRENT
    logic: Literal["and", "or"] = Field(default="and", description="Логика фильтров")
    filters: list[ObjectFilter] = Field(default_factory=list, max_length=50)
    sort: str | None = Field(default=None, max_length=130)
    selection_mode: Literal["filter", "ids"] = "filter"
    object_ids: list[UUID] = Field(default_factory=list, max_length=5000)
    excluded_ids: list[UUID] = Field(default_factory=list, max_length=5000)

    @model_validator(mode="after")
    def validate_selection(self) -> "RegistryProjection":
        if self.selection_mode == "ids" and not self.object_ids:
            raise ValueError("Для selectionMode=ids необходимо передать objectIds")
        if self.selection_mode == "ids" and self.excluded_ids:
            raise ValueError("excludedIds применим только к selectionMode=filter")
        return self


class ChildRegistryProjection(RegistryProjection):
    """Подреестр, присоединяемый через parentObjectId."""

    entity_code: str = Field(min_length=1, max_length=120)
    limit_per_parent: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Максимум дочерних объектов на одну запись родителя",
    )


class RegistryTreeSearch(RegistryProjection):
    """Проекция реестра вместе с его непосредственными подреестрами."""

    children: list[ChildRegistryProjection] = Field(default_factory=list, max_length=10)
    parent_object_id: UUID | None = None
    bbox: tuple[float, float, float, float] | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class ProjectedObjectRead(ApiModel):
    """Объект с выбранными колонками и сгруппированными подреестрами."""

    id: UUID
    parent_object_id: UUID | None
    status: str
    data_quality: str
    values: dict[str, ObjectValue]
    display_values: dict[str, DisplayValue] = Field(default_factory=dict)
    children: dict[str, list[ProjectedObjectRead]] = Field(default_factory=dict)


class RegistryTreePage(ApiModel):
    """Страница составной выборки без размножения родительских строк."""

    total: int
    returned: int
    offset: int
    limit: int
    data: list[ProjectedObjectRead]


class GeneratedApiKeyRead(ApiModel):
    """Ключ read-only API; значение доступно только администратору."""

    entity_id: UUID
    entity_code: str
    client_id: str
    api_key: str = Field(
        description="Полное значение заголовка X-API-Key. Не сохранять во frontend-коде."
    )
    header_name: Literal["X-API-Key"] = "X-API-Key"


class GeneratedApiKeyDeleteRead(ApiModel):
    entity_id: UUID
    deleted: bool = True


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
    schema_version_id: UUID
    owner_organization_id: UUID | None
    responsible_id: UUID | None
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
    archived_at: datetime | None


class EntityObjectPage(ApiModel):
    """Страница объектов с общим количеством записей."""

    total: int
    returned: int
    offset: int
    limit: int
    data: list[EntityObjectRead]


class ObjectClusterRead(ApiModel):
    """Кластер объектов для текущего масштаба карты."""

    longitude: float
    latitude: float
    count: int


class ObjectClusterPage(ApiModel):
    clusters: list[ObjectClusterRead]
    total_objects: int


class ObjectClusterSearch(ApiModel):
    """Единые параметры кластеризации и табличной фильтрации."""

    bbox: tuple[float, float, float, float]
    zoom: int = Field(ge=0, le=24)
    q: str | None = Field(default=None, min_length=1, max_length=300)
    status: ObjectStatusScope = ObjectStatusScope.CURRENT
    parent_object_id: UUID | None = None
    logic: Literal["and", "or"] = "and"
    filters: list[ObjectFilter] = Field(default_factory=list, max_length=50)


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


class ObjectImportJobPage(ApiModel):
    items: list[ObjectImportJobRead]
    total: int
    limit: int
    offset: int


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
