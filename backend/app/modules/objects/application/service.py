from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from typing import cast as type_cast
from uuid import UUID, uuid4

from sqlalchemy import Date, DateTime, Numeric, String, and_, cast, delete, func, not_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.entities.domain.enums import FieldType
from app.modules.entities.infrastructure.models import EntityFieldModel, EntitySchemaModel
from app.modules.objects.api.schemas import (
    EntityObjectBulkCreateRead,
    EntityObjectCreate,
    EntityObjectDeleteRead,
    EntityObjectPage,
    EntityObjectPatch,
    EntityObjectRead,
    EntityObjectStatusRead,
    GeoJsonGeometry,
    ObjectFilter,
    ObjectSearch,
    ValidationIssue,
)
from app.shared.db.models import (
    AttachmentModel,
    DictionaryItemModel,
    EntityObjectModel,
    ImportRowModel,
    ObjectSearchIndexModel,
    ObjectEventModel,
    OutboxEventModel,
)


class RuntimeEntityNotFound(Exception):
    pass


class RuntimeObjectNotFound(Exception):
    pass


class RuntimeValidationError(Exception):
    def __init__(self, issues: list[dict[str, str | None]]) -> None:
        super().__init__("Данные объекта не прошли проверку")
        self.issues = issues


class RevisionConflict(Exception):
    def __init__(self, current_revision: int) -> None:
        super().__init__("Конфликт ревизий объекта")
        self.current_revision = current_revision


