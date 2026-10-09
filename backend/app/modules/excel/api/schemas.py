from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.modules.entities.api.schemas import ApiModel
from app.modules.objects.api.schemas import (
    ChildRegistryProjection,
    GeoJsonGeometry,
    RegistryProjection,
)


class ExcelSystemMapping(ApiModel):
    """Сопоставление системных колонок импорта."""

    fixed_parent_object_id: UUID | None = None
    object_id_column: str | None = Field(
        default=None,
        description="Необязательная колонка UUID объекта для update/archive",
    )
    action_column: str | None = Field(
        default=None,
        description="Необязательная колонка действия create/update/archive",
    )
    parent_object_column: str | None = None
    latitude_column: str | None = None
    longitude_column: str | None = None
    geometry_column: str | None = None

    @model_validator(mode="after")
    def validate_mapping(self) -> "ExcelSystemMapping":
        if self.fixed_parent_object_id and self.parent_object_column:
            raise ValueError("Родитель задаётся либо фиксированным ID, либо колонкой")
        if bool(self.latitude_column) != bool(self.longitude_column):
            raise ValueError("Колонки широты и долготы задаются вместе")
        if self.geometry_column and self.latitude_column:
            raise ValueError("Геометрия задаётся либо GeoJSON, либо координатами")
        return self


class ExcelMapping(ApiModel):
    """Сопоставление заголовков XLSX с кодами полей сущности."""

    sheet_name: str
    header_row: int = Field(default=1, ge=1)
    columns: dict[str, str] = Field(description="Заголовок столбца — код поля сущности")
    system: ExcelSystemMapping = Field(default_factory=ExcelSystemMapping)


class ExcelPreviewRequest(ApiModel):
    mapping: ExcelMapping | None = None
    preview_rows: int = Field(default=20, ge=1, le=100)


class ExcelCellIssue(ApiModel):
    sheet: str
    row: int
    column: str
    field_code: str | None = None
    code: str
    message: str


class ExcelPreviewRow(ApiModel):
    row: int
    values: dict[str, Any]
    parent_object_id: UUID | None = None
    geometry: GeoJsonGeometry | None = None
    errors: list[ExcelCellIssue]


class ExcelPreviewRead(ApiModel):
    sheets: list[str]
    selected_sheet: str
    header_row: int
    headers: list[str]
    proposed_mapping: dict[str, str]
    total_rows: int
    rows: list[ExcelPreviewRow]


class ImportProfileCreate(ApiModel):
    entity_schema_id: UUID
    name: str = Field(min_length=1, max_length=255)
    mapping: ExcelMapping

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return value.strip()


class ImportProfileRead(ApiModel):
    id: UUID
    entity_schema_id: UUID
    owner_id: UUID
    name: str
    sheet_name: str | None
    header_row: int
    mapping: dict[str, str]
    created_at: datetime
    updated_at: datetime


class ImportProfileDeleteRead(ApiModel):
    id: UUID
    deleted: bool = True


class ExcelImportPlanRead(ApiModel):
    id: UUID
    entity_id: UUID
    schema_version_id: UUID
    source_name: str
    status: str
    summary: dict[str, int]
    rows: list[dict[str, Any]]
    total_rows: int
    limit: int
    offset: int
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class ExcelImportPlanDecision(ApiModel):
    action: Literal["apply", "reject", "cancel"]
    idempotency_key: str | None = Field(default=None, max_length=255)
    resolutions: dict[str, Literal["file", "current"]] = Field(
        default_factory=dict,
        description="Решения конфликтов по ключу rowIndex.fieldCode",
    )


class ExcelExportJobRead(ApiModel):
    """Состояние фоновой XLSX-выгрузки."""

    id: UUID
    entity_id: UUID
    entity_code: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    total_rows: int
    filename: str | None
    error: str | None
    download_url: str | None
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class ExcelExportJobPage(ApiModel):
    items: list[ExcelExportJobRead]
    total: int
    limit: int
    offset: int


class ExcelExportJobCommand(ApiModel):
    command: Literal["cancel"]


class ExcelExportChildProjection(ChildRegistryProjection):
    """Подреестр в фоновой выгрузке."""

    limit_per_parent: int = Field(default=1000, ge=1, le=500_000)


class ExcelExportRequest(RegistryProjection):
    """Снимок фильтров и колонок для фоновой XLSX-выгрузки."""

    children: list[ExcelExportChildProjection] = Field(default_factory=list, max_length=10)
    parent_object_id: UUID | None = None
    bbox: tuple[float, float, float, float] | None = None
    limit: int = Field(default=50_000, ge=1, le=500_000)
    offset: int = Field(default=0, ge=0)
