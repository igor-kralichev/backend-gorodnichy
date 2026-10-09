from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from typing import cast as type_cast
from uuid import UUID, uuid4
from urllib.parse import urlparse

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Numeric,
    String,
    and_,
    case,
    cast,
    delete,
    func,
    literal,
    not_,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.security import ActorContext
from app.modules.access.application.service import AuthorizationService
from app.modules.entities.domain.enums import FieldType
from app.modules.entities.infrastructure.models import (
    EntityFieldModel,
    EntitySchemaModel,
    EntitySchemaVersionModel,
)
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
    ObjectFilterOperator,
    ObjectStatusScope,
    ObjectClusterPage,
    ObjectClusterRead,
    ObjectSearch,
    ProjectedObjectRead,
    RegistryTreePage,
    RegistryTreeSearch,
    ValidationIssue,
)
from app.modules.objects.application.calculations import evaluate_calculated_fields
from app.shared.db.models import (
    AttachmentModel,
    DictionaryItemModel,
    EntityObjectModel,
    EntityUniqueValueModel,
    ImportRowModel,
    ObjectSearchIndexModel,
    ObjectEventModel,
    OrganizationModel,
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

    async def create(
        self,
        entity_code: str,
        payload: EntityObjectCreate,
        actor_id: UUID | None,
        *,
        actor_roles: frozenset[str] | None = None,
    ) -> EntityObjectRead:
        response = await self.create_many(
            entity_code,
            [payload],
            actor_id,
            actor_roles=actor_roles,
        )
        return response.data[0]

    async def create_many(
        self,
        entity_code: str,
        payloads: list[EntityObjectCreate],
        actor_id: UUID | None,
        *,
        object_ids: list[UUID] | None = None,
        actor_roles: frozenset[str] | None = None,
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
        if object_ids is not None and len(object_ids) != len(payloads):
            raise ValueError("Количество objectIds должно совпадать с количеством объектов")
        created: list[tuple[EntityObjectModel, EntitySchemaModel, GeoJsonGeometry | None]] = []
        batch_unique_values: set[tuple[UUID, str]] = set()
        async with self._session.begin():
            schema = await self._get_schema(entity_code)
            schema_version_id = await self._schema_version_id(schema.id)
            for index, payload in enumerate(payloads):
                self._ensure_writable_fields(
                    schema,
                    payload.values,
                    action="create",
                    actor_roles=actor_roles,
                )
                parent_object_id = await self._resolve_parent_object_id(schema, payload.parent_object_id)
                owner_organization_id = payload.owner_organization_id or schema.owner_organization_id
                await self._validate_organization(owner_organization_id)
                values = await self._normalize_values(schema, dict(payload.values))
                issues = await self._validate(schema, values, payload.geometry)
                issues.extend(await self._unique_issues(schema, values, batch_unique_values=batch_unique_values))
                object_id = object_ids[index] if object_ids is not None else uuid4()
                model = EntityObjectModel(
                    id=object_id,
                    entity_schema_id=schema.id,
                    schema_version_id=schema_version_id,
                    parent_object_id=parent_object_id,
                    municipality_id=schema.scope_municipality_id,
                    owner_organization_id=owner_organization_id,
                    responsible_id=payload.responsible_id,
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
                await self._sync_unique_values(model, schema, model.values)
        data = [
            await self._to_response(
                model,
                schema,
                geometry,
                actor_roles=actor_roles,
            )
            for model, schema, geometry in created
        ]
        return EntityObjectBulkCreateRead(
            created=len(data),
            published=sum(item.status == "published" for item in data),
            draft=sum(item.status == "draft" for item in data),
            data=data,
        )

    async def get(
        self,
        entity_code: str,
        object_id: UUID,
        *,
        actor_roles: frozenset[str] | None = None,
    ) -> EntityObjectRead:
        schema = await self._get_schema(entity_code)
        model, geometry = await self._get_object(schema.id, object_id)
        return await self._to_response(
            model,
            schema,
            geometry,
            actor_roles=actor_roles,
        )

    async def copy(
        self,
        entity_code: str,
        object_id: UUID,
        actor_id: UUID | None,
        *,
        actor_roles: frozenset[str] | None = None,
    ) -> EntityObjectRead:
        """Создать копию объекта без уникальных и вычисляемых значений."""

        schema = await self._get_schema(entity_code)
        source, geometry = await self._get_object(schema.id, object_id)
        excluded_codes = {
            field.code
            for field in schema.fields
            if field.unique_value or field.read_only or field.archived
        }
        values = {
            code: value
            for code, value in source.values.items()
            if code not in excluded_codes
        }
        payload = EntityObjectCreate(
            values=values,
            parent_object_id=source.parent_object_id,
            owner_organization_id=source.owner_organization_id,
            responsible_id=source.responsible_id,
            geometry=geometry,
        )
        await self._session.rollback()
        return await self.create(
            entity_code,
            payload,
            actor_id,
            actor_roles=actor_roles,
        )

    async def list_page(
        self,
        entity_code: str,
        search: ObjectSearch,
        *,
        actor: ActorContext | None = None,
    ) -> EntityObjectPage:
        schema = await self._get_schema(entity_code)
        conditions = [EntityObjectModel.entity_schema_id == schema.id]
        conditions.extend(self._status_conditions(search.status))
        if actor is not None:
            conditions.append(
                await AuthorizationService(self._session).readable_object_condition(
                    actor,
                    schema=schema,
                )
            )
        if search.parent_object_id is not None:
            conditions.append(EntityObjectModel.parent_object_id == search.parent_object_id)
        if search.object_ids:
            conditions.append(EntityObjectModel.id.in_(search.object_ids))
        if search.excluded_ids:
            conditions.append(EntityObjectModel.id.not_in(search.excluded_ids))
        if search.bbox is not None:
            min_lon, min_lat, max_lon, max_lat = search.bbox
            conditions.append(
                func.ST_Intersects(
                    EntityObjectModel.geometry,
                    func.ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat, 4326),
                )
            )
        if search.q:
            conditions.append(
                self._search_condition(
                    schema.id,
                    search.q,
                    field_codes=self._readable_search_field_codes(
                        schema,
                        actor.roles if actor is not None else None,
                    ),
                )
            )
        filter_conditions = [
            await self._filter_condition(
                schema,
                item,
                actor_roles=actor.roles if actor is not None else None,
            )
            for item in search.filters
        ]
        if filter_conditions:
            conditions.append(or_(*filter_conditions) if search.logic == "or" else and_(*filter_conditions))

        total = int(
            await self._session.scalar(select(func.count()).select_from(EntityObjectModel).where(*conditions)) or 0
        )
        if search.object_ids and total != len(set(search.object_ids)):
            self._invalid_filter(
                None,
                "Часть выбранных объектов не найдена или недоступна",
            )
        geometry_json = func.ST_AsGeoJSON(EntityObjectModel.geometry).label("geometry_json")
        statement = (
            select(EntityObjectModel, geometry_json)
            .where(*conditions)
            .order_by(
                self._sort_expression(
                    schema,
                    search.sort,
                    actor_roles=actor.roles if actor is not None else None,
                )
            )
            .offset(search.offset)
            .limit(search.limit)
        )
        rows = (await self._session.execute(statement)).all()
        data = [
            await self._to_response(
                model,
                schema,
                self._parse_geometry(geometry),
                actor_roles=actor.roles if actor is not None else None,
            )
            for model, geometry in rows
        ]
        return EntityObjectPage(total=total, returned=len(data), offset=search.offset, limit=search.limit, data=data)

    async def filter_dependency_entity_codes(
        self,
        entity_code: str,
        filters: list[ObjectFilter],
    ) -> set[str]:
        """Вернуть сущности, данные которых читаются составными фильтрами."""

        schema = await self._get_schema(entity_code)
        target_ids: set[UUID] = set()
        for item in filters:
            if "." not in item.field:
                continue
            relation_code, _ = item.field.split(".", 1)
            if relation_code == "parent":
                if schema.parent_entity_schema_id is not None:
                    target_ids.add(schema.parent_entity_schema_id)
                continue
            relation_field = next(
                (
                    field
                    for field in schema.fields
                    if field.code == relation_code
                    and field.field_type == FieldType.REFERENCE.value
                    and field.filterable
                    and not field.archived
                ),
                None,
            )
            if relation_field and relation_field.reference_entity_schema_id:
                target_ids.add(relation_field.reference_entity_schema_id)
        if not target_ids:
            return set()
        return set(
            (
                await self._session.scalars(
                    select(EntitySchemaModel.code).where(
                        EntitySchemaModel.id.in_(target_ids),
                        EntitySchemaModel.status == "active",
                    )
                )
            ).all()
        )

    async def list_tree(
        self,
        entity_code: str,
        search: RegistryTreeSearch,
        *,
        actor: ActorContext | None = None,
        max_returned_rows: int = 100_000,
    ) -> RegistryTreePage:
        """Получить родительские объекты и подреестры без N+1 запросов."""

        parent_schema = await self._get_schema(entity_code)
        parent_fields = self._projection_fields(
            parent_schema,
            search.columns,
            actor_roles=actor.roles if actor is not None else None,
        )
        page_payload = {
            "logic": search.logic,
            "q": search.q,
            "status": search.status,
            "filters": search.filters,
            "object_ids": search.object_ids,
            "excluded_ids": search.excluded_ids,
            "sort": search.sort,
            "parent_object_id": search.parent_object_id,
            "bbox": search.bbox,
            "limit": search.limit,
            "offset": search.offset,
        }
        page_search = (
            ObjectSearch.model_construct(**page_payload)
            if search.limit > 1000
            else ObjectSearch(**page_payload)
        )
        parent_page = await self.list_page(entity_code, page_search, actor=actor)
        projected = [
            ProjectedObjectRead(
                id=item.id,
                parent_object_id=item.parent_object_id,
                status=item.status,
                data_quality=item.data_quality,
                values={code: item.values.get(code) for code in parent_fields},
                display_values={
                    code: item.display_values[code]
                    for code in parent_fields
                    if code in item.display_values
                },
            )
            for item in parent_page.data
        ]
        parent_by_id = {item.id: item for item in projected}
        if not parent_by_id:
            return RegistryTreePage(
                total=parent_page.total,
                returned=0,
                offset=search.offset,
                limit=search.limit,
                data=[],
            )
        requested_child_rows = len(parent_by_id) * sum(
            child.limit_per_parent for child in search.children
        )
        if requested_child_rows > max_returned_rows:
            self._invalid_filter(
                None,
                f"Составная выборка может вернуть более {max_returned_rows} "
                "строк подреестров; "
                "уменьшите limit или limitPerParent",
            )

        for child_query in search.children:
            child_schema = await self._get_schema(child_query.entity_code)
            if child_schema.parent_entity_schema_id != parent_schema.id:
                self._invalid_filter(
                    child_query.entity_code,
                    "Сущность не является непосредственным подреестром выбранного реестра",
                )
            child_fields = self._projection_fields(
                child_schema,
                child_query.columns,
                actor_roles=actor.roles if actor is not None else None,
            )
            conditions = [
                EntityObjectModel.entity_schema_id == child_schema.id,
                EntityObjectModel.parent_object_id.in_(parent_by_id),
            ]
            conditions.extend(self._status_conditions(child_query.status))
            if child_query.object_ids:
                conditions.append(EntityObjectModel.id.in_(child_query.object_ids))
            if child_query.excluded_ids:
                conditions.append(EntityObjectModel.id.not_in(child_query.excluded_ids))
            if child_query.q:
                conditions.append(
                    self._search_condition(
                        child_schema.id,
                        child_query.q,
                        field_codes=self._readable_search_field_codes(
                            child_schema,
                            actor.roles if actor is not None else None,
                        ),
                    )
                )
            if actor is not None:
                conditions.append(
                    await AuthorizationService(self._session).readable_object_condition(
                        actor,
                        schema=child_schema,
                    )
                )
            child_conditions = [
                await self._filter_condition(
                    child_schema,
                    item,
                    actor_roles=actor.roles if actor is not None else None,
                )
                for item in child_query.filters
            ]
            if child_conditions:
                conditions.append(
                    or_(*child_conditions)
                    if child_query.logic == "or"
                    else and_(*child_conditions)
                )
            child_order = self._sort_expression(
                child_schema,
                child_query.sort,
                actor_roles=actor.roles if actor is not None else None,
            )
            ranked_children = (
                select(
                    EntityObjectModel.id.label("object_id"),
                    func.row_number()
                    .over(
                        partition_by=EntityObjectModel.parent_object_id,
                        order_by=child_order,
                    )
                    .label("row_number"),
                )
                .where(*conditions)
                .subquery()
            )
            children = (
                await self._session.scalars(
                    select(EntityObjectModel)
                    .join(
                        ranked_children,
                        ranked_children.c.object_id == EntityObjectModel.id,
                    )
                    .where(ranked_children.c.row_number <= child_query.limit_per_parent)
                    .order_by(EntityObjectModel.parent_object_id, ranked_children.c.row_number)
                )
            ).all()
            for child in children:
                if child.parent_object_id not in parent_by_id:
                    continue
                parent_by_id[child.parent_object_id].children.setdefault(
                    child_schema.code,
                    [],
                ).append(
                    ProjectedObjectRead(
                        id=child.id,
                        parent_object_id=child.parent_object_id,
                        status=child.status,
                        data_quality=child.data_quality,
                        values={code: child.values.get(code) for code in child_fields},
                        display_values={
                            code: value
                            for code, value in (
                                await self._display_values(child_schema, child.values)
                            ).items()
                            if code in child_fields
                        },
                    )
                )

        return RegistryTreePage(
            total=parent_page.total,
            returned=len(projected),
            offset=search.offset,
            limit=search.limit,
            data=projected,
        )

    @staticmethod
    def _projection_fields(
        schema: EntitySchemaModel,
        requested: list[str],
        *,
        actor_roles: frozenset[str] | None = None,
    ) -> list[str]:
        available = {
            field.code: field
            for field in schema.fields
            if not field.archived
            and RuntimeObjectService._field_is_readable(field, actor_roles)
        }
        if not requested:
            return [
                field.code
                for field in schema.fields
                if field.list_visible
                and not field.archived
                and RuntimeObjectService._field_is_readable(field, actor_roles)
            ]
        unknown = sorted(set(requested) - set(available))
        if unknown:
            RuntimeObjectService._invalid_filter(
                unknown[0],
                "Поле недоступно для выдачи",
            )
        return list(dict.fromkeys(requested))

    async def list_clusters(
        self,
        entity_code: str,
        bbox: tuple[float, float, float, float],
        zoom: int,
        *,
        actor: ActorContext | None = None,
        q: str | None = None,
        status: ObjectStatusScope = ObjectStatusScope.CURRENT,
        parent_object_id: UUID | None = None,
        logic: str = "and",
        filters: list[ObjectFilter] | None = None,
    ) -> ObjectClusterPage:
        schema = await self._get_schema(entity_code)
        min_lon, min_lat, max_lon, max_lat = bbox
        envelope = func.ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat, 4326)
        point = func.ST_PointOnSurface(EntityObjectModel.geometry)
        grid_size = 360.0 / ((2**zoom) * 64)
        snapped = func.ST_SnapToGrid(point, grid_size)
        conditions = [
            EntityObjectModel.entity_schema_id == schema.id,
            EntityObjectModel.geometry.is_not(None),
            func.ST_Intersects(EntityObjectModel.geometry, envelope),
        ]
        conditions.extend(self._status_conditions(status))
        if parent_object_id is not None:
            conditions.append(EntityObjectModel.parent_object_id == parent_object_id)
        if q:
            conditions.append(
                self._search_condition(
                    schema.id,
                    q,
                    field_codes=self._readable_search_field_codes(
                        schema,
                        actor.roles if actor is not None else None,
                    ),
                )
            )
        filter_conditions = [
            await self._filter_condition(
                schema,
                item,
                actor_roles=actor.roles if actor is not None else None,
            )
            for item in (filters or [])
        ]
        if filter_conditions:
            conditions.append(
                or_(*filter_conditions) if logic == "or" else and_(*filter_conditions)
            )
        if actor is not None:
            conditions.append(
                await AuthorizationService(self._session).readable_object_condition(
                    actor,
                    schema=schema,
                )
            )
        rows = (
            await self._session.execute(
                select(
                    func.ST_X(snapped).label("longitude"),
                    func.ST_Y(snapped).label("latitude"),
                    func.count().label("count"),
                )
                .where(*conditions)
                .group_by(snapped)
                .order_by(func.count().desc())
            )
        ).all()
        return ObjectClusterPage(
            clusters=[
                ObjectClusterRead(longitude=float(lon), latitude=float(lat), count=int(count))
                for lon, lat, count in rows
            ],
            total_objects=sum(int(count) for _, _, count in rows),
        )

    @staticmethod
    def _status_conditions(status: ObjectStatusScope) -> list[Any]:
        if status == ObjectStatusScope.ARCHIVED:
            return [EntityObjectModel.status == "archived"]
        if status == ObjectStatusScope.ALL:
            return []
        return [EntityObjectModel.status != "archived"]

    @classmethod
    def _search_condition(
        cls,
        entity_schema_id: UUID,
        query: str,
        *,
        field_codes: list[str],
    ) -> Any:
        normalized = " ".join(query.strip().casefold().split())
        escaped = cls._escape_like(normalized)
        return (
            select(literal(1))
            .select_from(ObjectSearchIndexModel)
            .where(
                ObjectSearchIndexModel.object_id == EntityObjectModel.id,
                ObjectSearchIndexModel.entity_schema_id == entity_schema_id,
                ObjectSearchIndexModel.field_code.in_(field_codes),
                ObjectSearchIndexModel.normalized_value.ilike(
                    f"%{escaped}%",
                    escape="\\",
                ),
            )
            .exists()
        )

    async def update(
        self,
        entity_code: str,
        object_id: UUID,
        payload: EntityObjectPatch,
        actor_id: UUID | None,
        *,
        actor_roles: frozenset[str] | None = None,
    ) -> EntityObjectRead:
        async with self._session.begin():
            schema = await self._get_schema(entity_code)
            model, current_geometry = await self._get_object(schema.id, object_id, for_update=True)
            if model.status == "archived":
                raise RuntimeObjectNotFound
            if payload.revision != model.revision:
                raise RevisionConflict(model.revision)

            before_values = dict(model.values)
            self._ensure_writable_fields(
                schema,
                payload.values or {},
                action="update",
                actor_roles=actor_roles,
            )
            values = await self._normalize_values(schema, {**before_values, **(payload.values or {})})
            geometry_changed = "geometry" in payload.model_fields_set
            next_geometry = payload.geometry if geometry_changed else current_geometry
            issues = await self._validate(schema, values, next_geometry)
            issues.extend(await self._unique_issues(schema, values, exclude_object_id=model.id))

            model.values = values
            if "owner_organization_id" in payload.model_fields_set:
                await self._validate_organization(payload.owner_organization_id)
                model.owner_organization_id = payload.owner_organization_id
            if "responsible_id" in payload.model_fields_set:
                model.responsible_id = payload.responsible_id
            if geometry_changed:
                model.geometry = self._geometry_expression(payload.geometry)
            model.schema_version_id = await self._schema_version_id(schema.id)
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
            await self._sync_unique_values(model, schema, values)
            await self._session.refresh(model, attribute_names=["updated_at"])
        return await self._to_response(
            model,
            schema,
            next_geometry,
            actor_roles=actor_roles,
        )

    async def archive(self, entity_code: str, object_id: UUID, actor_id: UUID | None) -> EntityObjectStatusRead:
        async with self._session.begin():
            schema = await self._get_schema(entity_code)
            model, _ = await self._get_object(schema.id, object_id, for_update=True)
            if model.status != "archived":
                model.status = "archived"
                model.archived_at = datetime.now(UTC)
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
            model.archived_at = None
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
            await self._sync_unique_values(model, schema, values)
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

    async def _schema_version_id(self, entity_id: UUID) -> UUID:
        version_id = await self._session.scalar(
            select(EntitySchemaVersionModel.id)
            .where(EntitySchemaVersionModel.entity_schema_id == entity_id)
            .order_by(EntitySchemaVersionModel.version.desc())
            .limit(1)
        )
        if version_id is None:
            raise RuntimeValidationError(
                [
                    {
                        "fieldCode": None,
                        "code": "schema_version_missing",
                        "message": "У опубликованной сущности отсутствует версия схемы",
                    }
                ]
            )
        return version_id

    async def _validate_organization(self, organization_id: UUID | None) -> None:
        if organization_id is None:
            return
        exists = await self._session.scalar(
            select(OrganizationModel.id).where(
                OrganizationModel.id == organization_id,
                OrganizationModel.active.is_(True),
            )
        )
        if exists is None:
            raise RuntimeValidationError(
                [
                    {
                        "fieldCode": None,
                        "code": "organization_not_found",
                        "message": "Организация-владелец не найдена или неактивна",
                    }
                ]
            )

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
        for field in schema.fields:
            if field.code not in normalized and field.default_value is not None and not field.read_only:
                normalized[field.code] = field.default_value
        for field_code, value in values.items():
            field = fields.get(field_code)
            if field is None:
                continue
            if isinstance(value, str):
                normalized[field_code] = value.strip()
                value = normalized[field_code]
            if field.field_type == FieldType.DECIMAL.value and not self._empty(value):
                try:
                    decimal_value = Decimal(str(value))
                    if not decimal_value.is_finite():
                        raise InvalidOperation
                    normalized[field_code] = format(decimal_value, "f")
                    value = normalized[field_code]
                except InvalidOperation:
                    pass
            if field.field_type == FieldType.DATE.value and not self._empty(value):
                try:
                    normalized[field_code] = date.fromisoformat(str(value)).isoformat()
                    value = normalized[field_code]
                except ValueError:
                    pass
            if field.field_type == FieldType.DATETIME.value and not self._empty(value):
                try:
                    parsed_datetime = datetime.fromisoformat(
                        str(value).replace("Z", "+00:00")
                    )
                    if parsed_datetime.utcoffset() is not None:
                        normalized[field_code] = (
                            parsed_datetime.astimezone(UTC)
                            .isoformat()
                            .replace("+00:00", "Z")
                        )
                        value = normalized[field_code]
                except ValueError:
                    pass
            if field.field_type != FieldType.ENUM.value or self._empty(value):
                continue
            enum_value = await self._normalize_enum_value(field, value)
            if enum_value is not None:
                normalized[field_code] = enum_value
        return evaluate_calculated_fields(schema.fields, normalized)

    @staticmethod
    def _ensure_writable_fields(
        schema: EntitySchemaModel,
        values: dict[str, Any],
        *,
        action: str,
        actor_roles: frozenset[str] | None,
    ) -> None:
        """Запретить изменение служебных и закрытых правилами полей."""

        fields = {field.code: field for field in schema.fields}
        issues: list[dict[str, str | None]] = []
        for code in values:
            field = fields.get(code)
            if field is None:
                issues.append(
                    {
                        "fieldCode": code,
                        "code": "unknown_field",
                        "message": f"Неизвестное поле «{code}»",
                    }
                )
                continue
            if field.archived:
                issues.append(
                    {
                        "fieldCode": code,
                        "code": "field_archived",
                        "message": f"Поле «{field.name}» находится в архиве",
                    }
                )
                continue
            if field.read_only:
                issues.append(
                    {
                        "fieldCode": code,
                        "code": "field_read_only",
                        "message": f"Поле «{field.name}» доступно только для чтения",
                    }
                )
                continue
            allowed_roles = (field.access_rules or {}).get(action)
            if (
                allowed_roles is not None
                and actor_roles is not None
                and not set(allowed_roles).intersection(actor_roles)
            ):
                issues.append(
                    {
                        "fieldCode": code,
                        "code": "field_access_denied",
                        "message": f"Недостаточно прав для изменения поля «{field.name}»",
                    }
                )
        if issues:
            raise RuntimeValidationError(issues)

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

    async def _sync_unique_values(
        self,
        model: EntityObjectModel,
        schema: EntitySchemaModel,
        values: dict[str, Any],
    ) -> None:
        await self._session.execute(
            delete(EntityUniqueValueModel).where(EntityUniqueValueModel.entity_object_id == model.id)
        )
        rows: list[EntityUniqueValueModel] = []
        for field in schema.fields:
            if not field.unique_value:
                continue
            value = values.get(field.code)
            if self._empty(value):
                continue
            normalized = self._normalize_unique_value(value)
            conflict = await self._session.scalar(
                select(EntityUniqueValueModel.id).where(
                    EntityUniqueValueModel.entity_field_id == field.id,
                    EntityUniqueValueModel.normalized_value == normalized,
                    EntityUniqueValueModel.entity_object_id != model.id,
                )
            )
            if conflict is not None:
                continue
            rows.append(
                EntityUniqueValueModel(
                    entity_schema_id=schema.id,
                    entity_field_id=field.id,
                    entity_object_id=model.id,
                    normalized_value=normalized,
                )
            )
        self._session.add_all(rows)

    async def _unique_issues(
        self,
        schema: EntitySchemaModel,
        values: dict[str, Any],
        *,
        exclude_object_id: UUID | None = None,
        batch_unique_values: set[tuple[UUID, str]] | None = None,
    ) -> list[dict[str, str | None]]:
        issues: list[dict[str, str | None]] = []
        for field in schema.fields:
            if not field.unique_value or self._empty(values.get(field.code)):
                continue
            normalized = self._normalize_unique_value(values[field.code])
            key = (field.id, normalized)
            in_batch = batch_unique_values is not None and key in batch_unique_values
            conditions = [
                EntityUniqueValueModel.entity_field_id == field.id,
                EntityUniqueValueModel.normalized_value == normalized,
            ]
            if exclude_object_id is not None:
                conditions.append(EntityUniqueValueModel.entity_object_id != exclude_object_id)
            exists = await self._session.scalar(
                select(EntityUniqueValueModel.id).where(*conditions).limit(1)
            )
            if in_batch or exists is not None:
                issues.append(
                    {
                        "fieldCode": field.code,
                        "code": "not_unique",
                        "message": f"Значение поля «{field.name}» уже используется",
                    }
                )
            elif batch_unique_values is not None:
                batch_unique_values.add(key)
        return issues

    @staticmethod
    def _normalize_unique_value(value: Any) -> str:
        if isinstance(value, str):
            return value.strip().casefold()
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

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
            issues.extend(self._validate_constraints(field, value))
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

    @staticmethod
    def _validate_constraints(field: EntityFieldModel, value: Any) -> list[dict[str, str | None]]:
        issues: list[dict[str, str | None]] = []
        if isinstance(value, str):
            if field.min_length is not None and len(value) < field.min_length:
                issues.append(
                    {
                        "fieldCode": field.code,
                        "code": "min_length",
                        "message": f"Поле «{field.name}» должно содержать не менее {field.min_length} символов",
                    }
                )
            if field.max_length is not None and len(value) > field.max_length:
                issues.append(
                    {
                        "fieldCode": field.code,
                        "code": "max_length",
                        "message": f"Поле «{field.name}» должно содержать не более {field.max_length} символов",
                    }
                )
            if field.field_type == FieldType.EMAIL.value and not re.fullmatch(
                r"[^@\s]+@[^@\s]+\.[^@\s]+", value
            ):
                issues.append(
                    {
                        "fieldCode": field.code,
                        "code": "invalid_email",
                        "message": f"Поле «{field.name}» должно содержать корректный email",
                    }
                )
            if field.field_type == FieldType.PHONE.value and not re.fullmatch(
                r"\+?[0-9()\-\s]{7,32}", value
            ):
                issues.append(
                    {
                        "fieldCode": field.code,
                        "code": "invalid_phone",
                        "message": f"Поле «{field.name}» должно содержать корректный телефон",
                    }
                )
            if field.field_type == FieldType.URL.value:
                parsed = urlparse(value)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    issues.append(
                        {
                            "fieldCode": field.code,
                            "code": "invalid_url",
                            "message": f"Поле «{field.name}» должно содержать корректный URL",
                        }
                    )
        if (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            or field.field_type == FieldType.DECIMAL.value
            and isinstance(value, str)
        ):
            try:
                decimal_value = Decimal(str(value))
            except InvalidOperation:
                return issues
            if field.min_value is not None and decimal_value < Decimal(str(field.min_value)):
                issues.append(
                    {
                        "fieldCode": field.code,
                        "code": "min_value",
                        "message": f"Значение поля «{field.name}» меньше допустимого",
                    }
                )
            if field.max_value is not None and decimal_value > Decimal(str(field.max_value)):
                issues.append(
                    {
                        "fieldCode": field.code,
                        "code": "max_value",
                        "message": f"Значение поля «{field.name}» больше допустимого",
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
            raw_values = value if field.multiple and isinstance(value, list) else [value]
            try:
                reference_ids = [UUID(str(item)) for item in raw_values]
            except (TypeError, ValueError):
                valid = False
            else:
                found = set(
                    await self._session.scalars(
                        select(EntityObjectModel.id).where(
                            EntityObjectModel.id.in_(reference_ids),
                            EntityObjectModel.entity_schema_id == field.reference_entity_schema_id,
                            EntityObjectModel.status != "archived",
                        )
                    )
                )
                valid = len(found) == len(set(reference_ids))
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
            FieldType.PHONE,
            FieldType.EMAIL,
            FieldType.URL,
        ):
            return isinstance(value, str)
        if field_type == FieldType.ENUM:
            return isinstance(value, str) or (
                isinstance(value, list)
                and all(isinstance(item, str) for item in value)
            )
        if field_type == FieldType.FILE:
            return isinstance(value, str) or (
                field.multiple and isinstance(value, list) and all(isinstance(item, str) for item in value)
            )
        if field_type == FieldType.INTEGER:
            return isinstance(value, int) and not isinstance(value, bool)
        if field_type == FieldType.DECIMAL:
            if isinstance(value, bool) or not isinstance(value, str | int | float):
                return False
            try:
                return Decimal(str(value)).is_finite()
            except InvalidOperation:
                return False
        if field_type == FieldType.BOOLEAN:
            return isinstance(value, bool)
        if field_type == FieldType.REFERENCE:
            values = value if field.multiple and isinstance(value, list) else [value]
            try:
                return all(bool(UUID(str(item))) for item in values)
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
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                return parsed.utcoffset() is not None
            except (TypeError, ValueError):
                return False
        if field_type == FieldType.CALCULATED:
            return isinstance(value, str | int | float | bool)
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
        return [
            {
                "Point": "point",
                "MultiPoint": "point",
                "LineString": "lineString",
                "MultiLineString": "lineString",
                "Polygon": "polygon",
                "MultiPolygon": "polygon",
            }[geometry.type]
        ]

    async def _filter_condition(
        self,
        schema: EntitySchemaModel,
        item: ObjectFilter,
        *,
        actor_roles: frozenset[str] | None = None,
    ) -> Any:
        if "." in item.field:
            return await self._related_filter_condition(
                schema,
                item,
                actor_roles=actor_roles,
            )

        system_fields = {
            "id": (cast(EntityObjectModel.id, String), "uuid"),
            "parentObjectId": (cast(EntityObjectModel.parent_object_id, String), "uuid"),
            "status": (EntityObjectModel.status, FieldType.STRING.value),
            "createdAt": (EntityObjectModel.created_at, FieldType.DATETIME.value),
            "updatedAt": (EntityObjectModel.updated_at, FieldType.DATETIME.value),
        }
        field = next(
            (
                candidate
                for candidate in schema.fields
                if candidate.code == item.field
                and candidate.filterable
                and not candidate.archived
                and self._field_is_readable(candidate, actor_roles)
            ),
            None,
        )
        system_field = system_fields.get(item.field)
        if system_field is None and field is None:
            self._invalid_filter(item.field, "Поле недоступно для фильтрации")
        if system_field is not None:
            expression, field_type = system_field
            return await self._typed_filter_condition(
                field_code=item.field,
                operator=item.operator,
                value=item.value,
                expression=expression,
                field_type=field_type,
            )
        assert field is not None
        raw_expression = EntityObjectModel.values[field.code]
        return await self._typed_filter_condition(
            field_code=item.field,
            operator=item.operator,
            value=item.value,
            expression=raw_expression.astext,
            raw_expression=raw_expression,
            field_type=field.field_type,
            field=field,
            multiple=field.multiple,
        )

    async def _related_filter_condition(
        self,
        schema: EntitySchemaModel,
        item: ObjectFilter,
        *,
        actor_roles: frozenset[str] | None = None,
    ) -> Any:
        relation_code, target_field_code = item.field.split(".", 1)
        if not relation_code or not target_field_code or "." in target_field_code:
            self._invalid_filter(item.field, "Поддерживается только один уровень связанного поля")

        target = aliased(EntityObjectModel)
        relation_field: EntityFieldModel | None = None
        if relation_code == "parent":
            target_entity_id = schema.parent_entity_schema_id
            link_condition = target.id == EntityObjectModel.parent_object_id
        else:
            relation_field = next(
                (
                    candidate
                    for candidate in schema.fields
                    if candidate.code == relation_code
                    and candidate.field_type == FieldType.REFERENCE.value
                    and candidate.filterable
                    and not candidate.archived
                    and self._field_is_readable(candidate, actor_roles)
                ),
                None,
            )
            target_entity_id = (
                relation_field.reference_entity_schema_id if relation_field is not None else None
            )
            if relation_field is None or target_entity_id is None:
                self._invalid_filter(item.field, "Связь для фильтрации не найдена")
            source_raw = EntityObjectModel.values[relation_field.code]
            link_condition = (
                source_raw.op("@>")(func.jsonb_build_array(cast(target.id, String)))
                if relation_field.multiple
                else source_raw.astext == cast(target.id, String)
            )

        if target_entity_id is None:
            self._invalid_filter(item.field, "У сущности нет родительского реестра")
        target_schema = await self._session.scalar(
            select(EntitySchemaModel).where(
                EntitySchemaModel.id == target_entity_id,
                EntitySchemaModel.status == "active",
            )
        )
        if target_schema is None:
            self._invalid_filter(item.field, "Связанная сущность не опубликована")

        target_system_fields = {
            "id": (cast(target.id, String), "uuid"),
            "parentObjectId": (cast(target.parent_object_id, String), "uuid"),
            "status": (target.status, FieldType.STRING.value),
            "createdAt": (target.created_at, FieldType.DATETIME.value),
            "updatedAt": (target.updated_at, FieldType.DATETIME.value),
        }
        target_system = target_system_fields.get(target_field_code)
        target_field = next(
            (
                candidate
                for candidate in target_schema.fields
                if candidate.code == target_field_code
                and candidate.filterable
                and not candidate.archived
                and self._field_is_readable(candidate, actor_roles)
            ),
            None,
        )
        if target_system is None and target_field is None:
            self._invalid_filter(item.field, "Поле связанной сущности недоступно для фильтрации")
        if target_system is not None:
            expression, field_type = target_system
            target_condition = await self._typed_filter_condition(
                field_code=item.field,
                operator=item.operator,
                value=item.value,
                expression=expression,
                field_type=field_type,
            )
        else:
            assert target_field is not None
            target_raw = target.values[target_field.code]
            target_condition = await self._typed_filter_condition(
                field_code=item.field,
                operator=item.operator,
                value=item.value,
                expression=target_raw.astext,
                raw_expression=target_raw,
                field_type=target_field.field_type,
                field=target_field,
                multiple=target_field.multiple,
            )
        return (
            select(literal(1))
            .select_from(target)
            .where(
                target.entity_schema_id == target_entity_id,
                target.status != "archived",
                link_condition,
                target_condition,
            )
            .exists()
        )

    async def _typed_filter_condition(
        self,
        *,
        field_code: str,
        operator: ObjectFilterOperator,
        value: str | None,
        expression: Any,
        field_type: str,
        raw_expression: Any | None = None,
        field: EntityFieldModel | None = None,
        multiple: bool = False,
    ) -> Any:
        if multiple:
            return await self._array_filter_condition(
                field_code=field_code,
                operator=operator,
                value=value,
                raw_expression=raw_expression,
                field=field,
            )
        if operator == ObjectFilterOperator.FILLED:
            return and_(expression.is_not(None), expression != "")
        if operator == ObjectFilterOperator.EMPTY:
            return or_(expression.is_(None), expression == "")

        needs_value = operator not in {
            ObjectFilterOperator.TODAY,
            ObjectFilterOperator.BEFORE_TODAY,
            ObjectFilterOperator.AFTER_TODAY,
        }
        if needs_value and (value is None or not value.strip()):
            self._invalid_filter(field_code, "Для оператора необходимо значение")
        normalized_value = (value or "").strip()
        enum_filter_values: list[str] | None = None
        if (
            field
            and field.field_type == FieldType.ENUM.value
            and normalized_value
            and operator
            in {
                ObjectFilterOperator.EQUALS,
                ObjectFilterOperator.NOT_EQUALS,
                ObjectFilterOperator.IN,
                ObjectFilterOperator.NOT_IN,
            }
        ):
            enum_filter_values = await self._normalize_enum_filter_values(
                field,
                operator.value,
                normalized_value,
            )
            normalized_value = enum_filter_values[0] if enum_filter_values else normalized_value

        if operator in {
            ObjectFilterOperator.CONTAINS,
            ObjectFilterOperator.STARTS_WITH,
            ObjectFilterOperator.ENDS_WITH,
        }:
            if field_type not in {
                FieldType.STRING.value,
                FieldType.TEXT.value,
                FieldType.ADDRESS.value,
                FieldType.EMAIL.value,
                FieldType.PHONE.value,
                FieldType.URL.value,
                FieldType.FILE.value,
            }:
                self._invalid_filter(field_code, "Текстовый поиск недоступен для этого типа поля")
            escaped = self._escape_like(normalized_value)
            patterns = {
                ObjectFilterOperator.CONTAINS: f"%{escaped}%",
                ObjectFilterOperator.STARTS_WITH: f"{escaped}%",
                ObjectFilterOperator.ENDS_WITH: f"%{escaped}",
            }
            return expression.ilike(patterns[operator], escape="\\")

        values: list[str] = []
        if operator in {ObjectFilterOperator.IN, ObjectFilterOperator.NOT_IN} or field_type in {
            "uuid",
            FieldType.REFERENCE.value,
        }:
            values = enum_filter_values or self._split_filter_values(normalized_value)
        if field_type in {"uuid", FieldType.REFERENCE.value}:
            values = [self._canonical_uuid(field_code, token) for token in values]
            normalized_value = values[0]
        if operator == ObjectFilterOperator.IN:
            return expression.in_(values)
        if operator == ObjectFilterOperator.NOT_IN:
            return and_(expression.is_not(None), not_(expression.in_(values)))
        if enum_filter_values and operator == ObjectFilterOperator.EQUALS:
            return expression.in_(enum_filter_values)
        if enum_filter_values and operator == ObjectFilterOperator.NOT_EQUALS:
            return and_(expression.is_not(None), not_(expression.in_(enum_filter_values)))

        comparable = expression
        expected: Any = normalized_value
        if field_type == FieldType.INTEGER.value:
            try:
                expected = int(normalized_value)
            except ValueError as error:
                self._invalid_filter(field_code, "Ожидается целое число", error)
            comparable = cast(expression, BigInteger())
        elif field_type == FieldType.DECIMAL.value:
            try:
                expected = Decimal(normalized_value)
            except InvalidOperation as error:
                self._invalid_filter(field_code, "Ожидается числовое значение", error)
            comparable = cast(expression, type_cast(Any, Numeric()))
        elif field_type == FieldType.BOOLEAN.value:
            lowered = normalized_value.casefold()
            if lowered not in {"true", "false"}:
                self._invalid_filter(field_code, "Ожидается true или false")
            expected = lowered == "true"
            comparable = cast(expression, Boolean())
        elif field_type == FieldType.DATE.value:
            comparable = cast(expression, type_cast(Any, Date()))
            if operator in {
                ObjectFilterOperator.TODAY,
                ObjectFilterOperator.BEFORE_TODAY,
                ObjectFilterOperator.AFTER_TODAY,
            }:
                expected = date.today()
            else:
                try:
                    expected = date.fromisoformat(normalized_value)
                except ValueError as error:
                    self._invalid_filter(field_code, "Ожидается дата в формате YYYY-MM-DD", error)
        elif field_type == FieldType.DATETIME.value:
            if operator in {
                ObjectFilterOperator.TODAY,
                ObjectFilterOperator.BEFORE_TODAY,
                ObjectFilterOperator.AFTER_TODAY,
            }:
                comparable = cast(expression, type_cast(Any, Date()))
                expected = date.today()
            else:
                comparable = cast(expression, DateTime(timezone=True))
                try:
                    expected = datetime.fromisoformat(normalized_value.replace("Z", "+00:00"))
                except ValueError as error:
                    self._invalid_filter(field_code, "Ожидается дата и время в ISO 8601", error)
                if expected.utcoffset() is None:
                    self._invalid_filter(
                        field_code,
                        "Для даты и времени необходимо указать часовой пояс: Z или ±HH:MM",
                    )
                expected = expected.astimezone(UTC)

        if operator in {
            ObjectFilterOperator.GREATER_THAN,
            ObjectFilterOperator.GREATER_OR_EQUAL,
            ObjectFilterOperator.LESS_THAN,
            ObjectFilterOperator.LESS_OR_EQUAL,
        } and field_type not in {
            FieldType.INTEGER.value,
            FieldType.DECIMAL.value,
            FieldType.DATE.value,
            FieldType.DATETIME.value,
        }:
            self._invalid_filter(field_code, "Сравнение по диапазону недоступно для этого типа поля")
        if operator in {
            ObjectFilterOperator.TODAY,
            ObjectFilterOperator.BEFORE_TODAY,
            ObjectFilterOperator.AFTER_TODAY,
        } and field_type not in {FieldType.DATE.value, FieldType.DATETIME.value}:
            self._invalid_filter(field_code, "Оператор доступен только для даты или даты и времени")

        if operator in {ObjectFilterOperator.EQUALS, ObjectFilterOperator.TODAY}:
            return comparable == expected
        if operator == ObjectFilterOperator.NOT_EQUALS:
            return and_(comparable.is_not(None), comparable != expected)
        if operator in {ObjectFilterOperator.GREATER_THAN, ObjectFilterOperator.AFTER_TODAY}:
            return comparable > expected
        if operator == ObjectFilterOperator.GREATER_OR_EQUAL:
            return comparable >= expected
        if operator in {ObjectFilterOperator.LESS_THAN, ObjectFilterOperator.BEFORE_TODAY}:
            return comparable < expected
        if operator == ObjectFilterOperator.LESS_OR_EQUAL:
            return comparable <= expected
        self._invalid_filter(field_code, "Оператор не поддерживается для поля")

    async def _array_filter_condition(
        self,
        *,
        field_code: str,
        operator: ObjectFilterOperator,
        value: str | None,
        raw_expression: Any,
        field: EntityFieldModel | None,
    ) -> Any:
        safe_array = case(
            (func.jsonb_typeof(raw_expression) == "array", raw_expression),
            else_=cast(literal("[]"), JSONB),
        )
        if operator == ObjectFilterOperator.FILLED:
            return func.jsonb_array_length(safe_array) > 0
        if operator == ObjectFilterOperator.EMPTY:
            return func.jsonb_array_length(safe_array) == 0
        if value is None or not value.strip():
            self._invalid_filter(field_code, "Для оператора необходимо значение")
        if operator not in {
            ObjectFilterOperator.EQUALS,
            ObjectFilterOperator.NOT_EQUALS,
            ObjectFilterOperator.IN,
            ObjectFilterOperator.NOT_IN,
            ObjectFilterOperator.CONTAINS,
            ObjectFilterOperator.STARTS_WITH,
            ObjectFilterOperator.ENDS_WITH,
        }:
            self._invalid_filter(field_code, "Оператор недоступен для множественного поля")

        values = self._split_filter_values(value)
        if field and field.field_type == FieldType.ENUM.value:
            values = await self._normalize_enum_filter_values(field, "in", value)
        elif field and field.field_type == FieldType.REFERENCE.value:
            values = [self._canonical_uuid(field_code, token) for token in values]

        if operator in {ObjectFilterOperator.EQUALS, ObjectFilterOperator.NOT_EQUALS}:
            condition = safe_array.contains([values[0]])
            return condition if operator == ObjectFilterOperator.EQUALS else not_(condition)
        if operator in {ObjectFilterOperator.IN, ObjectFilterOperator.NOT_IN}:
            condition = or_(*(safe_array.contains([token]) for token in values))
            return condition if operator == ObjectFilterOperator.IN else not_(condition)

        elements = func.jsonb_array_elements_text(safe_array).table_valued("value").alias()
        escaped = self._escape_like(values[0])
        patterns = {
            ObjectFilterOperator.CONTAINS: f"%{escaped}%",
            ObjectFilterOperator.STARTS_WITH: f"{escaped}%",
            ObjectFilterOperator.ENDS_WITH: f"%{escaped}",
        }
        return (
            select(literal(1))
            .select_from(elements)
            .where(elements.c.value.ilike(patterns[operator], escape="\\"))
            .exists()
        )

    @staticmethod
    def _split_filter_values(value: str) -> list[str]:
        values = [part.strip() for part in value.split(",") if part.strip()]
        if not values:
            RuntimeObjectService._invalid_filter(None, "Список значений пуст")
        return values

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _canonical_uuid(field_code: str | None, value: str) -> str:
        try:
            return str(UUID(value))
        except ValueError as error:
            RuntimeObjectService._invalid_filter(field_code, "Ожидается UUID", error)

    @staticmethod
    def _invalid_filter(
        field_code: str | None,
        message: str,
        cause: Exception | None = None,
    ) -> None:
        error = RuntimeValidationError(
            [{"fieldCode": field_code, "code": "invalid_filter", "message": message}]
        )
        if cause is not None:
            raise error from cause
        raise error

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

    def _sort_expression(
        self,
        schema: EntitySchemaModel,
        sort: str | None,
        *,
        actor_roles: frozenset[str] | None = None,
    ) -> Any:
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
        field = next(
            (
                candidate
                for candidate in schema.fields
                if candidate.code == key
                and not candidate.archived
                and self._field_is_readable(candidate, actor_roles)
            ),
            None,
        )
        if expression is None and field is not None:
            if field.multiple:
                raise RuntimeValidationError(
                    [
                        {
                            "fieldCode": key,
                            "code": "invalid_sort",
                            "message": "Множественное поле нельзя использовать для сортировки",
                        }
                    ]
                )
            expression = EntityObjectModel.values[field.code].astext
            if field.field_type in (FieldType.INTEGER.value, FieldType.DECIMAL.value):
                expression = cast(expression, Numeric())
            elif field.field_type == FieldType.BOOLEAN.value:
                expression = cast(expression, Boolean())
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
        *,
        actor_roles: frozenset[str] | None = None,
    ) -> EntityObjectRead:
        readable_codes = {
            field.code
            for field in schema.fields
            if not field.archived and self._field_is_readable(field, actor_roles)
        }
        values = {
            code: value
            for code, value in model.values.items()
            if code in readable_codes
        }
        display_values = await self._display_values(schema, values)
        validation_errors = [
            ValidationIssue.model_validate(item)
            for item in model.validation_errors
            if item.get("fieldCode", item.get("field_code")) in readable_codes
            or item.get("fieldCode", item.get("field_code")) is None
        ]
        return EntityObjectRead(
            id=model.id,
            entity_id=model.entity_schema_id,
            entity_code=schema.code,
            parent_object_id=model.parent_object_id,
            schema_version_id=model.schema_version_id,
            owner_organization_id=model.owner_organization_id,
            responsible_id=model.responsible_id,
            values=values,
            display_values=display_values,
            geometry=geometry,
            attachment_paths=model.attachment_paths,
            status=model.status,
            data_quality=model.data_quality,
            validation_errors=validation_errors,
            revision=model.revision,
            created_at=model.created_at,
            updated_at=model.updated_at,
            created_by=model.created_by,
            updated_by=model.updated_by,
            archived_at=model.archived_at,
        )

    @staticmethod
    def _field_is_readable(
        field: EntityFieldModel,
        actor_roles: frozenset[str] | None,
    ) -> bool:
        """Применить те же правила полей, что возвращает API capabilities."""

        allowed_roles = (field.access_rules or {}).get("read")
        return (
            allowed_roles is None
            or bool(actor_roles and set(allowed_roles).intersection(actor_roles))
        )

    @classmethod
    def _readable_search_field_codes(
        cls,
        schema: EntitySchemaModel,
        actor_roles: frozenset[str] | None,
    ) -> list[str]:
        """Ограничить полнотекстовый поиск доступными searchable-полями."""

        return [
            field.code
            for field in schema.fields
            if field.searchable
            and not field.archived
            and cls._field_is_readable(field, actor_roles)
        ]
