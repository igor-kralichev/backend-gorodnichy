import logging
from uuid import UUID

from redis.asyncio import Redis
from slugify import slugify
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.entities.api.schemas import (
    EntityCreate,
    EntityDeleteRead,
    EntityDuplicateCreate,
    EntityDraftPatch,
    EntityDraftRead,
    EntityDraftValidationRead,
    EntityFieldCreate,
    EntityListRead,
    EntityRead,
    EntityStatusRead,
    EntityUpdate,
    MapColorRuleCreate,
    MapSettingsCreate,
    MapStyle,
    MapStylesCreate,
)
from app.modules.entities.application.service import CreateEntitySchemaService
from app.modules.entities.domain.enums import EntityStatus, FieldType, GeometryType, MapGeometryType
from app.modules.entities.domain.errors import (
    EntitySchemaConflict,
    EntitySchemaError,
    EntitySchemaNotFound,
)
from app.modules.entities.infrastructure.models import (
    EntityAllowedGeometryTypeModel,
    EntityFieldModel,
    EntityLayerModel,
    EntityMapColorRuleModel,
    EntityMapStyleModel,
    EntitySchemaModel,
    EntitySchemaDraftModel,
    EntitySchemaVersionModel,
)
from app.shared.db.models import (
    AttachmentModel,
    DictionaryItemModel,
    DictionaryModel,
    EntityRelationModel,
    EntityObjectModel,
    FormDefinitionModel,
    ImportJobModel,
    ImportRowModel,
    ObjectEventModel,
    OutboxEventModel,
)

logger = logging.getLogger(__name__)


