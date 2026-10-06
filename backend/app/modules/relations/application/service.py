from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import ActorContext
from app.modules.access.application.service import AuthorizationService
from app.modules.entities.infrastructure.models import EntitySchemaModel
from app.modules.relations.api.schemas import (
    EntityRelationCreate,
    EntityRelationRead,
    EntityRelationUpdate,
    ObjectRelationCreate,
    ObjectRelationPatch,
    ObjectRelationRead,
    RelationFieldDefinition,
)
from app.shared.db.models import (
    AuditEventModel,
    EntityObjectModel,
    EntityRelationModel,
    ObjectRelationModel,
    OutboxEventModel,
)


class RelationNotFound(Exception):
    pass


class RelationValidationError(Exception):
    pass


class RelationConflict(Exception):
    pass


class RelationService:
    """Управляет типами связей и отдельными связующими объектами."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_definition(
        self, payload: EntityRelationCreate, actor: ActorContext
    ) -> EntityRelationRead:
        async with self._session.begin():
            await self._ensure_entity(payload.source_entity_schema_id)
            await self._ensure_entity(payload.target_entity_schema_id)
            model = EntityRelationModel(
                code=payload.code,
                name=payload.name.strip(),
                source_entity_schema_id=payload.source_entity_schema_id,
                target_entity_schema_id=payload.target_entity_schema_id,
                source_cardinality=payload.source_cardinality,
                target_cardinality=payload.target_cardinality,
                field_definitions=[field.model_dump(mode="json", by_alias=True) for field in payload.fields],
            )
            self._session.add(model)
            await self._session.flush()
            self._track(
                actor,
                model,
                "relation_definition.created",
                None,
                self._definition_snapshot(model),
            )
            await self._session.refresh(model)
        return self._definition(model)

    async def list_definitions(self, entity_id: UUID | None) -> list[EntityRelationRead]:
        conditions = [EntityRelationModel.active.is_(True)]
        if entity_id is not None:
            conditions.append(
                or_(
                    EntityRelationModel.source_entity_schema_id == entity_id,
                    EntityRelationModel.target_entity_schema_id == entity_id,
                )
            )
        rows = (
            await self._session.scalars(
                select(EntityRelationModel).where(*conditions).order_by(EntityRelationModel.name)
            )
        ).all()
        return [self._definition(row) for row in rows]

    async def update_definition(
        self,
        relation_id: UUID,
        payload: EntityRelationUpdate,
        actor: ActorContext,
    ) -> EntityRelationRead:
        async with self._session.begin():
            model = await self._definition_model(relation_id, for_update=True)
            before = self._definition_snapshot(model)
            if payload.name is not None:
                model.name = payload.name.strip()
            if payload.fields is not None:
                model.field_definitions = [field.model_dump(mode="json", by_alias=True) for field in payload.fields]
            if payload.active is not None:
                model.active = payload.active
            await self._session.flush()
            self._track(
                actor,
                model,
                "relation_definition.updated",
                before,
                self._definition_snapshot(model),
            )
            await self._session.refresh(model)
        return self._definition(model)

    async def create_link(
        self, payload: ObjectRelationCreate, actor: ActorContext
    ) -> ObjectRelationRead:
        async with self._session.begin():
            definition = await self._definition_model(payload.relation_id)
            source = await self._object(payload.source_object_id, definition.source_entity_schema_id)
            target = await self._object(payload.target_object_id, definition.target_entity_schema_id)
            authorization = AuthorizationService(self._session)
            await authorization.require(
                actor,
                "update",
                organization_id=source.owner_organization_id,
                entity_schema_id=definition.source_entity_schema_id,
                entity_object_id=source.id,
            )
            await authorization.require(
                actor,
                "read",
                organization_id=target.owner_organization_id,
                entity_schema_id=definition.target_entity_schema_id,
                entity_object_id=target.id,
            )
            await self._validate_cardinality(definition, payload.source_object_id, payload.target_object_id)
            values = self._validate_values(definition, payload.values)
            model = ObjectRelationModel(
                relation_id=definition.id,
                source_object_id=payload.source_object_id,
                target_object_id=payload.target_object_id,
                values=values,
                attachment_paths=[],
                created_by=actor.id,
                updated_by=actor.id,
            )
            self._session.add(model)
            await self._session.flush()
            self._track(
                actor,
                model,
                "relation.created",
                None,
                self._link_snapshot(model),
            )
            await self._session.refresh(model)
        return self._link(model)

    async def list_links(
        self, *, object_id: UUID, relation_id: UUID | None, actor: ActorContext
    ) -> list[ObjectRelationRead]:
        record = await self._session.get(EntityObjectModel, object_id)
        if record is None:
            raise RelationNotFound
        await AuthorizationService(self._session).require(
            actor,
            "read",
            organization_id=record.owner_organization_id,
            entity_schema_id=record.entity_schema_id,
            entity_object_id=record.id,
        )
        conditions = [
            or_(
                ObjectRelationModel.source_object_id == object_id,
                ObjectRelationModel.target_object_id == object_id,
            )
        ]
        if relation_id is not None:
            conditions.append(ObjectRelationModel.relation_id == relation_id)
        rows = (
            await self._session.scalars(
                select(ObjectRelationModel)
                .where(*conditions)
                .order_by(ObjectRelationModel.created_at.desc())
            )
        ).all()
        return [self._link(row) for row in rows]

    async def update_link(
        self, link_id: UUID, payload: ObjectRelationPatch, actor: ActorContext
    ) -> ObjectRelationRead:
        async with self._session.begin():
            model = await self._link_model(link_id, for_update=True)
            if model.revision != payload.revision:
                raise RelationConflict
            definition = await self._definition_model(model.relation_id)
            source = await self._object(model.source_object_id, definition.source_entity_schema_id)
            await AuthorizationService(self._session).require(
                actor,
                "update",
                organization_id=source.owner_organization_id,
                entity_schema_id=definition.source_entity_schema_id,
                entity_object_id=source.id,
            )
            before = self._link_snapshot(model)
            model.values = self._validate_values(definition, payload.values)
            model.revision += 1
            model.updated_by = actor.id
            await self._session.flush()
            self._track(
                actor,
                model,
                "relation.updated",
                before,
                self._link_snapshot(model),
            )
            await self._session.refresh(model)
        return self._link(model)

    async def delete_link(self, link_id: UUID, actor: ActorContext) -> None:
        async with self._session.begin():
            model = await self._link_model(link_id, for_update=True)
            definition = await self._definition_model(model.relation_id)
            source = await self._object(
                model.source_object_id, definition.source_entity_schema_id
            )
            await AuthorizationService(self._session).require(
                actor,
                "update",
                organization_id=source.owner_organization_id,
                entity_schema_id=definition.source_entity_schema_id,
                entity_object_id=source.id,
            )
            before = self._link_snapshot(model)
            self._track(actor, model, "relation.deleted", before, None)
            await self._session.delete(model)

    async def _validate_cardinality(
        self, definition: EntityRelationModel, source_id: UUID, target_id: UUID
    ) -> None:
        conditions = [ObjectRelationModel.relation_id == definition.id]
        if definition.target_cardinality == "one":
            exists = await self._session.scalar(
                select(ObjectRelationModel.id).where(*conditions, ObjectRelationModel.source_object_id == source_id)
            )
            if exists is not None:
                raise RelationValidationError("Для исходного объекта уже существует связь этого типа")
        if definition.source_cardinality == "one":
            exists = await self._session.scalar(
                select(ObjectRelationModel.id).where(*conditions, ObjectRelationModel.target_object_id == target_id)
            )
            if exists is not None:
                raise RelationValidationError("Для целевого объекта уже существует связь этого типа")

    @staticmethod
    def _validate_values(
        definition: EntityRelationModel, values: dict[str, Any]
    ) -> dict[str, Any]:
        fields = {
            item.code: item
            for raw in definition.field_definitions
            for item in [RelationFieldDefinition.model_validate(raw)]
        }
        unknown = sorted(set(values) - set(fields))
        if unknown:
            raise RelationValidationError("Неизвестные поля связи: " + ", ".join(unknown))
        for field in fields.values():
            value = values.get(field.code)
            if field.required and value in (None, "", []):
                raise RelationValidationError(f"Поле связи «{field.name}» обязательно")
            if value not in (None, "", []) and not _valid_value(field.type, value):
                raise RelationValidationError(f"Некорректный тип поля связи «{field.name}»")
        return values

    async def _ensure_entity(self, entity_id: UUID) -> None:
        exists = await self._session.scalar(select(EntitySchemaModel.id).where(EntitySchemaModel.id == entity_id))
        if exists is None:
            raise RelationValidationError("Сущность связи не найдена")

    async def _object(self, object_id: UUID, entity_id: UUID) -> EntityObjectModel:
        model = await self._session.scalar(
            select(EntityObjectModel).where(
                EntityObjectModel.id == object_id,
                EntityObjectModel.entity_schema_id == entity_id,
                EntityObjectModel.status != "archived",
            )
        )
        if model is None:
            raise RelationValidationError("Объект связи не найден или имеет неверный тип")
        return model

    async def _definition_model(
        self, relation_id: UUID, *, for_update: bool = False
    ) -> EntityRelationModel:
        statement = select(EntityRelationModel).where(EntityRelationModel.id == relation_id)
        if for_update:
            statement = statement.with_for_update()
        model = await self._session.scalar(statement)
        if model is None:
            raise RelationNotFound
        return model

    async def _link_model(self, link_id: UUID, *, for_update: bool) -> ObjectRelationModel:
        statement = select(ObjectRelationModel).where(ObjectRelationModel.id == link_id)
        if for_update:
            statement = statement.with_for_update()
        model = await self._session.scalar(statement)
        if model is None:
            raise RelationNotFound
        return model

    @staticmethod
    def _definition(model: EntityRelationModel) -> EntityRelationRead:
        return EntityRelationRead(
            id=model.id,
            code=model.code,
            name=model.name,
            source_entity_schema_id=model.source_entity_schema_id,
            target_entity_schema_id=model.target_entity_schema_id,
            source_cardinality=model.source_cardinality,
            target_cardinality=model.target_cardinality,
            fields=[RelationFieldDefinition.model_validate(item) for item in model.field_definitions],
            active=model.active,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    @staticmethod
    def _link(model: ObjectRelationModel) -> ObjectRelationRead:
        return ObjectRelationRead.model_validate(model, from_attributes=True)

    @staticmethod
    def _definition_snapshot(model: EntityRelationModel) -> dict[str, Any]:
        return {
            "code": model.code,
            "name": model.name,
            "sourceEntitySchemaId": str(model.source_entity_schema_id),
            "targetEntitySchemaId": str(model.target_entity_schema_id),
            "sourceCardinality": model.source_cardinality,
            "targetCardinality": model.target_cardinality,
            "fields": model.field_definitions,
            "active": model.active,
        }

    @staticmethod
    def _link_snapshot(model: ObjectRelationModel) -> dict[str, Any]:
        return {
            "relationId": str(model.relation_id),
            "sourceObjectId": str(model.source_object_id),
            "targetObjectId": str(model.target_object_id),
            "values": model.values,
            "attachmentPaths": model.attachment_paths,
            "revision": model.revision,
        }

    def _track(
        self,
        actor: ActorContext,
        model: EntityRelationModel | ObjectRelationModel,
        action: str,
        old_value: dict[str, Any] | None,
        new_value: dict[str, Any] | None,
    ) -> None:
        changes = []
        for key in sorted(set(old_value or {}) | set(new_value or {})):
            old = (old_value or {}).get(key)
            new = (new_value or {}).get(key)
            if old != new:
                changes.append({"path": key, "oldValue": old, "newValue": new})
        resource_code = model.code if isinstance(model, EntityRelationModel) else None
        resource_name = model.name if isinstance(model, EntityRelationModel) else None
        self._session.add(
            AuditEventModel(
                resource_type="relation",
                resource_id=model.id,
                resource_code=resource_code,
                resource_name=resource_name,
                action=action,
                actor_id=actor.id,
                actor_full_name=actor.display_name or actor.username or actor.email,
                actor_email=actor.email,
                old_value=old_value,
                new_value=new_value,
                changes=changes,
                metadata_json={"relationKind": "definition" if resource_code else "object"},
            )
        )
        self._session.add(
            OutboxEventModel(
                aggregate_type="relation",
                aggregate_id=model.id,
                event_type=f"{action}.v1",
                payload={
                    "relationId": str(model.id),
                    "actorId": str(actor.id),
                    "occurredAt": datetime.now(UTC).isoformat(),
                },
            )
        )


def _valid_value(field_type: str, value: Any) -> bool:
    if field_type in {"string", "text", "url", "file"}:
        return isinstance(value, str)
    if field_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if field_type == "decimal":
        return isinstance(value, int | float | Decimal) and not isinstance(value, bool)
    if field_type == "boolean":
        return isinstance(value, bool)
    if field_type == "date":
        try:
            date.fromisoformat(str(value))
            return True
        except ValueError:
            return False
    if field_type == "datetime":
        try:
            datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return True
        except ValueError:
            return False
    return False
