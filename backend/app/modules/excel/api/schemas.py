from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator

from app.modules.entities.api.schemas import ApiModel


class ExcelMapping(ApiModel):
    """Сопоставление заголовков XLSX с кодами полей сущности."""

    sheet_name: str
    header_row: int = Field(default=1, ge=1)
    columns: dict[str, str] = Field(description="Заголовок столбца — код поля сущности")


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
