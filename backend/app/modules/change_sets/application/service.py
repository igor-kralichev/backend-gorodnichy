from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import ActorContext
from app.modules.change_sets.api.schemas import (
    ChangeSetCreate,
    ChangeSetItemCreate,
    ChangeSetItemRead,
    ChangeSetRead,
)
from app.modules.entities.infrastructure.models import EntitySchemaModel, EntitySchemaVersionModel
from app.modules.objects.api.schemas import EntityObjectCreate, ValidationIssue
from app.modules.objects.application.service import (
    RuntimeObjectNotFound,
    RuntimeObjectService,
    RuntimeValidationError,
)
from app.shared.db.models import (
    AuditEventModel,
    ChangeSetItemModel,
    ChangeSetModel,
    EntityObjectModel,
    FieldConfirmationModel,
    ObjectEventModel,
    OutboxEventModel,
)


class ChangeSetNotFound(Exception):
    pass


class ChangeSetConflict(Exception):
    def __init__(self, message: str = "Данные изменились после предварительной проверки") -> None:
        super().__init__(message)


class ChangeSetValidationError(Exception):
    pass


class ChangeSetService:
    """Готовит, проверяет и атомарно применяет набор предметных изменений."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, payload: ChangeSetCreate, actor: ActorContext) -> ChangeSetRead:
        request_hash = self._request_hash(payload)
        existing = None
        if payload.idempotency_key:
            async with self._session.begin():
                existing = await self._session.scalar(
                    select(ChangeSetModel).where(
                        ChangeSetModel.idempotency_key == payload.idempotency_key
                    )
                )
        if existing is not None:
            if existing.metadata_json.get("__requestHash") != request_hash:
                raise ChangeSetConflict(
                    "Idempotency-Key уже использован для другого набора изменений"
                )
            return await self.get(existing.id)

        async with self._session.begin():
            schema = await self._schema(payload.entity_code)
            version_id = await self._version_id(schema.id)
            change_set = ChangeSetModel(
                entity_schema_id=schema.id,
                schema_version_id=version_id,
                source=payload.source,
                status="draft",
                idempotency_key=payload.idempotency_key,
                created_by=actor.id,
                metadata_json={**payload.metadata, "__requestHash": request_hash},
            )
            self._session.add(change_set)
            await self._session.flush()
            runtime = RuntimeObjectService(self._session)
            all_valid = True
            for proposed in payload.items:
                item = await self._build_item(
                    runtime,
                    schema,
                    change_set.id,
                    proposed,
                    actor,
                )
                all_valid = all_valid and item.status == "valid"
                self._session.add(item)
            change_set.status = "validated" if all_valid else "draft"
            self._track(actor, change_set, "prepared", None, {"status": change_set.status})
            self._add_outbox(change_set, "change_set.prepared.v1", actor.id)
            await self._session.flush()
        return await self.get(change_set.id)

    async def get(self, change_set_id: UUID) -> ChangeSetRead:
        model = await self._session.get(ChangeSetModel, change_set_id)
        if model is None:
            raise ChangeSetNotFound
        items = (
            await self._session.scalars(
                select(ChangeSetItemModel)
                .where(ChangeSetItemModel.change_set_id == model.id)
                .order_by(ChangeSetItemModel.id)
            )
        ).all()
        return self._response(model, items)

    async def apply(self, change_set_id: UUID, actor: ActorContext) -> ChangeSetRead:
        conflict = False
        async with self._session.begin():
            change_set = await self._session.scalar(
                select(ChangeSetModel).where(ChangeSetModel.id == change_set_id).with_for_update()
            )
            if change_set is None:
                raise ChangeSetNotFound
            if change_set.status == "applied":
                return await self.get(change_set.id)
            if change_set.status != "validated":
                raise ChangeSetValidationError("Набор изменений не прошёл проверку")
            schema = await self._session.get(EntitySchemaModel, change_set.entity_schema_id)
            if schema is None or schema.status != "active":
                raise ChangeSetValidationError("Опубликованная сущность недоступна")
            current_schema_version_id = await self._version_id(schema.id)
            if current_schema_version_id != change_set.schema_version_id:
                raise ChangeSetConflict(
                    "Схема сущности изменилась после подготовки набора; "
                    "подготовьте набор повторно по текущей версии"
                )
            items = list(
                await self._session.scalars(
                    select(ChangeSetItemModel)
                    .where(ChangeSetItemModel.change_set_id == change_set.id)
                    .order_by(ChangeSetItemModel.id)
                    .with_for_update()
                )
            )
            for item in items:
                if item.status != "valid" or item.operation == "create":
                    continue
                target = await self._session.scalar(
                    select(EntityObjectModel)
                    .where(
                        EntityObjectModel.id == item.object_id,
                        EntityObjectModel.entity_schema_id == schema.id,
                    )
                    .with_for_update()
                )
                if target is None or (
                    item.base_revision is not None and target.revision != item.base_revision
                ):
                    item.status = "conflict"
                    conflict = True
            if conflict:
                change_set.status = "conflict"
                change_set.decided_by = actor.id
                change_set.decided_at = datetime.now(UTC)
                self._track(
                    actor,
                    change_set,
                    "conflict",
                    {"status": "validated"},
                    {"status": "conflict"},
                )
                self._add_outbox(change_set, "change_set.conflict.v1", actor.id)
            else:
                runtime = RuntimeObjectService(self._session)
                for item in items:
                    if item.status != "valid":
                        continue
                    await self._apply_item(runtime, schema, item, actor)
                change_set.status = "applied"
                change_set.decided_by = actor.id
                change_set.decided_at = datetime.now(UTC)
                self._track(
                    actor,
                    change_set,
                    "applied",
                    {"status": "validated"},
                    {"status": "applied"},
                )
                self._add_outbox(change_set, "change_set.applied.v1", actor.id)
                await self._session.flush()
        if conflict:
            raise ChangeSetConflict
        return await self.get(change_set_id)

    async def decide(
        self,
        change_set_id: UUID,
        status: str,
        actor: ActorContext,
    ) -> ChangeSetRead:
        async with self._session.begin():
            model = await self._session.scalar(
                select(ChangeSetModel).where(ChangeSetModel.id == change_set_id).with_for_update()
            )
            if model is None:
                raise ChangeSetNotFound
            if model.status == "applied":
                raise ChangeSetValidationError("Применённый набор нельзя изменить")
            before_status = model.status
            model.status = status
            model.decided_by = actor.id
            model.decided_at = datetime.now(UTC)
            self._track(
                actor,
                model,
                status,
                {"status": before_status},
                {"status": status},
            )
            self._add_outbox(model, f"change_set.{status}.v1", actor.id)
        return await self.get(change_set_id)

    async def _build_item(
        self,
        runtime: RuntimeObjectService,
        schema: EntitySchemaModel,
        change_set_id: UUID,
        proposed: ChangeSetItemCreate,
        actor: ActorContext,
    ) -> ChangeSetItemModel:
        values = proposed.values
        geometry = proposed.geometry
        parent_object_id = proposed.parent_object_id
        parent_errors: list[dict[str, str | None]] = []
        if proposed.operation in {"create", "update"}:
            runtime._ensure_writable_fields(  # noqa: SLF001
                schema,
                proposed.values,
                action=proposed.operation,
                actor_roles=actor.roles,
            )
        if proposed.operation != "create":
            try:
                current, current_geometry = await runtime._get_object(schema.id, proposed.object_id)  # noqa: SLF001
            except RuntimeObjectNotFound:
                return ChangeSetItemModel(
                    change_set_id=change_set_id,
                    object_id=proposed.object_id,
                    operation=proposed.operation,
                    base_revision=proposed.base_revision,
                    proposed_values=values,
                    validation_errors=[{"fieldCode": None, "code": "not_found", "message": "Объект не найден"}],
                    status="invalid",
                )
            if proposed.operation in {"update", "confirm"}:
                values = {**current.values, **values}
                geometry = geometry or current_geometry
                if parent_object_id is None:
                    parent_object_id = current.parent_object_id
        if proposed.operation in {"create", "update"}:
            try:
                parent_object_id = await runtime._resolve_parent_object_id(  # noqa: SLF001
                    schema,
                    parent_object_id,
                )
            except RuntimeValidationError as error:
                parent_errors.extend(error.issues)
        normalized = await runtime._normalize_values(schema, dict(values))  # noqa: SLF001
        errors = [] if proposed.operation == "archive" else await runtime._validate(schema, normalized, geometry)  # noqa: SLF001
        errors.extend(parent_errors)
        if proposed.operation in {"create", "update"}:
            errors.extend(
                await runtime._unique_issues(  # noqa: SLF001
                    schema,
                    normalized,
                    exclude_object_id=proposed.object_id,
                )
            )
        return ChangeSetItemModel(
            change_set_id=change_set_id,
            object_id=proposed.object_id,
            parent_object_id=parent_object_id,
            operation=proposed.operation,
            base_revision=proposed.base_revision,
            proposed_values=normalized,
            proposed_geometry=runtime._geometry_expression(geometry),  # noqa: SLF001
            validation_errors=errors,
            status="valid" if not errors else "invalid",
        )

    async def _apply_item(
        self,
        runtime: RuntimeObjectService,
        schema: EntitySchemaModel,
        item: ChangeSetItemModel,
        actor: ActorContext,
    ) -> None:
        geometry_json = None
        if item.proposed_geometry is not None:
            geometry_json = await self._session.scalar(func.ST_AsGeoJSON(item.proposed_geometry))
        geometry = runtime._parse_geometry(geometry_json)  # noqa: SLF001
        if item.operation == "create":
            object_id = uuid4()
            writable_values = {
                field.code: item.proposed_values[field.code]
                for field in schema.fields
                if field.field_type != "calculated"
                and field.code in item.proposed_values
            }
            runtime._ensure_writable_fields(  # noqa: SLF001
                schema,
                writable_values,
                action="create",
                actor_roles=actor.roles,
            )
            normalized = await runtime._normalize_values(  # noqa: SLF001
                schema,
                dict(item.proposed_values),
            )
            errors = await runtime._validate(schema, normalized, geometry)  # noqa: SLF001
            errors.extend(await runtime._unique_issues(schema, normalized))  # noqa: SLF001
            if errors:
                raise ChangeSetValidationError(
                    "Набор больше не проходит проверку: "
                    + "; ".join(error["message"] or error["code"] for error in errors[:3])
                )
            model = EntityObjectModel(
                id=object_id,
                entity_schema_id=schema.id,
                schema_version_id=await runtime._schema_version_id(schema.id),  # noqa: SLF001
                parent_object_id=await runtime._resolve_parent_object_id(  # noqa: SLF001
                    schema,
                    item.parent_object_id,
                ),
                municipality_id=schema.scope_municipality_id,
                owner_organization_id=schema.owner_organization_id,
                values=normalized,
                geometry=runtime._geometry_expression(geometry),  # noqa: SLF001
                attachment_paths=[],
                status="published" if not errors else "draft",
                data_quality="complete" if not errors else "incomplete",
                validation_errors=errors,
                revision=1,
                created_by=actor.id,
                updated_by=actor.id,
            )
            self._session.add(model)
            await self._session.flush()
            await runtime._sync_search_index(model, schema, model.values)  # noqa: SLF001
            await runtime._sync_unique_values(model, schema, model.values)  # noqa: SLF001
            item.result_object_id = model.id
            item.status = "applied"
            self._session.add(
                ObjectEventModel(
                    entity_schema_id=schema.id,
                    object_id=model.id,
                    revision=1,
                    event_type="object.created",
                    actor_id=actor.id,
                    before_values=None,
                    after_values=dict(model.values),
                    changes=runtime._changes({}, model.values, schema.fields),  # noqa: SLF001
                    metadata_json={"changeSetId": str(item.change_set_id)},
                )
            )
            runtime._add_outbox("entity_object.created.v1", model, schema.code, actor.id)  # noqa: SLF001
            return
        try:
            model, current_geometry = await runtime._get_object(  # noqa: SLF001
                schema.id,
                item.object_id,
                for_update=True,
            )
        except RuntimeObjectNotFound:
            raise ChangeSetConflict
        before = dict(model.values)
        if item.operation == "archive":
            model.status = "archived"
            model.archived_at = datetime.now(UTC)
        elif item.operation == "update":
            if item.parent_object_id != model.parent_object_id:
                model.parent_object_id = await runtime._resolve_parent_object_id(  # noqa: SLF001
                    schema,
                    item.parent_object_id,
                )
            normalized = await runtime._normalize_values(  # noqa: SLF001
                schema,
                dict(item.proposed_values),
            )
            calculated_codes = {
                field.code for field in schema.fields if field.field_type == "calculated"
            }
            changed_values = {
                code: value
                for code, value in normalized.items()
                if before.get(code) != value and code not in calculated_codes
            }
            runtime._ensure_writable_fields(  # noqa: SLF001
                schema,
                changed_values,
                action="update",
                actor_roles=actor.roles,
            )
            errors = await runtime._validate(schema, normalized, geometry)  # noqa: SLF001
            errors.extend(
                await runtime._unique_issues(  # noqa: SLF001
                    schema,
                    normalized,
                    exclude_object_id=model.id,
                )
            )
            if errors:
                raise ChangeSetValidationError(
                    "Набор больше не проходит проверку: "
                    + "; ".join(error["message"] or error["code"] for error in errors[:3])
                )
            if normalized == before and geometry == current_geometry:
                item.result_object_id = model.id
                item.status = "applied"
                return
            model.values = normalized
            model.geometry = runtime._geometry_expression(geometry)  # noqa: SLF001
            model.status = "published"
            model.data_quality = "complete"
            model.validation_errors = []
            model.schema_version_id = await runtime._schema_version_id(schema.id)  # noqa: SLF001
            await self._session.flush()
            await runtime._sync_search_index(model, schema, model.values)  # noqa: SLF001
            await runtime._sync_unique_values(model, schema, model.values)  # noqa: SLF001
        elif item.operation == "confirm":
            fields = {field.code: field for field in schema.fields if not field.archived}
            for code, value in item.proposed_values.items():
                field = fields.get(code)
                if field is None:
                    continue
                value_hash = hashlib.sha256(
                    json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
                ).hexdigest()
                existing = await self._session.scalar(
                    select(FieldConfirmationModel).where(
                        FieldConfirmationModel.object_id == model.id,
                        FieldConfirmationModel.entity_field_id == field.id,
                        FieldConfirmationModel.value_hash == value_hash,
                    )
                )
                if existing is None:
                    self._session.add(
                        FieldConfirmationModel(
                            object_id=model.id,
                            entity_field_id=field.id,
                            value_hash=value_hash,
                            confirmed_by=actor.id,
                            method="change_set",
                        )
                    )
        model.revision += 1
        model.updated_by = actor.id
        item.result_object_id = model.id
        item.status = "applied"
        self._session.add(
            ObjectEventModel(
                entity_schema_id=schema.id,
                object_id=model.id,
                revision=model.revision,
                event_type=f"object.{item.operation}",
                actor_id=actor.id,
                before_values=before,
                after_values=dict(model.values),
                changes=runtime._changes(before, model.values, schema.fields),  # noqa: SLF001
                metadata_json={"changeSetId": str(item.change_set_id)},
            )
        )
        runtime._add_outbox(  # noqa: SLF001
            f"entity_object.{item.operation}.v1", model, schema.code, actor.id
        )

    async def _schema(self, code: str) -> EntitySchemaModel:
        schema = await self._session.scalar(
            select(EntitySchemaModel).where(EntitySchemaModel.code == code, EntitySchemaModel.status == "active")
        )
        if schema is None:
            raise ChangeSetValidationError("Опубликованная сущность не найдена")
        return schema

    async def _version_id(self, entity_id: UUID) -> UUID:
        version_id = await self._session.scalar(
            select(EntitySchemaVersionModel.id)
            .where(EntitySchemaVersionModel.entity_schema_id == entity_id)
            .order_by(EntitySchemaVersionModel.version.desc())
            .limit(1)
        )
        if version_id is None:
            raise ChangeSetValidationError("Версия схемы не найдена")
        return version_id

    @staticmethod
    def _response(model: ChangeSetModel, items: list[ChangeSetItemModel]) -> ChangeSetRead:
        return ChangeSetRead(
            id=model.id,
            entity_id=model.entity_schema_id,
            schema_version_id=model.schema_version_id,
            source=model.source,
            status=model.status,
            idempotency_key=model.idempotency_key,
            created_by=model.created_by,
            decided_by=model.decided_by,
            decided_at=model.decided_at,
            metadata={
                key: value
                for key, value in model.metadata_json.items()
                if key != "__requestHash"
            },
            items=[
                ChangeSetItemRead(
                    id=item.id,
                    object_id=item.object_id,
                    parent_object_id=item.parent_object_id,
                    operation=item.operation,
                    base_revision=item.base_revision,
                    proposed_values=item.proposed_values,
                    validation_errors=[ValidationIssue.model_validate(error) for error in item.validation_errors],
                    status=item.status,
                    result_object_id=item.result_object_id,
                )
                for item in items
            ],
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    def _add_outbox(
        self, model: ChangeSetModel, event_type: str, actor_id: UUID
    ) -> None:
        self._session.add(
            OutboxEventModel(
                aggregate_type="change_set",
                aggregate_id=model.id,
                event_type=event_type,
                payload={
                    "changeSetId": str(model.id),
                    "entityId": str(model.entity_schema_id),
                    "status": model.status,
                    "actorId": str(actor_id),
                },
            )
        )

    def _track(
        self,
        actor: ActorContext,
        model: ChangeSetModel,
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
        self._session.add(
            AuditEventModel(
                resource_type="change_set",
                resource_id=model.id,
                action=f"change_set.{action}",
                actor_id=actor.id,
                actor_full_name=actor.display_name or actor.username or actor.email,
                actor_email=actor.email,
                old_value=old_value,
                new_value=new_value,
                changes=changes,
                metadata_json={
                    "entityId": str(model.entity_schema_id),
                    "source": model.source,
                },
            )
        )

    @staticmethod
    def _request_hash(payload: ChangeSetCreate) -> str:
        canonical = json.dumps(
            payload.model_dump(
                mode="json",
                by_alias=True,
                exclude={"idempotency_key"},
            ),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()