class RuntimeObjectService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._dictionary_lookup_cache: dict[UUID, dict[str, Any]] = {}

    async def create(self, entity_code: str, payload: EntityObjectCreate, actor_id: UUID | None) -> EntityObjectRead:
        response = await self.create_many(entity_code, [payload], actor_id)
        return response.data[0]

    async def create_many(
        self,
        entity_code: str,
        payloads: list[EntityObjectCreate],
        actor_id: UUID | None,
    ) -> EntityObjectBulkCreateRead:
        if not payloads:
            raise RuntimeValidationError(
                [
                    {
                        "fieldCode": None,
                        "code": "empty_payload",
                        "message": "Необходимо передать хотя бы один объект",
                    }
                ]
            )
        created: list[tuple[EntityObjectModel, EntitySchemaModel, GeoJsonGeometry | None]] = []
        async with self._session.begin():
            schema = await self._get_schema(entity_code)
            for payload in payloads:
                parent_object_id = await self._resolve_parent_object_id(schema, payload.parent_object_id)
                values = await self._normalize_values(schema, dict(payload.values))
                issues = await self._validate(schema, values, payload.geometry)
                object_id = uuid4()
                model = EntityObjectModel(
                    id=object_id,
                    entity_schema_id=schema.id,
                    parent_object_id=parent_object_id,
                    municipality_id=schema.scope_municipality_id,
                    values=values,
                    geometry=self._geometry_expression(payload.geometry),
                    attachment_paths=[],
                    status="published" if not issues else "draft",
                    data_quality="complete" if not issues else "incomplete",
                    validation_errors=issues,
                    revision=1,
                    created_by=actor_id,
                    updated_by=actor_id,
                )
                self._session.add(model)
                self._session.add(
                    ObjectEventModel(
                        entity_schema_id=schema.id,
                        object_id=object_id,
                        revision=1,
                        event_type="object.created",
                        actor_id=actor_id,
                        before_values=None,
                        after_values=values,
                        changes=self._changes({}, values, schema.fields),
                        metadata_json={
                            "dataQuality": model.data_quality,
                            "status": model.status,
                            "parentObjectId": str(parent_object_id) if parent_object_id else None,
                        },
                    )
                )
                self._add_outbox("entity_object.created.v1", model, schema.code, actor_id)
                created.append((model, schema, payload.geometry))
            await self._session.flush()
            for model, schema, _ in created:
                await self._sync_search_index(model, schema, model.values)
        data = [
            await self._to_response(model, schema, geometry)
            for model, schema, geometry in created
        ]
        return EntityObjectBulkCreateRead(
            created=len(data),
            published=sum(item.status == "published" for item in data),
            draft=sum(item.status == "draft" for item in data),
            data=data,
        )

    async def get(self, entity_code: str, object_id: UUID) -> EntityObjectRead:
        schema = await self._get_schema(entity_code)
        model, geometry = await self._get_object(schema.id, object_id)
        return await self._to_response(model, schema, geometry)

    async def list_page(
        self,
        entity_code: str,
        search: ObjectSearch,
    ) -> EntityObjectPage:
        schema = await self._get_schema(entity_code)
        conditions = [EntityObjectModel.entity_schema_id == schema.id, EntityObjectModel.status != "archived"]
        if search.parent_object_id is not None:
            conditions.append(EntityObjectModel.parent_object_id == search.parent_object_id)
        filter_conditions = [await self._filter_condition(schema, item) for item in search.filters]
        if filter_conditions:
            conditions.append(or_(*filter_conditions) if search.logic == "or" else and_(*filter_conditions))

        total = int(
            await self._session.scalar(select(func.count()).select_from(EntityObjectModel).where(*conditions)) or 0
        )
        geometry_json = func.ST_AsGeoJSON(EntityObjectModel.geometry).label("geometry_json")
        statement = (
            select(EntityObjectModel, geometry_json)
            .where(*conditions)
            .order_by(self._sort_expression(schema, search.sort))
            .offset(search.offset)
            .limit(search.limit)
        )
        rows = (await self._session.execute(statement)).all()
        data = [
            await self._to_response(model, schema, self._parse_geometry(geometry))
            for model, geometry in rows
        ]
        return EntityObjectPage(total=total, returned=len(data), offset=search.offset, limit=search.limit, data=data)

    async def update(
        self,
        entity_code: str,
        object_id: UUID,
        payload: EntityObjectPatch,
        actor_id: UUID | None,
    ) -> EntityObjectRead:
        async with self._session.begin():
            schema = await self._get_schema(entity_code)
            model, current_geometry = await self._get_object(schema.id, object_id, for_update=True)
            if model.status == "archived":
                raise RuntimeObjectNotFound
            if payload.revision is not None and payload.revision != model.revision:
                raise RevisionConflict(model.revision)

            before_values = dict(model.values)
            values = await self._normalize_values(schema, {**before_values, **(payload.values or {})})
            geometry_changed = "geometry" in payload.model_fields_set
            next_geometry = payload.geometry if geometry_changed else current_geometry
            issues = await self._validate(schema, values, next_geometry)

            model.values = values
            if geometry_changed:
                model.geometry = self._geometry_expression(payload.geometry)
            model.status = "published" if not issues else "draft"
            model.data_quality = "complete" if not issues else "incomplete"
            model.validation_errors = issues
            model.revision += 1
            model.updated_by = actor_id
            self._session.add(
                ObjectEventModel(
                    entity_schema_id=schema.id,
                    object_id=model.id,
                    revision=model.revision,
                    event_type="object.updated",
                    actor_id=actor_id,
                    before_values=before_values,
                    after_values=values,
                    changes=self._changes(before_values, values, schema.fields),
                    metadata_json={"dataQuality": model.data_quality, "status": model.status},
                )
            )
            self._add_outbox("entity_object.updated.v1", model, schema.code, actor_id)
            await self._session.flush()
            await self._sync_search_index(model, schema, values)
            await self._session.refresh(model, attribute_names=["updated_at"])
        return await self._to_response(model, schema, next_geometry)

    async def archive(self, entity_code: str, object_id: UUID, actor_id: UUID | None) -> EntityObjectStatusRead:
        async with self._session.begin():
            schema = await self._get_schema(entity_code)
            model, _ = await self._get_object(schema.id, object_id, for_update=True)
            if model.status != "archived":
                model.status = "archived"
                model.revision += 1
                model.updated_by = actor_id
                self._session.add(
                    ObjectEventModel(
                        entity_schema_id=schema.id,
                        object_id=model.id,
                        revision=model.revision,
                        event_type="object.archived",
                        actor_id=actor_id,
                        before_values=dict(model.values),
                        after_values=dict(model.values),
                        changes=[],
                        metadata_json={"status": "archived"},
                    )
                )
                self._add_outbox("entity_object.archived.v1", model, schema.code, actor_id)
                await self._session.flush()
        return EntityObjectStatusRead(
            id=model.id,
            status=model.status,
            data_quality=model.data_quality,
            revision=model.revision,
        )

    async def restore(self, entity_code: str, object_id: UUID, actor_id: UUID | None) -> EntityObjectStatusRead:
        async with self._session.begin():
            schema = await self._get_schema(entity_code)
            model, geometry = await self._get_object(schema.id, object_id, for_update=True)
            if model.status != "archived":
                return EntityObjectStatusRead(
                    id=model.id,
                    status=model.status,
                    data_quality=model.data_quality,
                    revision=model.revision,
                )
            values = await self._normalize_values(schema, dict(model.values))
            issues = await self._validate(schema, values, geometry)
            model.values = values
            model.status = "published" if not issues else "draft"
            model.data_quality = "complete" if not issues else "incomplete"
            model.validation_errors = issues
            model.revision += 1
            model.updated_by = actor_id
            self._session.add(
                ObjectEventModel(
                    entity_schema_id=schema.id,
                    object_id=model.id,
                    revision=model.revision,
                    event_type="object.restored",
                    actor_id=actor_id,
                    before_values=dict(model.values),
                    after_values=values,
                    changes=[],
                    metadata_json={"dataQuality": model.data_quality, "status": model.status},
                )
            )
            self._add_outbox("entity_object.restored.v1", model, schema.code, actor_id)
            await self._session.flush()
            await self._sync_search_index(model, schema, values)
        return EntityObjectStatusRead(
            id=model.id,
            status=model.status,
            data_quality=model.data_quality,
            revision=model.revision,
        )

    async def delete(self, entity_code: str, object_id: UUID, actor_id: UUID | None) -> EntityObjectDeleteRead:
        async with self._session.begin():
            schema = await self._get_schema(entity_code)
            model, _ = await self._get_object(schema.id, object_id, for_update=True)
            has_children = bool(
                await self._session.scalar(
                    select(EntityObjectModel.id).where(
                        EntityObjectModel.parent_object_id == model.id,
                    ).limit(1)
                )
            )
            if has_children:
                raise RuntimeValidationError(
                    [
                        {
                            "fieldCode": None,
                            "code": "object_has_children",
                            "message": "Нельзя удалить объект, у которого есть вложенные объекты",
                        }
                    ]
                )
            await self._session.execute(
                update(ImportRowModel)
                .where(ImportRowModel.object_id == model.id)
                .values(object_id=None)
            )
            await self._session.execute(
                delete(AttachmentModel).where(AttachmentModel.object_id == model.id)
            )
            await self._session.execute(
                delete(ObjectEventModel).where(ObjectEventModel.object_id == model.id)
            )
            await self._session.execute(
                delete(ObjectSearchIndexModel).where(ObjectSearchIndexModel.object_id == model.id)
            )
            await self._session.execute(
                delete(OutboxEventModel).where(
                    OutboxEventModel.aggregate_type == "entity_object",
                    OutboxEventModel.aggregate_id == model.id,
                )
            )
            await self._session.delete(model)
            self._session.add(
                OutboxEventModel(
                    aggregate_type="entity_object",
                    aggregate_id=object_id,
                    event_type="entity_object.deleted.v1",
                    payload={
                        "objectId": str(object_id),
                        "entityId": str(schema.id),
                        "entityCode": schema.code,
                        "actorId": str(actor_id) if actor_id else None,
                    },
                )
            )
            await self._session.flush()
        return EntityObjectDeleteRead(id=object_id, deleted=True)

    async def _get_schema(self, entity_code: str) -> EntitySchemaModel:
        schema = await self._session.scalar(
            select(EntitySchemaModel).where(EntitySchemaModel.code == entity_code, EntitySchemaModel.status == "active")
        )
        if schema is None:
            raise RuntimeEntityNotFound
        return schema

    async def _get_object(
        self,
        entity_id: UUID,
        object_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[EntityObjectModel, GeoJsonGeometry | None]:
        statement = select(EntityObjectModel, func.ST_AsGeoJSON(EntityObjectModel.geometry)).where(
            EntityObjectModel.entity_schema_id == entity_id,
            EntityObjectModel.id == object_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            raise RuntimeObjectNotFound
        return row[0], self._parse_geometry(row[1])

    async def _normalize_values(
        self,
        schema: EntitySchemaModel,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = dict(values)
        fields = {field.code: field for field in schema.fields}
        for field_code, value in values.items():
            field = fields.get(field_code)
            if field is None:
                continue
            if isinstance(value, str):
                normalized[field_code] = value.strip()
                value = normalized[field_code]
            if field.field_type != FieldType.ENUM.value or self._empty(value):
                continue
            enum_value = await self._normalize_enum_value(field, value)
            if enum_value is not None:
                normalized[field_code] = enum_value
        return normalized

    async def _sync_search_index(
        self,
        model: EntityObjectModel,
        schema: EntitySchemaModel,
        values: dict[str, Any],
    ) -> None:
        await self._session.execute(
            delete(ObjectSearchIndexModel).where(ObjectSearchIndexModel.object_id == model.id)
        )
        searchable_fields = {
            field.code: field
            for field in schema.fields
            if field.searchable
            and field.field_type
            in (
                FieldType.STRING.value,
                FieldType.TEXT.value,
                FieldType.ADDRESS.value,
                FieldType.ENUM.value,
                FieldType.REFERENCE.value,
            )
        }
        index_rows: list[ObjectSearchIndexModel] = []
        seen: set[tuple[str, str]] = set()
        for field_code, field in searchable_fields.items():
            value = values.get(field_code)
            parts = value if isinstance(value, list) else [value]
            for part in parts:
                if part is None:
                    continue
                raw_value = str(part).strip()
                normalized_value = self._normalize_search_value(raw_value)
                if not normalized_value:
                    continue
                key = (field.code, raw_value)
                if key in seen:
                    continue
                seen.add(key)
                index_rows.append(
                    ObjectSearchIndexModel(
                        entity_schema_id=schema.id,
                        object_id=model.id,
                        field_code=field.code,
                        value=raw_value,
                        normalized_value=normalized_value,
                    )
                )
        self._session.add_all(index_rows)

    @staticmethod
    def _normalize_search_value(value: str) -> str:
        return " ".join(value.strip().lower().split())

    async def _normalize_enum_value(
        self,
        field: EntityFieldModel,
        value: Any,
    ) -> str | list[str] | None:
        if field.dictionary_id is None:
            return None
        values = value if isinstance(value, list) else [value]
        if not all(isinstance(item, str) for item in values):
            return None
        lookup_values = [item.strip() for item in values]
        if any(not item for item in lookup_values):
            return None
        items = await self._dictionary_item_lookup(field, lookup_values)
        codes: list[str] = []
        for item in lookup_values:
            dictionary_item = self._dictionary_item_for_token(items, item)
            if dictionary_item is None:
                return None
            codes.append(dictionary_item["code"])
        return codes if isinstance(value, list) else codes[0]

    async def _validate(
        self,
        schema: EntitySchemaModel,
        values: dict[str, Any],
        geometry: GeoJsonGeometry | None,
    ) -> list[dict[str, str | None]]:
        fields = {field.code: field for field in schema.fields}
        unknown = sorted(set(values) - set(fields))
        if unknown:
            raise RuntimeValidationError(
                [
                    {
                        "fieldCode": code,
                        "code": "unknown_field",
                        "message": f"Неизвестное поле «{code}»",
                    }
                    for code in unknown
                ]
            )

        issues: list[dict[str, str | None]] = []
        for field in schema.fields:
            value = values.get(field.code)
            if field.required and self._empty(value):
                issues.append(
                    {
                        "fieldCode": field.code,
                        "code": "required",
                        "message": f"Поле «{field.name}» обязательно для заполнения",
                    }
                )
                continue
            if not self._empty(value) and not self._valid_type(field, value):
                issues.append(
                    {
                        "fieldCode": field.code,
                        "code": "invalid_type",
                        "message": f"Некорректное значение поля «{field.name}»",
                    }
                )
                continue
            if self._empty(value):
                continue
            if field.field_type == FieldType.ENUM.value:
                issues.extend(await self._validate_enum_value(field, value))
            elif field.field_type == FieldType.REFERENCE.value:
                issues.extend(await self._validate_reference_value(field, value))

        if schema.geometry_types and geometry is None:
            issues.append(
                {
                    "fieldCode": None,
                    "code": "geometry_required",
                    "message": "Необходимо указать геометрию",
                }
            )
        elif geometry is not None:
            allowed = {item.geometry_type for item in schema.geometry_types}
            actual_types = self._geometry_types(geometry)
            disallowed = sorted(set(actual_types) - allowed)
            if disallowed:
                issues.append(
                    {
                        "fieldCode": None,
                        "code": "geometry_type",
                        "message": "Геометрия содержит запрещённые типы: " + ", ".join(disallowed),
                    }
                )
        return issues

    async def _validate_enum_value(
        self,
        field: EntityFieldModel,
        value: Any,
    ) -> list[dict[str, str | None]]:
        values = value if isinstance(value, list) else [value]
        codes = [item for item in values if isinstance(item, str)]
        if len(codes) != len(values) or field.dictionary_id is None:
            return [
                {
                    "fieldCode": field.code,
                    "code": "invalid_enum_value",
                    "message": f"Значение поля «{field.name}» отсутствует в справочнике",
                }
            ]
        found = set(
            await self._session.scalars(
                select(DictionaryItemModel.code).where(
                    DictionaryItemModel.dictionary_id == field.dictionary_id,
                    DictionaryItemModel.active.is_(True),
                    DictionaryItemModel.code.in_(codes),
                )
            )
        )
        if all(code in found for code in codes):
            return []
        return [
            {
                "fieldCode": field.code,
                "code": "invalid_enum_value",
                "message": f"Значение поля «{field.name}» отсутствует в справочнике",
            }
        ]

    async def _validate_reference_value(
        self,
        field: EntityFieldModel,
        value: Any,
    ) -> list[dict[str, str | None]]:
        if field.reference_entity_schema_id is None:
            valid = False
        else:
            try:
                reference_id = UUID(str(value))
            except (TypeError, ValueError):
                valid = False
            else:
                valid = bool(
                    await self._session.scalar(
                        select(EntityObjectModel.id).where(
                            EntityObjectModel.id == reference_id,
                            EntityObjectModel.entity_schema_id
                            == field.reference_entity_schema_id,
                            EntityObjectModel.status != "archived",
                        )
                    )
                )
        if valid:
            return []
        return [
            {
                "fieldCode": field.code,
                "code": "invalid_reference_value",
                "message": f"Связанный объект поля «{field.name}» не найден",
            }
        ]

    @staticmethod
    def _valid_type(field: EntityFieldModel, value: Any) -> bool:
        field_type = FieldType(field.field_type)
        if field_type in (
            FieldType.STRING,
            FieldType.TEXT,
            FieldType.ADDRESS,
            FieldType.FILE,
        ):
            return isinstance(value, str)
        if field_type == FieldType.ENUM:
            return isinstance(value, str) or (
                isinstance(value, list)
                and all(isinstance(item, str) for item in value)
            )
        if field_type == FieldType.INTEGER:
            return isinstance(value, int) and not isinstance(value, bool)
        if field_type == FieldType.DECIMAL:
            return isinstance(value, int | float) and not isinstance(value, bool)
        if field_type == FieldType.BOOLEAN:
            return isinstance(value, bool)
        if field_type == FieldType.REFERENCE:
            try:
                UUID(str(value))
                return True
            except (TypeError, ValueError):
                return False
        if field_type == FieldType.DATE:
            try:
                date.fromisoformat(str(value))
                return True
            except (TypeError, ValueError):
                return False
        if field_type == FieldType.DATETIME:
            try:
                datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                return True
            except (TypeError, ValueError):
                return False
        return False

    @staticmethod
    def _empty(value: Any) -> bool:
        return value is None or value == "" or value == []

    @staticmethod
    def _geometry_expression(geometry: GeoJsonGeometry | None) -> Any:
        if geometry is None:
            return None
        serialized = json.dumps(geometry.model_dump(mode="json"), ensure_ascii=False)
        return func.ST_SetSRID(func.ST_GeomFromGeoJSON(serialized), 4326)

    @staticmethod
    def _parse_geometry(value: str | None) -> GeoJsonGeometry | None:
        return GeoJsonGeometry.model_validate(json.loads(value)) if value else None

    @classmethod
    def _geometry_types(cls, geometry: GeoJsonGeometry) -> list[str]:
        if geometry.type == "GeometryCollection":
            result: list[str] = []
            for child in geometry.geometries or []:
                result.extend(cls._geometry_types(child))
            return result
        return [{"Point": "point", "LineString": "lineString", "Polygon": "polygon"}[geometry.type]]

    async def _filter_condition(self, schema: EntitySchemaModel, item: ObjectFilter) -> Any:
        system_fields = {
            "id": cast(EntityObjectModel.id, String),
            "parentObjectId": cast(EntityObjectModel.parent_object_id, String),
            "status": EntityObjectModel.status,
            "createdAt": EntityObjectModel.created_at,
            "updatedAt": EntityObjectModel.updated_at,
        }
        field = next(
            (
                candidate
                for candidate in schema.fields
                if candidate.code == item.field and candidate.filterable
            ),
            None,
        )
        expression = system_fields.get(item.field)
        if expression is None and field is not None:
            expression = EntityObjectModel.values[field.code].astext
        if expression is None:
            raise RuntimeValidationError(
                [
                    {
                        "fieldCode": item.field,
                        "code": "invalid_filter",
                        "message": "Поле недоступно для фильтрации",
                    }
                ]
            )

        raw_value = item.value or ""
        value = raw_value
        enum_filter_values: list[str] | None = None
        if (
            field
            and field.field_type == FieldType.ENUM.value
            and value
            and item.operator in ("equals", "notEquals", "in", "notIn")
        ):
            enum_filter_values = await self._normalize_enum_filter_values(
                field,
                item.operator,
                value,
            )
            value = enum_filter_values[0] if enum_filter_values else value
        operator = item.operator
        if operator == "filled":
            return and_(expression.is_not(None), expression != "")
        if operator == "empty":
            return or_(expression.is_(None), expression == "")
        if operator == "contains":
            return expression.ilike(f"%{value}%")
        if operator == "startsWith":
            return expression.ilike(f"{value}%")
        if operator == "endsWith":
            return expression.ilike(f"%{value}")
        if operator == "in":
            return expression.in_(enum_filter_values or [part.strip() for part in value.split(",")])
        if operator == "notIn":
            return not_(expression.in_(enum_filter_values or [part.strip() for part in value.split(",")]))
        if enum_filter_values and operator == "equals":
            return expression.in_(enum_filter_values)
        if enum_filter_values and operator == "notEquals":
            return not_(expression.in_(enum_filter_values))

        comparable = expression
        expected: Any = value
        if field and field.field_type in (FieldType.INTEGER.value, FieldType.DECIMAL.value):
            try:
                expected = Decimal(value)
            except InvalidOperation as error:
                raise RuntimeValidationError(
                    [
                        {
                            "fieldCode": item.field,
                            "code": "invalid_filter",
                            "message": "Ожидается числовое значение",
                        }
                    ]
                ) from error
            comparable = cast(expression, type_cast(Any, Numeric()))
        elif operator in ("today", "beforeToday", "afterToday"):
            comparable = cast(expression, type_cast(Any, Date()))
            expected = date.today()

        comparisons = {
            "equals": comparable == expected,
            "notEquals": comparable != expected,
            "greaterThan": comparable > expected,
            "greaterOrEqual": comparable >= expected,
            "lessThan": comparable < expected,
            "lessOrEqual": comparable <= expected,
            "today": comparable == expected,
            "beforeToday": comparable < expected,
            "afterToday": comparable > expected,
        }
        return comparisons[operator]

    async def _normalize_enum_filter_values(
        self,
        field: EntityFieldModel,
        operator: str,
        value: str,
    ) -> list[str]:
        if operator in ("in", "notIn"):
            parts = [part.strip() for part in value.split(",")]
            lookup = await self._dictionary_item_lookup(field, parts)
            values: list[str] = []
            for part in parts:
                dictionary_item = self._dictionary_item_for_token(lookup, part)
                values.append(dictionary_item["code"] if dictionary_item else part)
                if dictionary_item and part != dictionary_item["code"]:
                    values.append(part)
            return values
        enum_value = await self._normalize_enum_value(field, value)
        if isinstance(enum_value, str):
            return [enum_value, value] if enum_value != value else [enum_value]
        return [value]

    def _sort_expression(self, schema: EntitySchemaModel, sort: str | None) -> Any:
        if not sort:
            return EntityObjectModel.updated_at.desc()
        descending = sort.startswith("-")
        key = sort[1:] if descending else sort
        system_fields = {
            "id": EntityObjectModel.id,
            "status": EntityObjectModel.status,
            "createdAt": EntityObjectModel.created_at,
            "updatedAt": EntityObjectModel.updated_at,
        }
        expression: Any = system_fields.get(key)
        field = next((candidate for candidate in schema.fields if candidate.code == key), None)
        if expression is None and field is not None:
            expression = EntityObjectModel.values[field.code].astext
            if field.field_type in (FieldType.INTEGER.value, FieldType.DECIMAL.value):
                expression = cast(expression, Numeric())
            elif field.field_type == FieldType.DATE.value:
                expression = cast(expression, Date())
            elif field.field_type == FieldType.DATETIME.value:
                expression = cast(expression, DateTime(timezone=True))
        if expression is None:
            raise RuntimeValidationError(
                [
                    {
                        "fieldCode": key,
                        "code": "invalid_sort",
                        "message": "Поле недоступно для сортировки",
                    }
                ]
            )
        return expression.desc() if descending else expression.asc()

    @staticmethod
    def _changes(
        before: dict[str, Any],
        after: dict[str, Any],
        fields: list[EntityFieldModel],
    ) -> list[dict[str, Any]]:
        names = {field.code: field.name for field in fields}
        return [
            {
                "fieldCode": code,
                "fieldName": names.get(code, code),
                "oldValue": before.get(code),
                "newValue": after.get(code),
            }
            for code in sorted(set(before) | set(after))
            if before.get(code) != after.get(code)
        ]

    def _add_outbox(
        self,
        event_type: str,
        model: EntityObjectModel,
        entity_code: str,
        actor_id: UUID | None,
    ) -> None:
        self._session.add(
            OutboxEventModel(
                aggregate_type="entity_object",
                aggregate_id=model.id,
                event_type=event_type,
                payload={
                    "objectId": str(model.id),
                    "entityId": str(model.entity_schema_id),
                    "entityCode": entity_code,
                    "revision": model.revision,
                    "parentObjectId": str(model.parent_object_id) if model.parent_object_id else None,
                    "actorId": str(actor_id) if actor_id else None,
                },
            )
        )

    async def _resolve_parent_object_id(
        self,
        schema: EntitySchemaModel,
        parent_object_id: UUID | None,
    ) -> UUID | None:
        if schema.parent_entity_schema_id is None:
            if parent_object_id is not None:
                raise RuntimeValidationError(
                    [
                        {
                            "fieldCode": None,
                            "code": "parent_not_allowed",
                            "message": "Для корневой сущности parentObjectId передавать нельзя",
                        }
                    ]
                )
            return None
        if parent_object_id is None:
            raise RuntimeValidationError(
                [
                    {
                        "fieldCode": None,
                        "code": "parent_required",
                        "message": "Для дочерней сущности необходимо передать parentObjectId",
                    }
                ]
            )
        exists = bool(
            await self._session.scalar(
                select(EntityObjectModel.id).where(
                    EntityObjectModel.id == parent_object_id,
                    EntityObjectModel.entity_schema_id == schema.parent_entity_schema_id,
                    EntityObjectModel.status != "archived",
                )
            )
        )
        if not exists:
            raise RuntimeValidationError(
                [
                    {
                        "fieldCode": None,
                        "code": "parent_not_found",
                        "message": "Родительский объект не найден или находится в архиве",
                    }
                ]
            )
        return parent_object_id

    async def _display_values(
        self,
        schema: EntitySchemaModel,
        values: dict[str, Any],
    ) -> dict[str, str | list[str] | None]:
        result: dict[str, str | list[str] | None] = {}
        for field in schema.fields:
            if field.field_type != FieldType.ENUM.value:
                continue
            value = values.get(field.code)
            if self._empty(value):
                result[field.code] = None
                continue
            items = await self._dictionary_item_lookup(
                field,
                value if isinstance(value, list) else [value],
            )
            if isinstance(value, list):
                result[field.code] = [
                    (self._dictionary_item_for_token(items, str(item)) or {"name": str(item)})["name"]
                    for item in value
                ]
            else:
                result[field.code] = (
                    self._dictionary_item_for_token(items, str(value)) or {"name": str(value)}
                )["name"]
        return result

    async def _dictionary_item_lookup(
        self,
        field: EntityFieldModel,
        values: list[Any],
    ) -> dict[str, Any]:
        if field.dictionary_id is None:
            return {}
        lookup_values = [
            self._lookup_token(str(value))
            for value in values
            if isinstance(value, str) and value.strip()
        ]
        if not lookup_values:
            return {}
        if field.dictionary_id not in self._dictionary_lookup_cache:
            rows = (
                await self._session.execute(
                    select(
                        DictionaryItemModel.id,
                        DictionaryItemModel.code,
                        DictionaryItemModel.name,
                    )
                    .where(
                        DictionaryItemModel.dictionary_id == field.dictionary_id,
                        DictionaryItemModel.active.is_(True),
                    )
                    .order_by(DictionaryItemModel.sort_order, DictionaryItemModel.name)
                )
            ).all()
            cache: dict[str, Any] = {"byId": {}, "byCode": {}, "byName": {}}
            for item_id, code, name in rows:
                payload = {"id": str(item_id), "code": code, "name": name}
                cache["byId"][self._lookup_token(str(item_id))] = payload
                cache["byCode"][self._lookup_token(code)] = payload
                cache["byName"].setdefault(self._lookup_token(name), []).append(payload)
            self._dictionary_lookup_cache[field.dictionary_id] = cache
        dictionary_lookup = self._dictionary_lookup_cache[field.dictionary_id]
        return dictionary_lookup

    @staticmethod
    def _lookup_token(value: str) -> str:
        return value.strip().casefold()

    @staticmethod
    def _dictionary_item_for_token(
        lookup: dict[str, Any],
        value: str,
    ) -> dict[str, str] | None:
        token = RuntimeObjectService._lookup_token(value)
        by_id = lookup.get("byId", {})
        by_code = lookup.get("byCode", {})
        by_name = lookup.get("byName", {})
        if token in by_id:
            return by_id[token]
        if token in by_code:
            return by_code[token]
        name_matches = by_name.get(token, [])
        return name_matches[0] if len(name_matches) == 1 else None

    async def _to_response(
        self,
        model: EntityObjectModel,
        schema: EntitySchemaModel,
        geometry: GeoJsonGeometry | None,
    ) -> EntityObjectRead:
        return EntityObjectRead(
            id=model.id,
            entity_id=model.entity_schema_id,
            entity_code=schema.code,
            parent_object_id=model.parent_object_id,
            values=model.values,
            display_values=await self._display_values(schema, model.values),
            geometry=geometry,
            attachment_paths=model.attachment_paths,
            status=model.status,
            data_quality=model.data_quality,
            validation_errors=[ValidationIssue.model_validate(item) for item in model.validation_errors],
            revision=model.revision,
            created_at=model.created_at,
            updated_at=model.updated_at,
            created_by=model.created_by,
            updated_by=model.updated_by,
        )
