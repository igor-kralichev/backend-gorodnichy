from slugify import slugify
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.dictionaries.api.schemas import (
    DictionaryCreate,
    DictionaryDeleteRead,
    DictionaryExcelImportRead,
    DictionaryExcelPreviewRead,
    DictionaryExcelPreviewRow,
    DictionaryItemCreate,
    DictionaryItemMutation,
    DictionaryItemRead,
    DictionaryItemUpdate,
    DictionaryListRead,
    DictionaryRead,
    DictionaryStatusRead,
    DictionaryUpdate,
)
from app.modules.dictionaries.domain.errors import (
    DictionaryAlreadyExists,
    DictionaryConflict,
    DictionaryEntityNotFound,
    DictionaryItemNotFound,
    DictionaryNotFound,
)
from app.modules.entities.infrastructure.models import EntitySchemaModel
from app.modules.excel.application.service import ExcelService, ExcelValidationError
from app.shared.db.models import DictionaryItemModel, DictionaryModel, OrganizationModel


class DictionaryService:
    """Сервис управления справочниками сущностей."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_page(
        self,
        *,
        entity_id: object | None,
        include_shared: bool,
        can_manage: bool,
        visible_organization_ids: set[object],
        scope: str | None,
        owner_organization_id: object | None,
        include_archived: bool,
        query: str | None,
        sort: str,
        limit: int,
        offset: int,
    ) -> DictionaryListRead:
        conditions = []
        if entity_id is not None:
            if include_shared:
                entity_owner_id = await self.session.scalar(
                    select(EntitySchemaModel.owner_organization_id).where(
                        EntitySchemaModel.id == entity_id
                    )
                )
                shared_conditions = [
                    DictionaryModel.entity_schema_id == entity_id,
                    DictionaryModel.scope == "global",
                ]
                if entity_owner_id is not None:
                    shared_conditions.append(
                        (DictionaryModel.scope == "organization")
                        & (DictionaryModel.owner_organization_id == entity_owner_id)
                    )
                conditions.append(or_(*shared_conditions))
            else:
                conditions.append(DictionaryModel.entity_schema_id == entity_id)
        elif not can_manage:
            visibility = [DictionaryModel.scope == "global"]
            if visible_organization_ids:
                visibility.append(
                    (DictionaryModel.scope == "organization")
                    & (DictionaryModel.owner_organization_id.in_(visible_organization_ids))
                )
            conditions.append(or_(*visibility))
        if scope is not None:
            conditions.append(DictionaryModel.scope == scope)
        if owner_organization_id is not None:
            conditions.append(DictionaryModel.owner_organization_id == owner_organization_id)
        if not include_archived:
            conditions.append(DictionaryModel.active.is_(True))
        if query:
            pattern = f"%{query.strip()}%"
            conditions.append(
                or_(DictionaryModel.name.ilike(pattern), DictionaryModel.code.ilike(pattern))
            )
        sort_expressions = {
            "name": DictionaryModel.name.asc(),
            "-name": DictionaryModel.name.desc(),
            "updatedAt": DictionaryModel.updated_at.asc(),
            "-updatedAt": DictionaryModel.updated_at.desc(),
        }

        total = int(
            await self.session.scalar(select(func.count()).select_from(DictionaryModel).where(*conditions)) or 0
        )
        dictionaries = (
            await self.session.scalars(
                select(DictionaryModel)
                .where(*conditions)
                .order_by(sort_expressions[sort])
                .offset(offset)
                .limit(limit)
            )
        ).all()
        return DictionaryListRead(
            items=[await self._to_response(dictionary) for dictionary in dictionaries],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def create(self, payload: DictionaryCreate) -> DictionaryRead:
        if payload.entity_id is not None:
            entity_exists = await self.session.scalar(
                select(EntitySchemaModel.id).where(EntitySchemaModel.id == payload.entity_id)
            )
            if entity_exists is None:
                raise DictionaryEntityNotFound("Сущность для справочника не найдена")
        if payload.owner_organization_id is not None:
            organization_exists = await self.session.scalar(
                select(OrganizationModel.id).where(
                    OrganizationModel.id == payload.owner_organization_id,
                    OrganizationModel.active.is_(True),
                )
            )
            if organization_exists is None:
                raise DictionaryEntityNotFound("Организация справочника не найдена или неактивна")

        dictionary_code = payload.code or _code_from_name(payload.name)
        await self._ensure_code_available(
            payload.scope,
            payload.entity_id,
            payload.owner_organization_id,
            dictionary_code,
        )

        dictionary = DictionaryModel(
            entity_schema_id=payload.entity_id,
            scope=payload.scope,
            owner_organization_id=payload.owner_organization_id,
            revision=1,
            code=dictionary_code,
            name=payload.name,
            active=True,
        )
        self.session.add(dictionary)
        await self.session.flush()

        self._add_items(dictionary.id, payload.items)

        await self.session.commit()
        return await self.get(dictionary.id)

    async def get(self, dictionary_id: object) -> DictionaryRead:
        dictionary = await self._get_model(dictionary_id)
        return await self._to_response(dictionary)

    async def update(self, dictionary_id: object, payload: DictionaryUpdate) -> DictionaryRead:
        async with self.session.begin():
            dictionary = await self._get_model(dictionary_id, for_update=True)
            self._ensure_revision(dictionary, payload.revision)
            if payload.code is not None and payload.code != dictionary.code:
                await self._ensure_code_available(
                    dictionary.scope,
                    dictionary.entity_schema_id,
                    dictionary.owner_organization_id,
                    payload.code,
                    exclude_id=dictionary.id,
                )
                dictionary.code = payload.code
            if payload.name is not None:
                dictionary.name = payload.name
            if payload.items is not None:
                await self.session.execute(
                    delete(DictionaryItemModel).where(DictionaryItemModel.dictionary_id == dictionary.id)
                )
                self._add_items(dictionary.id, payload.items)
            dictionary.revision += 1
            await self.session.flush()
            await self.session.refresh(dictionary, attribute_names=["updated_at"])
        return await self.get(dictionary.id)

    async def archive(self, dictionary_id: object) -> DictionaryStatusRead:
        async with self.session.begin():
            dictionary = await self._get_model(dictionary_id, for_update=True)
            dictionary.active = False
            dictionary.revision += 1
            await self.session.flush()
        return DictionaryStatusRead(id=dictionary.id, active=dictionary.active, revision=dictionary.revision)

    async def restore(self, dictionary_id: object) -> DictionaryStatusRead:
        async with self.session.begin():
            dictionary = await self._get_model(dictionary_id, for_update=True)
            dictionary.active = True
            dictionary.revision += 1
            await self.session.flush()
        return DictionaryStatusRead(id=dictionary.id, active=dictionary.active, revision=dictionary.revision)

    async def add_item(
        self,
        dictionary_id: object,
        payload: DictionaryItemMutation,
    ) -> DictionaryRead:
        async with self.session.begin():
            dictionary = await self._get_model(dictionary_id, for_update=True)
            self._ensure_revision(dictionary, payload.revision)
            existing_codes = set(
                await self.session.scalars(
                    select(DictionaryItemModel.code).where(
                        DictionaryItemModel.dictionary_id == dictionary.id
                    )
                )
            )
            item_code = payload.item.code or _unique_code(payload.item.name, existing_codes)
            sort_order = payload.item.sort_order
            if "sort_order" not in payload.item.model_fields_set:
                sort_order = int(
                    await self.session.scalar(
                        select(func.coalesce(func.max(DictionaryItemModel.sort_order), 0)).where(
                            DictionaryItemModel.dictionary_id == dictionary.id
                        )
                    )
                    or 0
                ) + 1
            self.session.add(
                DictionaryItemModel(
                    dictionary_id=dictionary.id,
                    code=item_code,
                    name=payload.item.name,
                    normalized_name=_normalized_item_name(payload.item.name),
                    active=payload.item.active,
                    sort_order=sort_order,
                )
            )
            dictionary.revision += 1
            try:
                await self.session.flush()
            except IntegrityError as error:
                raise DictionaryConflict(
                    "Элемент с таким кодом или названием уже существует"
                ) from error
        return await self.get(dictionary.id)

    async def update_item(
        self,
        dictionary_id: object,
        item_id: object,
        payload: DictionaryItemUpdate,
    ) -> DictionaryRead:
        async with self.session.begin():
            dictionary = await self._get_model(dictionary_id, for_update=True)
            self._ensure_revision(dictionary, payload.revision)
            item = await self.session.scalar(
                select(DictionaryItemModel).where(
                    DictionaryItemModel.id == item_id,
                    DictionaryItemModel.dictionary_id == dictionary.id,
                ).with_for_update()
            )
            if item is None:
                raise DictionaryItemNotFound("Элемент справочника не найден")
            for field in ("code", "name", "active", "sort_order"):
                if field in payload.model_fields_set:
                    setattr(item, field, getattr(payload, field))
            if "name" in payload.model_fields_set:
                item.normalized_name = _normalized_item_name(item.name)
            dictionary.revision += 1
            try:
                await self.session.flush()
            except IntegrityError as error:
                raise DictionaryConflict(
                    "Элемент с таким кодом или названием уже существует"
                ) from error
        return await self.get(dictionary.id)

    async def delete_item(
        self,
        dictionary_id: object,
        item_id: object,
        revision: int,
    ) -> DictionaryRead:
        async with self.session.begin():
            dictionary = await self._get_model(dictionary_id, for_update=True)
            self._ensure_revision(dictionary, revision)
            item = await self.session.scalar(
                select(DictionaryItemModel).where(
                    DictionaryItemModel.id == item_id,
                    DictionaryItemModel.dictionary_id == dictionary.id,
                ).with_for_update()
            )
            if item is None:
                raise DictionaryItemNotFound("Элемент справочника не найден")
            await self.session.delete(item)
            dictionary.revision += 1
        return await self.get(dictionary.id)

    async def preview_excel(
        self,
        dictionary_id: object,
        content: bytes,
        *,
        sheet_name: str | None,
        header_row: int,
        value_column: str | None,
        preview_rows: int,
    ) -> DictionaryExcelPreviewRead:
        dictionary = await self._get_model(dictionary_id)
        parsed = self._excel_rows(
            content,
            sheet_name=sheet_name,
            header_row=header_row,
            value_column=value_column,
        )
        existing = set(
            await self.session.scalars(
                select(DictionaryItemModel.normalized_name).where(
                    DictionaryItemModel.dictionary_id == dictionary.id
                )
            )
        )
        seen: set[str] = set()
        rows: list[DictionaryExcelPreviewRow] = []
        counts = {"new": 0, "existing": 0, "duplicate": 0, "empty": 0}
        for row_number, value, error in parsed["values"]:
            if error:
                state = "error"
            elif value is None:
                state = "empty"
                counts["empty"] += 1
            else:
                normalized = _normalized_item_name(value)
                if normalized in seen:
                    state = "duplicate"
                    counts["duplicate"] += 1
                elif normalized in existing:
                    state = "existing"
                    counts["existing"] += 1
                else:
                    state = "new"
                    counts["new"] += 1
                seen.add(normalized)
            if len(rows) < preview_rows:
                rows.append(
                    DictionaryExcelPreviewRow(
                        row=row_number,
                        value=value,
                        state=state,
                        message=error,
                    )
                )
        return DictionaryExcelPreviewRead(
            sheets=parsed["sheets"],
            selected_sheet=parsed["selected_sheet"],
            header_row=header_row,
            headers=parsed["headers"],
            value_column=parsed["value_column"],
            total_rows=len(parsed["values"]),
            new_count=counts["new"],
            existing_count=counts["existing"],
            duplicate_count=counts["duplicate"],
            empty_count=counts["empty"],
            rows=rows,
        )

    async def import_excel(
        self,
        dictionary_id: object,
        content: bytes,
        *,
        revision: int,
        sheet_name: str | None,
        header_row: int,
        value_column: str | None,
    ) -> DictionaryExcelImportRead:
        parsed = self._excel_rows(
            content,
            sheet_name=sheet_name,
            header_row=header_row,
            value_column=value_column,
        )
        errors = [error for _, _, error in parsed["values"] if error]
        if errors:
            raise ExcelValidationError(errors[0])
        created = existing_count = duplicate = empty = 0
        async with self.session.begin():
            dictionary = await self._get_model(dictionary_id, for_update=True)
            self._ensure_revision(dictionary, revision)
            existing_names = set(
                await self.session.scalars(
                    select(DictionaryItemModel.normalized_name).where(
                        DictionaryItemModel.dictionary_id == dictionary.id
                    )
                )
            )
            existing_codes = set(
                await self.session.scalars(
                    select(DictionaryItemModel.code).where(
                        DictionaryItemModel.dictionary_id == dictionary.id
                    )
                )
            )
            seen: set[str] = set()
            next_order = int(
                await self.session.scalar(
                    select(func.coalesce(func.max(DictionaryItemModel.sort_order), 0)).where(
                        DictionaryItemModel.dictionary_id == dictionary.id
                    )
                )
                or 0
            )
            for _, value, _ in parsed["values"]:
                if value is None:
                    empty += 1
                    continue
                normalized = _normalized_item_name(value)
                if normalized in seen:
                    duplicate += 1
                    continue
                seen.add(normalized)
                if normalized in existing_names:
                    existing_count += 1
                    continue
                next_order += 1
                item_code = _unique_code(value, existing_codes)
                existing_codes.add(item_code)
                existing_names.add(normalized)
                self.session.add(
                    DictionaryItemModel(
                        dictionary_id=dictionary.id,
                        code=item_code,
                        name=value,
                        normalized_name=normalized,
                        active=True,
                        sort_order=next_order,
                    )
                )
                created += 1
            if created:
                dictionary.revision += 1
            await self.session.flush()
        response = await self.get(dictionary.id)
        return DictionaryExcelImportRead(
            dictionary=response,
            created_count=created,
            existing_count=existing_count,
            duplicate_count=duplicate,
            empty_count=empty,
            metadata={"mode": "merge", "deleted": 0},
        )

    def _excel_rows(
        self,
        content: bytes,
        *,
        sheet_name: str | None,
        header_row: int,
        value_column: str | None,
    ) -> dict:
        formula_book, value_book = ExcelService(self.session)._workbooks(content)  # noqa: SLF001
        formula_sheet = ExcelService._sheet(formula_book, sheet_name)  # noqa: SLF001
        value_sheet = ExcelService._sheet(value_book, formula_sheet.title)  # noqa: SLF001
        headers = ExcelService._headers(formula_sheet, header_row)  # noqa: SLF001
        selected = value_column or next((header for header in headers if header), None)
        if selected is None or selected not in headers:
            raise ExcelValidationError("Выбранный столбец значений не найден")
        if headers.count(selected) != 1:
            raise ExcelValidationError("Заголовок выбранного столбца должен быть уникальным")
        index = headers.index(selected)
        values: list[tuple[int, str | None, str | None]] = []
        for row_number in range(header_row + 1, (formula_sheet.max_row or 0) + 1):
            formula_cell = formula_sheet[row_number][index]
            value_cell = value_sheet[row_number][index]
            if formula_cell.data_type == "f" and value_cell.value is None:
                values.append(
                    (row_number, None, f"Строка {row_number}: отсутствует сохранённый результат формулы")
                )
                continue
            raw = value_cell.value if formula_cell.data_type == "f" else formula_cell.value
            value = None if raw is None or not str(raw).strip() else str(raw).strip()
            values.append((row_number, value, None))
        return {
            "sheets": formula_book.sheetnames,
            "selected_sheet": formula_sheet.title,
            "headers": headers,
            "value_column": selected,
            "values": values,
        }

    async def delete(self, dictionary_id: object) -> DictionaryDeleteRead:
        dictionary = await self._get_model(dictionary_id)
        await self.session.delete(dictionary)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise
        return DictionaryDeleteRead(id=dictionary.id, deleted=True)

    async def _get_model(self, dictionary_id: object, *, for_update: bool = False) -> DictionaryModel:
        statement = select(DictionaryModel).where(DictionaryModel.id == dictionary_id)
        if for_update:
            statement = statement.with_for_update()
        dictionary = await self.session.scalar(statement)
        if dictionary is None:
            raise DictionaryNotFound("Справочник не найден")
        return dictionary

    async def _to_response(self, dictionary: DictionaryModel) -> DictionaryRead:
        items = (
            await self.session.scalars(
                select(DictionaryItemModel)
                .where(DictionaryItemModel.dictionary_id == dictionary.id)
                .order_by(DictionaryItemModel.sort_order, DictionaryItemModel.name)
            )
        ).all()
        return DictionaryRead(
            id=dictionary.id,
            scope=dictionary.scope,
            entity_id=dictionary.entity_schema_id,
            owner_organization_id=dictionary.owner_organization_id,
            revision=dictionary.revision,
            code=dictionary.code,
            name=dictionary.name,
            active=dictionary.active,
            items=[
                DictionaryItemRead(
                    id=item.id,
                    code=item.code,
                    name=item.name,
                    active=item.active,
                    sort_order=item.sort_order,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
                for item in items
            ],
            created_at=dictionary.created_at,
            updated_at=dictionary.updated_at,
        )

    async def _ensure_code_available(
        self,
        scope: str,
        entity_id: object | None,
        owner_organization_id: object | None,
        code: str,
        *,
        exclude_id: object | None = None,
    ) -> None:
        conditions = [DictionaryModel.scope == scope, DictionaryModel.code == code]
        if scope == "entity":
            conditions.append(DictionaryModel.entity_schema_id == entity_id)
        elif scope == "organization":
            conditions.append(DictionaryModel.owner_organization_id == owner_organization_id)
        if exclude_id is not None:
            conditions.append(DictionaryModel.id != exclude_id)
        code_exists = await self.session.scalar(select(DictionaryModel.id).where(*conditions))
        if code_exists is not None:
            raise DictionaryAlreadyExists("Справочник с таким кодом уже существует в выбранной области")

    @staticmethod
    def _ensure_revision(dictionary: DictionaryModel, expected: int) -> None:
        if dictionary.revision != expected:
            raise DictionaryConflict(
                f"Справочник уже изменён: ожидалась ревизия {expected}, "
                f"текущая — {dictionary.revision}"
            )

    def _add_items(self, dictionary_id: object, items: list[DictionaryItemCreate]) -> None:
        used_item_codes: set[str] = set()
        used_item_names: set[str] = set()
        for index, item in enumerate(items, start=1):
            item_code = item.code or _unique_code(item.name, used_item_codes)
            if item_code in used_item_codes:
                raise ValueError("Коды элементов справочника должны быть уникальными")
            used_item_codes.add(item_code)
            normalized_name = _normalized_item_name(item.name)
            if normalized_name in used_item_names:
                raise ValueError("Названия элементов справочника должны быть уникальными без учёта регистра и пробелов по краям")
            used_item_names.add(normalized_name)
            sort_order = item.sort_order if "sort_order" in item.model_fields_set else index
            self.session.add(
                DictionaryItemModel(
                    dictionary_id=dictionary_id,
                    code=item_code,
                    name=item.name,
                    normalized_name=normalized_name,
                    active=item.active,
                    sort_order=sort_order,
                )
            )


def _code_from_name(name: str) -> str:
    return slugify(name, separator="_") or "dictionary"


def _normalized_item_name(name: str) -> str:
    return name.strip().casefold()


def _unique_code(name: str, used_codes: set[str]) -> str:
    base_code = _code_from_name(name)
    code = base_code
    suffix = 2
    while code in used_codes:
        code = f"{base_code}_{suffix}"
        suffix += 1
    return code
