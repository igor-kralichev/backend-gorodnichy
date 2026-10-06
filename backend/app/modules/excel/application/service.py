from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import orjson
from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import Cell
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.entities.infrastructure.models import (
    EntityFieldModel,
    EntitySchemaModel,
    EntitySchemaVersionModel,
)
from app.modules.change_sets.api.schemas import ChangeSetItemCreate
from app.modules.excel.api.schemas import (
    ExcelCellIssue,
    ExcelMapping,
    ExcelPreviewRead,
    ExcelPreviewRow,
    ImportProfileCreate,
    ImportProfileRead,
)
from app.modules.objects.api.schemas import EntityObjectCreate
from app.modules.objects.application.service import RuntimeEntityNotFound
from app.shared.db.models import EntityObjectModel, ImportProfileModel


class ExcelValidationError(Exception):
    """XLSX не соответствует ограничениям или выбранному сопоставлению."""


class ExcelService:
    """Читает и формирует XLSX без исполнения формул книги."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def preview(
        self,
        entity_code: str,
        content: bytes,
        mapping: ExcelMapping | None,
        preview_rows: int,
    ) -> ExcelPreviewRead:
        schema = await self._schema(entity_code)
        formula_workbook, value_workbook = self._workbooks(content)
        sheet = self._sheet(formula_workbook, mapping.sheet_name if mapping else None)
        value_sheet = self._sheet(value_workbook, sheet.title)
        header_row = mapping.header_row if mapping else 1
        headers = self._headers(sheet, header_row)
        technical_headers = self._headers(sheet, 1)
        exported_roundtrip = technical_headers[:4] == [
            "__objectId",
            "__revision",
            "__schemaVersionId",
            "__action",
        ]
        if exported_roundtrip:
            header_row = 2
            headers = self._headers(sheet, header_row)
            fields_by_id = {field.id.hex: field.code for field in schema.fields}
            proposed = {
                headers[index]: fields_by_id[technical.strip().replace("-", "").casefold()]
                for index, technical in enumerate(technical_headers)
                if index < len(headers)
                and technical.strip().replace("-", "").casefold() in fields_by_id
            }
        else:
            proposed = mapping.columns if mapping else self._propose_mapping(headers, schema.fields)
        rows: list[ExcelPreviewRow] = []
        max_row = sheet.max_row or 0
        for row_number in range(header_row + 1, min(max_row, header_row + preview_rows) + 1):
            values, errors = self._row_values(sheet, value_sheet, row_number, headers, proposed)
            rows.append(ExcelPreviewRow(row=row_number, values=values, errors=errors))
        return ExcelPreviewRead(
            sheets=formula_workbook.sheetnames,
            selected_sheet=sheet.title,
            header_row=header_row,
            headers=headers,
            proposed_mapping=proposed,
            total_rows=max(0, max_row - header_row),
            rows=rows,
        )

    async def to_objects(
        self, entity_code: str, content: bytes, mapping: ExcelMapping
    ) -> list[EntityObjectCreate]:
        schema = await self._schema(entity_code)
        formula_workbook, value_workbook = self._workbooks(content)
        sheet = self._sheet(formula_workbook, mapping.sheet_name)
        value_sheet = self._sheet(value_workbook, sheet.title)
        headers = self._headers(sheet, mapping.header_row)
        allowed_codes = {field.code for field in schema.fields if not field.read_only and not field.archived}
        invalid_codes = sorted(set(mapping.columns.values()) - allowed_codes)
        if invalid_codes:
            raise ExcelValidationError(
                "Сопоставление содержит недоступные поля: " + ", ".join(invalid_codes)
            )
        objects: list[EntityObjectCreate] = []
        issues: list[ExcelCellIssue] = []
        for row_number in range(mapping.header_row + 1, (sheet.max_row or 0) + 1):
            values, row_issues = self._row_values(
                sheet, value_sheet, row_number, headers, mapping.columns
            )
            if not values and not row_issues:
                continue
            issues.extend(row_issues)
            objects.append(EntityObjectCreate(values=values))
        if issues:
            first = issues[0]
            raise ExcelValidationError(
                f"{first.sheet}, строка {first.row}, столбец {first.column}: {first.message}"
            )
        if not objects:
            raise ExcelValidationError("В выбранном листе нет строк данных")
        return objects

    async def to_change_set_items(
        self, entity_code: str, content: bytes, mapping: ExcelMapping
    ) -> list[ChangeSetItemCreate]:
        """Преобразовать произвольный или ранее экспортированный XLSX в ChangeSet."""

        schema = await self._schema(entity_code)
        formula_workbook, value_workbook = self._workbooks(content)
        sheet = self._sheet(formula_workbook, mapping.sheet_name)
        value_sheet = self._sheet(value_workbook, sheet.title)
        technical_headers = self._headers(sheet, 1)
        if technical_headers[:4] != [
            "__objectId",
            "__revision",
            "__schemaVersionId",
            "__action",
        ]:
            objects = await self.to_objects(entity_code, content, mapping)
            return [
                ChangeSetItemCreate(operation="create", values=item.values)
                for item in objects
            ]

        field_by_id = {
            field.id.hex: field
            for field in schema.fields
            if not field.archived
        }
        column_fields = {
            index: field_by_id[header.strip().replace("-", "").casefold()]
            for index, header in enumerate(technical_headers[4:], start=4)
            if header.strip().replace("-", "").casefold() in field_by_id
        }
        current_schema_version_id = await self._schema_version_id(schema.id)
        items: list[ChangeSetItemCreate] = []
        for row_number in range(3, (sheet.max_row or 0) + 1):
            formula_row = sheet[row_number]
            value_row = value_sheet[row_number]
            if not any(cell.value is not None for cell in formula_row):
                continue
            raw_object_id = formula_row[0].value
            raw_revision = formula_row[1].value
            raw_schema_version_id = formula_row[2].value
            raw_action = str(formula_row[3].value or "").strip().casefold()
            values: dict[str, object] = {}
            for index, field in column_fields.items():
                if field.read_only:
                    continue
                value = self._import_cell_value(
                    formula_row[index], value_row[index], sheet.title, row_number
                )
                values[field.code] = value
            if raw_action in {"archive", "архив", "архивировать"}:
                if raw_object_id is None:
                    raise ExcelValidationError(
                        f"{sheet.title}, строка {row_number}: для архивирования нужен __objectId"
                    )
                items.append(
                    ChangeSetItemCreate(
                        object_id=UUID(str(raw_object_id)),
                        operation="archive",
                        base_revision=int(raw_revision) if raw_revision is not None else None,
                    )
                )
                continue
            if raw_object_id is None or str(raw_object_id).strip() == "":
                items.append(ChangeSetItemCreate(operation="create", values=values))
                continue
            if raw_revision is None or raw_schema_version_id is None:
                raise ExcelValidationError(
                    f"{sheet.title}, строка {row_number}: "
                    "отсутствует __revision или __schemaVersionId"
                )
            try:
                object_id = UUID(str(raw_object_id))
                revision = int(raw_revision)
                schema_version_id = UUID(str(raw_schema_version_id))
            except (TypeError, ValueError) as error:
                raise ExcelValidationError(
                    f"{sheet.title}, строка {row_number}: "
                    "некорректный ID, ревизия или версия схемы"
                ) from error
            if schema_version_id != current_schema_version_id:
                raise ExcelValidationError(
                    f"{sheet.title}, строка {row_number}: структура сущности изменилась; "
                    "выгрузите новый XLSX и повторите изменения"
                )
            items.append(
                ChangeSetItemCreate(
                    object_id=object_id,
                    operation="update",
                    base_revision=revision,
                    values=values,
                )
            )
        if not items:
            raise ExcelValidationError("В выбранном листе нет строк данных")
        return items

    async def export(self, entity_code: str) -> tuple[str, bytes]:
        schema = await self._schema(entity_code)
        current_schema_version_id = await self._schema_version_id(schema.id)
        objects = (
            await self._session.scalars(
                select(EntityObjectModel)
                .where(
                    EntityObjectModel.entity_schema_id == schema.id,
                    EntityObjectModel.status != "archived",
                )
                .order_by(EntityObjectModel.created_at)
                .limit(settings.excel_max_rows)
            )
        ).all()
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Объекты"
        fields = [field for field in schema.fields if not field.archived]
        sheet.append(
            [
                "__objectId",
                "__revision",
                "__schemaVersionId",
                "__action",
                *[field.id.hex for field in fields],
            ]
        )
        sheet.append(
            [
                "ID объекта",
                "Ревизия",
                "Версия схемы",
                "Действие",
                *[field.name for field in fields],
            ]
        )
        for item in objects:
            sheet.append(
                [
                    str(item.id),
                    item.revision,
                    str(current_schema_version_id),
                    "",
                    *[self._safe_excel_value(item.values.get(field.code)) for field in fields],
                ]
            )
        buffer = io.BytesIO()
        workbook.save(buffer)
        return f"{schema.code}.xlsx", buffer.getvalue()

    def objects_json(self, objects: list[EntityObjectCreate]) -> bytes:
        return orjson.dumps([item.model_dump(mode="json", by_alias=True) for item in objects])

    def _workbooks(self, content: bytes):
        if not content:
            raise ExcelValidationError("XLSX-файл пуст")
        if len(content) > settings.excel_max_file_size_bytes:
            raise ExcelValidationError("XLSX-файл превышает допустимый размер")
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if sum(item.file_size for item in archive.infolist()) > settings.excel_max_uncompressed_bytes:
                    raise ExcelValidationError("Распакованный XLSX превышает допустимый размер")
            formula_workbook = load_workbook(
                io.BytesIO(content), read_only=True, data_only=False, keep_links=False
            )
            value_workbook = load_workbook(
                io.BytesIO(content), read_only=True, data_only=True, keep_links=False
            )
        except (zipfile.BadZipFile, OSError, ValueError) as error:
            raise ExcelValidationError("Файл не является корректной книгой XLSX") from error
        if len(formula_workbook.sheetnames) > settings.excel_max_sheets:
            raise ExcelValidationError("В XLSX слишком много листов")
        for sheet in formula_workbook.worksheets:
            max_row = sheet.max_row or 0
            max_column = sheet.max_column or 0
            if max_row > settings.excel_max_rows + 2:
                raise ExcelValidationError(f"Лист «{sheet.title}» превышает лимит строк")
            if max_column > settings.excel_max_columns:
                raise ExcelValidationError(f"Лист «{sheet.title}» превышает лимит столбцов")
        return formula_workbook, value_workbook

    @staticmethod
    def _sheet(workbook, sheet_name: str | None) -> Worksheet:
        if sheet_name is None:
            return workbook[workbook.sheetnames[0]]
        if sheet_name not in workbook.sheetnames:
            raise ExcelValidationError(f"Лист «{sheet_name}» не найден")
        return workbook[sheet_name]

    @staticmethod
    def _headers(sheet: Worksheet, header_row: int) -> list[str]:
        if header_row > (sheet.max_row or 0):
            raise ExcelValidationError("Строка заголовков находится за пределами листа")
        headers = [str(cell.value).strip() if cell.value is not None else "" for cell in sheet[header_row]]
        if not any(headers):
            raise ExcelValidationError("Строка заголовков пуста")
        return headers

    @staticmethod
    def _propose_mapping(headers: list[str], fields: list[EntityFieldModel]) -> dict[str, str]:
        by_token: dict[str, str] = {}
        for field in fields:
            if field.archived or field.read_only:
                continue
            by_token[field.code.casefold()] = field.code
            by_token[field.name.strip().casefold()] = field.code
            by_token[field.id.hex.casefold()] = field.code
        return {
            header: by_token[token]
            for header in headers
            if header and (token := header.strip().casefold()) in by_token
        }

    @staticmethod
    def _row_values(
        formula_sheet: Worksheet,
        value_sheet: Worksheet,
        row_number: int,
        headers: list[str],
        mapping: dict[str, str],
    ) -> tuple[dict[str, object], list[ExcelCellIssue]]:
        values: dict[str, object] = {}
        issues: list[ExcelCellIssue] = []
        formula_row = formula_sheet[row_number]
        value_row = value_sheet[row_number]
        for index, header in enumerate(headers):
            field_code = mapping.get(header)
            if not field_code or index >= len(formula_row):
                continue
            cell: Cell = formula_row[index]
            value_cell: Cell = value_row[index]
            if cell.data_type == "f":
                if value_cell.value is None:
                    issues.append(
                        ExcelCellIssue(
                            sheet=formula_sheet.title,
                            row=row_number,
                            column=cell.coordinate,
                            field_code=field_code,
                            code="formula_result_missing",
                            message=(
                                "В файле нет сохранённого результата формулы; "
                                "пересчитайте и сохраните книгу в Excel или LibreOffice"
                            ),
                        )
                    )
                    continue
                value = value_cell.value
            else:
                value = cell.value
            if value is None:
                continue
            if isinstance(value, datetime | date):
                value = value.isoformat()
            elif isinstance(value, Decimal):
                value = str(value)
            values[field_code] = value
        return values, issues

    @staticmethod
    def _safe_excel_value(value: object) -> object:
        if isinstance(value, list | dict):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    @staticmethod
    def _import_cell_value(
        formula_cell: Cell,
        value_cell: Cell,
        sheet_name: str,
        row_number: int,
    ) -> object:
        if formula_cell.data_type == "f":
            if value_cell.value is None:
                raise ExcelValidationError(
                    f"{sheet_name}, строка {row_number}, столбец {formula_cell.coordinate}: "
                    "нет сохранённого результата формулы"
                )
            value = value_cell.value
        else:
            value = formula_cell.value
        if isinstance(value, datetime | date):
            return value.isoformat()
        if isinstance(value, Decimal):
            return format(value, "f")
        return value

    async def _schema(self, entity_code: str) -> EntitySchemaModel:
        schema = await self._session.scalar(
            select(EntitySchemaModel).where(
                EntitySchemaModel.code == entity_code,
                EntitySchemaModel.status == "active",
            )
        )
        if schema is None:
            raise RuntimeEntityNotFound
        return schema

    async def _schema_version_id(self, entity_id: UUID) -> UUID:
        version_id = await self._session.scalar(
            select(EntitySchemaVersionModel.id)
            .where(EntitySchemaVersionModel.entity_schema_id == entity_id)
            .order_by(EntitySchemaVersionModel.version.desc())
            .limit(1)
        )
        if version_id is None:
            raise ExcelValidationError("У сущности отсутствует опубликованная версия схемы")
        return version_id


class ImportProfileService:
    """Сохраняет подтверждённые пользователем Excel-сопоставления."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, payload: ImportProfileCreate, owner_id: UUID) -> ImportProfileRead:
        async with self._session.begin():
            model = ImportProfileModel(
                entity_schema_id=payload.entity_schema_id,
                owner_id=owner_id,
                name=payload.name,
                sheet_name=payload.mapping.sheet_name,
                header_row=payload.mapping.header_row,
                mapping=payload.mapping.columns,
            )
            self._session.add(model)
            await self._session.flush()
            await self._session.refresh(model)
        return ImportProfileRead.model_validate(model, from_attributes=True)

    async def list(self, entity_id: UUID, owner_id: UUID) -> list[ImportProfileRead]:
        rows = (
            await self._session.scalars(
                select(ImportProfileModel)
                .where(
                    ImportProfileModel.entity_schema_id == entity_id,
                    ImportProfileModel.owner_id == owner_id,
                )
                .order_by(ImportProfileModel.name)
            )
        ).all()
        return [ImportProfileRead.model_validate(row, from_attributes=True) for row in rows]

    async def delete(self, profile_id: UUID, owner_id: UUID) -> None:
        async with self._session.begin():
            result = await self._session.execute(
                delete(ImportProfileModel).where(
                    ImportProfileModel.id == profile_id,
                    ImportProfileModel.owner_id == owner_id,
                )
            )
            if result.rowcount == 0:
                raise ExcelValidationError("Шаблон импорта не найден")