class EntitySchemaManagementService:
    def __init__(self, session: AsyncSession, redis: Redis) -> None:
        self._session = session
        self._redis = redis
        self._builder = CreateEntitySchemaService(session, redis)

    async def list(
        self,
        *,
        status_filter: EntityStatus | None,
        include_archived: bool,
        query: str | None,
        sort: str,
        limit: int,
        offset: int,
    ) -> EntityListRead:
        conditions = []
        if status_filter is not None:
            conditions.append(EntitySchemaModel.status == status_filter.value)
        elif not include_archived:
            conditions.append(EntitySchemaModel.status != EntityStatus.ARCHIVED.value)
        if query:
            pattern = f"%{query.strip()}%"
            conditions.append(
                or_(
                    EntitySchemaModel.name.ilike(pattern),
                    EntitySchemaModel.code.ilike(pattern),
                    EntitySchemaModel.description.ilike(pattern),
                )
            )
        sort_expressions = {
            "name": EntitySchemaModel.name.asc(),
            "-name": EntitySchemaModel.name.desc(),
            "updatedAt": EntitySchemaModel.updated_at.asc(),
            "-updatedAt": EntitySchemaModel.updated_at.desc(),
            "createdAt": EntitySchemaModel.created_at.asc(),
            "-createdAt": EntitySchemaModel.created_at.desc(),
        }

        total = int(
            await self._session.scalar(
                select(func.count())
                .select_from(EntitySchemaModel)
                .where(*conditions)
            )
            or 0
        )
        entities = list(
            await self._session.scalars(
                select(EntitySchemaModel)
                .where(*conditions)
                .order_by(sort_expressions[sort])
                .offset(offset)
                .limit(limit)
            )
        )
        data = [self._builder.to_response(entity) for entity in entities]
        return EntityListRead(
            total=total,
            returned=len(data),
            offset=offset,
            limit=limit,
            data=data,
        )

    async def get(self, identifier: str) -> EntityRead:
        entity = await self._find(identifier)
        return self._builder.to_response(entity)

    async def update(
        self,
        identifier: str,
        payload: EntityUpdate,
        actor_id: UUID,
    ) -> EntityRead:
        previous_version = 0
        entity_id: UUID | None = None
        async with self._session.begin():
            entity = await self._find(identifier, for_update=True)
            if entity.status == EntityStatus.ARCHIVED.value:
                raise EntitySchemaConflict(
                    "Архивную сущность необходимо сначала восстановить"
                )
            if entity.status == EntityStatus.ACTIVE.value:
                raise EntitySchemaConflict(
                    "Активную схему изменяют через отдельный draft с последующей публикацией"
                )

            command = self._merge_command(entity, payload)
            await self._builder.validate_references(command, target_entity_id=entity.id)
            await self._ensure_valid_parent_entity(entity.id, command.parent_entity_id)
            code = command.code or entity.code
            if code != entity.code:
                code = await self._builder.resolve_entity_code(code, explicit=True)

            prototype = self._builder.build_entity(command, code)
            await self._check_safe_schema_change(entity, prototype)
            await self._apply_prototype(entity, prototype, command)
            previous_version = entity.current_version
            entity.current_version += 1
            await self._session.flush()

            if entity.status == EntityStatus.ACTIVE.value:
                self._sync_layer(entity)
            await self._session.flush()
            await self._session.refresh(entity, attribute_names=["updated_at"])
            self._session.add(
                EntitySchemaVersionModel(
                    entity_schema_id=entity.id,
                    version=entity.current_version,
                    snapshot=self._builder.snapshot(entity),
                    created_by=actor_id,
                )
            )
            self._add_outbox(
                entity,
                "entity_schema.updated.v1",
                actor_id,
                {"previousVersion": previous_version},
            )
            await self._session.flush()
            await self._session.refresh(entity, attribute_names=["updated_at"])
            entity_id = entity.id
            response = self._builder.to_response(entity)

        await self._invalidate_cache(entity_id, previous_version)
        await self._builder.cache_schema(response)
        return response

    async def get_or_create_draft(
        self,
        identifier: str,
        actor_id: UUID,
    ) -> EntityDraftRead:
        async with self._session.begin():
            entity = await self._find(identifier, for_update=True)
            if entity.status == EntityStatus.ARCHIVED.value:
                raise EntitySchemaConflict("Для архивной сущности нельзя создать черновик")
            draft = await self._session.scalar(
                select(EntitySchemaDraftModel).where(
                    EntitySchemaDraftModel.entity_schema_id == entity.id
                )
            )
            if draft is None:
                draft = EntitySchemaDraftModel(
                    entity_schema_id=entity.id,
                    base_version=entity.current_version,
                    snapshot=self._to_command(entity).model_dump(mode="json", by_alias=True),
                    updated_by=actor_id,
                )
                self._session.add(draft)
                await self._session.flush()
                await self._session.refresh(draft)
        return self._draft_response(draft)

    async def update_draft(
        self,
        identifier: str,
        payload: EntityDraftPatch,
        actor_id: UUID,
    ) -> EntityDraftRead:
        async with self._session.begin():
            entity = await self._find(identifier, for_update=True)
            draft = await self._session.scalar(
                select(EntitySchemaDraftModel)
                .where(EntitySchemaDraftModel.entity_schema_id == entity.id)
                .with_for_update()
            )
            if draft is None:
                raise EntitySchemaConflict("Сначала получите черновик сущности")
            if (
                draft.base_version != payload.expected_version
                or entity.current_version != payload.expected_version
            ):
                raise EntitySchemaConflict("Active-схема изменилась; создайте новый черновик")
            command = EntityCreate.model_validate(draft.snapshot)
            changes = {
                name: getattr(payload.changes, name)
                for name in payload.changes.model_fields_set
            }
            command = EntityCreate.model_validate(
                command.model_copy(update=changes).model_dump(mode="python")
            )
            await self._builder.validate_references(command, target_entity_id=entity.id)
            await self._ensure_valid_parent_entity(entity.id, command.parent_entity_id)
            draft.snapshot = command.model_dump(mode="json", by_alias=True)
            draft.updated_by = actor_id
            await self._session.flush()
            await self._session.refresh(draft, attribute_names=["updated_at"])
        return self._draft_response(draft)

    async def validate_draft(self, identifier: str) -> EntityDraftValidationRead:
        entity = await self._find(identifier)
        draft = await self._session.scalar(
            select(EntitySchemaDraftModel).where(
                EntitySchemaDraftModel.entity_schema_id == entity.id
            )
        )
        if draft is None:
            raise EntitySchemaConflict("Черновик сущности не найден")
        issues: list[str] = []
        command = EntityCreate.model_validate(draft.snapshot)
        prototype = self._builder.build_entity(command, command.code or entity.code)
        try:
            await self._builder.validate_references(command, target_entity_id=entity.id)
            await self._ensure_valid_parent_entity(entity.id, command.parent_entity_id)
            await self._check_safe_schema_change(entity, prototype)
        except (EntitySchemaError, ValueError) as error:
            issues.append(str(error))
        object_count = int(
            await self._session.scalar(
                select(func.count()).select_from(EntityObjectModel).where(
                    EntityObjectModel.entity_schema_id == entity.id
                )
            )
            or 0
        )
        relation_count = int(
            await self._session.scalar(
                select(func.count()).select_from(EntityRelationModel).where(
                    or_(
                        EntityRelationModel.source_entity_schema_id == entity.id,
                        EntityRelationModel.target_entity_schema_id == entity.id,
                    )
                )
            )
            or 0
        )
        form_count = int(
            await self._session.scalar(
                select(func.count()).select_from(FormDefinitionModel).where(
                    FormDefinitionModel.entity_schema_id == entity.id
                )
            )
            or 0
        )
        old_fields = {field.code: field.field_type for field in entity.fields}
        new_fields = {field.code: field.field_type for field in prototype.fields}
        return EntityDraftValidationRead(
            valid=not issues,
            issues=issues,
            impact={
                "objects": object_count,
                "relations": relation_count,
                "forms": form_count,
                "addedFields": len(set(new_fields) - set(old_fields)),
                "removedFields": len(set(old_fields) - set(new_fields)),
                "changedFieldTypes": sum(
                    old_fields[code] != new_fields[code]
                    for code in set(old_fields) & set(new_fields)
                ),
            },
        )

    async def publish_draft(
        self,
        identifier: str,
        expected_version: int,
        actor_id: UUID,
    ) -> EntityRead:
        entity_id: UUID | None = None
        async with self._session.begin():
            entity = await self._find(identifier, for_update=True)
            draft = await self._session.scalar(
                select(EntitySchemaDraftModel)
                .where(EntitySchemaDraftModel.entity_schema_id == entity.id)
                .with_for_update()
            )
            if draft is None:
                raise EntitySchemaConflict("Черновик сущности не найден")
            if draft.base_version != expected_version or entity.current_version != expected_version:
                raise EntitySchemaConflict("Active-схема изменилась; публикация отменена")
            command = EntityCreate.model_validate(draft.snapshot)
            await self._builder.validate_references(command, target_entity_id=entity.id)
            await self._ensure_valid_parent_entity(entity.id, command.parent_entity_id)
            prototype = self._builder.build_entity(command, command.code or entity.code)
            await self._check_safe_schema_change(entity, prototype)
            previous_version = entity.current_version
            await self._apply_prototype(entity, prototype, command)
            entity.current_version += 1
            if entity.status == EntityStatus.ACTIVE.value:
                self._sync_layer(entity)
            self._session.add(
                EntitySchemaVersionModel(
                    entity_schema_id=entity.id,
                    version=entity.current_version,
                    snapshot=self._builder.snapshot(entity),
                    created_by=actor_id,
                )
            )
            await self._session.delete(draft)
            self._add_outbox(
                entity,
                "entity_schema.draft_published.v1",
                actor_id,
                {"previousVersion": previous_version},
            )
            await self._session.flush()
            entity_id = entity.id
            response = self._builder.to_response(entity)
        await self._invalidate_cache(entity_id, expected_version)
        await self._builder.cache_schema(response)
        return response

    @staticmethod
    def _draft_response(draft: EntitySchemaDraftModel) -> EntityDraftRead:
        return EntityDraftRead(
            id=draft.id,
            entity_id=draft.entity_schema_id,
            base_version=draft.base_version,
            draft_schema=EntityCreate.model_validate(draft.snapshot),
            updated_at=draft.updated_at,
        )

    async def publish(self, identifier: str, actor_id: UUID) -> EntityRead:
        previous_version = 0
        version_created = False
        entity_id: UUID | None = None
        async with self._session.begin():
            entity = await self._find(identifier, for_update=True)
            if entity.status == EntityStatus.ARCHIVED.value:
                raise EntitySchemaConflict(
                    "Архивную сущность нельзя опубликовать до восстановления"
                )
            if not entity.fields and not entity.geometry_types:
                raise EntitySchemaConflict(
                    "Сущность должна содержать хотя бы одно поле или геометрию"
                )
            if entity.status != EntityStatus.ACTIVE.value:
                previous_version = entity.current_version
                entity.current_version += 1
                version_created = True
            else:
                previous_version = entity.current_version
            entity.status = EntityStatus.ACTIVE.value
            self._sync_layer(entity)
            await self._session.flush()
            await self._session.refresh(entity, attribute_names=["updated_at"])
            if version_created:
                self._session.add(
                    EntitySchemaVersionModel(
                        entity_schema_id=entity.id,
                        version=entity.current_version,
                        snapshot=self._builder.snapshot(entity),
                        created_by=actor_id,
                    )
                )
            self._add_outbox(entity, "entity_schema.published.v1", actor_id)
            await self._session.flush()
            await self._session.refresh(entity, attribute_names=["updated_at"])
            entity_id = entity.id
            response = self._builder.to_response(entity)

        await self._invalidate_cache(entity_id, previous_version)
        await self._builder.cache_schema(response)
        return response

    async def archive(self, identifier: str, actor_id: UUID) -> EntityStatusRead:
        entity_id: UUID | None = None
        version = 0
        async with self._session.begin():
            entity = await self._find(identifier, for_update=True)
            if entity.status != EntityStatus.ARCHIVED.value:
                entity.status = EntityStatus.ARCHIVED.value
                if entity.layer is not None:
                    entity.layer.active = False
                self._add_outbox(entity, "entity_schema.archived.v1", actor_id)
                await self._session.flush()
                await self._session.refresh(entity, attribute_names=["updated_at"])
            entity_id = entity.id
            version = entity.current_version
            response = EntityStatusRead(
                id=entity.id,
                code=entity.code,
                status=EntityStatus(entity.status),
                updated_at=entity.updated_at,
            )
        await self._invalidate_cache(entity_id, version)
        return response

    async def restore(self, identifier: str, actor_id: UUID) -> EntityStatusRead:
        entity_id: UUID | None = None
        version = 0
        async with self._session.begin():
            entity = await self._find(identifier, for_update=True)
            if entity.status == EntityStatus.ARCHIVED.value:
                entity.status = EntityStatus.DRAFT.value
                if entity.layer is not None:
                    entity.layer.active = False
                self._add_outbox(entity, "entity_schema.restored.v1", actor_id)
                await self._session.flush()
                await self._session.refresh(entity, attribute_names=["updated_at"])
            entity_id = entity.id
            version = entity.current_version
            response = EntityStatusRead(
                id=entity.id,
                code=entity.code,
                status=EntityStatus(entity.status),
                updated_at=entity.updated_at,
            )
        await self._invalidate_cache(entity_id, version)
        return response

    async def delete(self, identifier: str, actor_id: UUID) -> EntityDeleteRead:
        entity_id: UUID | None = None
        version = 0
        async with self._session.begin():
            entity = await self._find(identifier, for_update=True)
            entity_id = entity.id
            entity_code = entity.code
            version = entity.current_version

            has_child_entities = bool(
                await self._session.scalar(
                    select(EntitySchemaModel.id)
                    .where(EntitySchemaModel.parent_entity_schema_id == entity.id)
                    .limit(1)
                )
            )
            if has_child_entities:
                raise EntitySchemaConflict(
                    "Нельзя удалить сущность, пока у неё есть дочерние сущности"
                )

            object_ids = list(
                await self._session.scalars(
                    select(EntityObjectModel.id).where(
                        EntityObjectModel.entity_schema_id == entity.id
                    )
                )
            )
            if object_ids:
                has_child_objects = bool(
                    await self._session.scalar(
                        select(EntityObjectModel.id)
                        .where(EntityObjectModel.parent_object_id.in_(object_ids))
                        .limit(1)
                    )
                )
                if has_child_objects:
                    raise EntitySchemaConflict(
                        "Нельзя удалить сущность, пока у её объектов есть вложенные объекты"
                    )
            import_job_ids = list(
                await self._session.scalars(
                    select(ImportJobModel.id).where(
                        ImportJobModel.entity_schema_id == entity.id
                    )
                )
            )
            dictionary_ids = list(
                await self._session.scalars(
                    select(DictionaryModel.id).where(
                        DictionaryModel.entity_schema_id == entity.id
                    )
                )
            )

            if object_ids:
                await self._session.execute(
                    update(ImportRowModel)
                    .where(ImportRowModel.object_id.in_(object_ids))
                    .values(object_id=None)
                )
                await self._session.execute(
                    delete(AttachmentModel).where(
                        AttachmentModel.object_id.in_(object_ids)
                    )
                )
                await self._session.execute(
                    delete(ObjectEventModel).where(
                        ObjectEventModel.object_id.in_(object_ids)
                    )
                )
                await self._session.execute(
                    delete(OutboxEventModel).where(
                        OutboxEventModel.aggregate_type == "entity_object",
                        OutboxEventModel.aggregate_id.in_(object_ids),
                    )
                )

            if import_job_ids:
                await self._session.execute(
                    delete(ImportRowModel).where(
                        ImportRowModel.import_job_id.in_(import_job_ids)
                    )
                )
                await self._session.execute(
                    delete(ImportJobModel).where(
                        ImportJobModel.id.in_(import_job_ids)
                    )
                )

            await self._session.execute(
                delete(EntityObjectModel).where(
                    EntityObjectModel.entity_schema_id == entity.id
                )
            )
            await self._session.execute(
                delete(OutboxEventModel).where(
                    OutboxEventModel.aggregate_type == "entity_schema",
                    OutboxEventModel.aggregate_id == entity.id,
                )
            )
            await self._session.execute(
                delete(EntityMapColorRuleModel).where(
                    EntityMapColorRuleModel.entity_schema_id == entity.id
                )
            )
            await self._session.execute(
                delete(EntityFieldModel).where(
                    EntityFieldModel.entity_schema_id == entity.id
                )
            )

            if dictionary_ids:
                await self._session.execute(
                    delete(DictionaryItemModel).where(
                        DictionaryItemModel.dictionary_id.in_(dictionary_ids)
                    )
                )
                await self._session.execute(
                    delete(DictionaryModel).where(
                        DictionaryModel.id.in_(dictionary_ids)
                    )
                )

            await self._session.execute(
                delete(EntityAllowedGeometryTypeModel).where(
                    EntityAllowedGeometryTypeModel.entity_schema_id == entity.id
                )
            )
            await self._session.execute(
                delete(EntityMapStyleModel).where(
                    EntityMapStyleModel.entity_schema_id == entity.id
                )
            )
            await self._session.execute(
                delete(EntityLayerModel).where(
                    EntityLayerModel.entity_schema_id == entity.id
                )
            )
            await self._session.execute(
                delete(EntitySchemaVersionModel).where(
                    EntitySchemaVersionModel.entity_schema_id == entity.id
                )
            )
            await self._session.execute(
                delete(EntitySchemaModel).where(EntitySchemaModel.id == entity.id)
            )
            self._session.add(
                OutboxEventModel(
                    aggregate_type="entity_schema",
                    aggregate_id=entity_id,
                    event_type="entity_schema.deleted.v1",
                    payload={
                        "entityId": str(entity_id),
                        "code": entity_code,
                        "version": version,
                        "actorId": str(actor_id),
                    },
                )
            )

        await self._invalidate_cache(entity_id, version)
        logger.info(
            "Сущность %s полностью удалена пользователем %s",
            entity_code,
            actor_id,
        )
        return EntityDeleteRead(id=entity_id, code=entity_code, deleted=True)

    async def duplicate(
        self,
        identifier: str,
        payload: EntityDuplicateCreate,
        actor_id: UUID,
    ) -> EntityRead:
        async with self._session.begin():
            source = await self._find(identifier)
            name = payload.name or f"{source.name} — копия"
            base_code = payload.code or (
                slugify(name, separator="_", lowercase=True, max_length=120)
                or f"{source.code}_copy"
            )
            command = self._to_command(source).model_copy(
                update={"code": base_code, "name": name}
            )
            await self._builder.validate_references(command)
            code = await self._builder.resolve_entity_code(
                base_code,
                explicit=payload.code is not None,
            )
            duplicate = self._builder.build_entity(command, code)
            duplicate.status = EntityStatus.DRAFT.value
            self._session.add(duplicate)
            await self._session.flush()
            self._session.add(
                EntitySchemaVersionModel(
                    entity_schema_id=duplicate.id,
                    version=1,
                    snapshot=self._builder.snapshot(duplicate),
                    created_by=actor_id,
                )
            )
            self._add_outbox(
                duplicate,
                "entity_schema.duplicated.v1",
                actor_id,
                {"sourceEntityId": str(source.id)},
            )
        response = self._builder.to_response(duplicate)
        await self._builder.cache_schema(response)
        return response

    async def _find(
        self,
        identifier: str,
        *,
        for_update: bool = False,
    ) -> EntitySchemaModel:
        conditions = [EntitySchemaModel.code == identifier]
        try:
            conditions.append(EntitySchemaModel.id == UUID(identifier))
        except ValueError:
            pass
        statement = select(EntitySchemaModel).where(or_(*conditions))
        if for_update:
            statement = statement.with_for_update()
        entity = await self._session.scalar(statement)
        if entity is None:
            raise EntitySchemaNotFound
        return entity

    async def _ensure_valid_parent_entity(
        self,
        entity_id: UUID,
        parent_entity_id: UUID | None,
    ) -> None:
        if parent_entity_id is None:
            return
        if parent_entity_id == entity_id:
            raise EntitySchemaConflict("Сущность не может быть родителем самой себе")

        current_parent_id = parent_entity_id
        visited: set[UUID] = set()
        while current_parent_id is not None:
            if current_parent_id in visited:
                raise EntitySchemaConflict("Обнаружен цикл в иерархии сущностей")
            if current_parent_id == entity_id:
                raise EntitySchemaConflict("Обнаружен цикл в иерархии сущностей")
            visited.add(current_parent_id)
            current_parent_id = await self._session.scalar(
                select(EntitySchemaModel.parent_entity_schema_id).where(
                    EntitySchemaModel.id == current_parent_id
                )
            )

    def _merge_command(
        self,
        entity: EntitySchemaModel,
        payload: EntityUpdate,
    ) -> EntityCreate:
        current = self._to_command(entity)
        updates: dict[str, object] = {}
        for field_name in (
            "code",
            "name",
            "description",
            "parent_entity_id",
            "geometry_type",
            "include_address",
            "fields",
            "map_settings",
            "scope_municipality_id",
            "owner_organization_id",
        ):
            if field_name in payload.model_fields_set:
                updates[field_name] = getattr(payload, field_name)
        if updates.get("map_settings") is None and "map_settings" in updates:
            updates["map_settings"] = MapSettingsCreate()
        return EntityCreate.model_validate(
            current.model_copy(update=updates).model_dump(mode="python")
        )

    def _to_command(self, entity: EntitySchemaModel) -> EntityCreate:
        styles = {
            MapGeometryType(style.geometry_type): MapStyle(
                fill=style.fill,
                stroke=style.stroke,
                stroke_width=float(style.stroke_width),
                point_size=float(style.point_size),
                opacity=float(style.opacity),
                marker_icon=style.marker_icon,
            )
            for style in entity.map_styles
        }
        fields_by_id = {field.id: field for field in entity.fields}
        return EntityCreate(
            code=entity.code,
            name=entity.name,
            description=entity.description,
            parent_entity_id=entity.parent_entity_schema_id,
            geometry_type=GeometryType(entity.geometry_type),
            include_address=any(
                field.field_type == FieldType.ADDRESS.value for field in entity.fields
            ),
            fields=[
                EntityFieldCreate(
                    id=field.id,
                    code=field.code,
                    name=field.name,
                    type=FieldType(field.field_type),
                    required=field.required,
                    list_visible=field.list_visible,
                    card_visible=field.card_visible,
                    searchable=field.searchable,
                    filterable=field.filterable,
                    enum_id=field.dictionary_id,
                    reference_entity_id=field.reference_entity_schema_id,
                    hint=field.hint,
                    default_value=field.default_value,
                    group=field.group_name,
                    min_length=field.min_length,
                    max_length=field.max_length,
                    min_value=float(field.min_value) if field.min_value is not None else None,
                    max_value=float(field.max_value) if field.max_value is not None else None,
                    unique=field.unique_value,
                    multiple=field.multiple,
                    read_only=field.read_only,
                    archived=field.archived,
                    access=field.access_rules,
                    formula=field.formula,
                    unit_code=field.unit_code,
                    decimal_scale=field.decimal_scale,
                )
                for field in sorted(entity.fields, key=lambda item: item.sort_order)
            ],
            map_settings=MapSettingsCreate(
                enabled_geometry_types=[
                    MapGeometryType(item.geometry_type)
                    for item in sorted(
                        entity.geometry_types,
                        key=lambda item: item.sort_order,
                    )
                ],
                clustering_enabled=entity.clustering_enabled,
                styles=MapStylesCreate(
                    point=styles.get(MapGeometryType.POINT),
                    line_string=styles.get(MapGeometryType.LINE_STRING),
                    polygon=styles.get(MapGeometryType.POLYGON),
                ),
                color_rules=[
                    MapColorRuleCreate(
                        name=rule.name,
                        field_code=fields_by_id[rule.entity_field_id].code,
                        operator=rule.operator,
                        value=rule.value,
                        color=rule.color,
                    )
                    for rule in sorted(
                        entity.color_rules,
                        key=lambda item: item.sort_order,
                    )
                ],
                selectable=entity.layer_selectable,
                visible_by_default=entity.layer_visible_by_default,
            ),
            scope_municipality_id=entity.scope_municipality_id,
            owner_organization_id=entity.owner_organization_id,
        )

    async def _check_safe_schema_change(
        self,
        entity: EntitySchemaModel,
        prototype: EntitySchemaModel,
    ) -> None:
        object_count = int(
            await self._session.scalar(
                select(func.count())
                .select_from(EntityObjectModel)
                .where(EntityObjectModel.entity_schema_id == entity.id)
            )
            or 0
        )
        if not object_count:
            return

        old_fields = {field.code: field.field_type for field in entity.fields}
        new_fields = {field.code: field.field_type for field in prototype.fields}
        removed_fields = set(old_fields) - set(new_fields)
        changed_types = {
            code
            for code in set(old_fields) & set(new_fields)
            if old_fields[code] != new_fields[code]
        }
        old_geometries = {item.geometry_type for item in entity.geometry_types}
        new_geometries = {item.geometry_type for item in prototype.geometry_types}
        if removed_fields or changed_types or not old_geometries.issubset(new_geometries):
            raise EntitySchemaConflict(
                "Нельзя удалить поле, изменить его тип или запретить используемую "
                "геометрию, пока у сущности есть объекты"
            )

    async def _apply_prototype(
        self,
        entity: EntitySchemaModel,
        prototype: EntitySchemaModel,
        command: EntityCreate,
    ) -> None:
        entity.code = prototype.code
        entity.name = prototype.name
        entity.description = prototype.description
        entity.parent_entity_schema_id = prototype.parent_entity_schema_id
        entity.geometry_type = prototype.geometry_type
        entity.clustering_enabled = prototype.clustering_enabled
        entity.scope_municipality_id = prototype.scope_municipality_id
        entity.owner_organization_id = prototype.owner_organization_id
        entity.layer_selectable = prototype.layer_selectable
        entity.layer_visible_by_default = prototype.layer_visible_by_default

        entity.color_rules.clear()
        for index, field in enumerate(entity.fields, start=1):
            field.sort_order = 1000 + index
        await self._session.flush()

        existing_fields = {field.code: field for field in entity.fields}
        applied_fields: list[EntityFieldModel] = []
        for desired_field in prototype.fields:
            applied_field = existing_fields.get(desired_field.code)
            if applied_field is None:
                applied_field = EntityFieldModel(id=desired_field.id)
            applied_field.code = desired_field.code
            applied_field.name = desired_field.name
            applied_field.field_type = desired_field.field_type
            applied_field.required = desired_field.required
            applied_field.list_visible = desired_field.list_visible
            applied_field.card_visible = desired_field.card_visible
            applied_field.searchable = desired_field.searchable
            applied_field.filterable = desired_field.filterable
            applied_field.sort_order = desired_field.sort_order
            applied_field.dictionary_id = desired_field.dictionary_id
            applied_field.reference_entity_schema_id = (
                desired_field.reference_entity_schema_id
            )
            applied_field.hint = desired_field.hint
            applied_field.default_value = desired_field.default_value
            applied_field.group_name = desired_field.group_name
            applied_field.min_length = desired_field.min_length
            applied_field.max_length = desired_field.max_length
            applied_field.min_value = desired_field.min_value
            applied_field.max_value = desired_field.max_value
            applied_field.unique_value = desired_field.unique_value
            applied_field.multiple = desired_field.multiple
            applied_field.read_only = desired_field.read_only
            applied_field.archived = desired_field.archived
            applied_field.access_rules = desired_field.access_rules
            applied_field.formula = desired_field.formula
            applied_field.unit_code = desired_field.unit_code
            applied_field.decimal_scale = desired_field.decimal_scale
            applied_fields.append(applied_field)
        entity.fields = applied_fields

        entity.geometry_types.clear()
        await self._session.flush()
        entity.geometry_types = [
            EntityAllowedGeometryTypeModel(
                geometry_type=item.geometry_type,
                sort_order=item.sort_order,
            )
            for item in prototype.geometry_types
        ]

        existing_styles = {style.geometry_type: style for style in entity.map_styles}
        for desired_style in prototype.map_styles:
            applied_style = existing_styles.get(desired_style.geometry_type)
            if applied_style is None:
                applied_style = EntityMapStyleModel(
                    geometry_type=desired_style.geometry_type
                )
                entity.map_styles.append(applied_style)
            applied_style.fill = desired_style.fill
            applied_style.stroke = desired_style.stroke
            applied_style.stroke_width = desired_style.stroke_width
            applied_style.point_size = desired_style.point_size
            applied_style.opacity = desired_style.opacity
            applied_style.marker_icon = desired_style.marker_icon

        fields_by_code = {field.code: field for field in entity.fields}
        entity.color_rules = [
            EntityMapColorRuleModel(
                entity_field_id=fields_by_code[rule.field_code].id,
                name=rule.name,
                operator=rule.operator.value,
                value=rule.value,
                color=rule.color,
                sort_order=index,
            )
            for index, rule in enumerate(
                command.map_settings.color_rules if command.map_settings else [],
                start=1,
            )
        ]

    def _sync_layer(self, entity: EntitySchemaModel) -> None:
        if not entity.geometry_types:
            if entity.layer is not None:
                entity.layer.active = False
            return

        fields_by_id = {field.id: field for field in entity.fields}
        styles: dict[str, dict[str, str | float]] = {
            style.geometry_type: {
                "fill": style.fill,
                "stroke": style.stroke,
                "strokeWidth": float(style.stroke_width),
                "pointSize": float(style.point_size),
                "opacity": float(style.opacity),
            }
            for style in entity.map_styles
        }
        primary_geometry = (
            entity.geometry_type
            if entity.geometry_type != GeometryType.NONE.value
            else entity.geometry_types[0].geometry_type
        )
        primary_style = styles[primary_geometry]
        style_payload = {
            "geometryTypes": [
                item.geometry_type
                for item in sorted(
                    entity.geometry_types,
                    key=lambda item: item.sort_order,
                )
            ],
            "styles": styles,
            "clusteringEnabled": entity.clustering_enabled,
            "colorRules": [
                {
                    "name": rule.name,
                    "fieldCode": fields_by_id[rule.entity_field_id].code,
                    "operator": rule.operator,
                    "value": rule.value,
                    "color": rule.color,
                }
                for rule in sorted(
                    entity.color_rules,
                    key=lambda item: item.sort_order,
                )
            ],
        }
        if entity.layer is None:
            entity.layer = EntityLayerModel(
                code=entity.code,
                name=entity.name,
                style=style_payload,
                opacity=float(primary_style["opacity"]),
                selectable=entity.layer_selectable,
                visible_by_default=entity.layer_visible_by_default,
                active=True,
            )
            return
        entity.layer.code = entity.code
        entity.layer.name = entity.name
        entity.layer.style = style_payload
        entity.layer.opacity = float(primary_style["opacity"])
        entity.layer.selectable = entity.layer_selectable
        entity.layer.visible_by_default = entity.layer_visible_by_default
        entity.layer.active = True

    def _add_outbox(
        self,
        entity: EntitySchemaModel,
        event_type: str,
        actor_id: UUID,
        extra: dict[str, object] | None = None,
    ) -> None:
        self._session.add(
            OutboxEventModel(
                aggregate_type="entity_schema",
                aggregate_id=entity.id,
                event_type=event_type,
                payload={
                    "entityId": str(entity.id),
                    "code": entity.code,
                    "version": entity.current_version,
                    "actorId": str(actor_id),
                    **(extra or {}),
                },
            )
        )

    async def _invalidate_cache(
        self,
        entity_id: UUID | None,
        previous_version: int,
    ) -> None:
        if entity_id is None:
            return
        try:
            await self._redis.delete(
                f"entity-schema:{entity_id}:v{previous_version}"
            )
        except Exception:
            logger.exception("Не удалось удалить устаревшую схему из Redis")
