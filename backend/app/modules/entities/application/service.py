import json
import logging
from collections.abc import Iterable
from uuid import UUID, uuid4

from redis.asyncio import Redis
from slugify import slugify
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.entities.api.schemas import (
    EntityCreate,
    EntityFieldCreate,
    EntityFieldRead,
    EntityRead,
    MapColorRuleRead,
    MapSettingsRead,
    MapStyle,
    MapStylesRead,
)
from app.modules.entities.domain.enums import FieldType, GeometryType, MapGeometryType, MapRuleOperator
from app.modules.entities.domain.errors import EntityCodeAlreadyExists, EntityFieldReferenceError
from app.modules.entities.infrastructure.models import (
    EntityAllowedGeometryTypeModel,
    EntityFieldModel,
    EntityMapColorRuleModel,
    EntityMapStyleModel,
    EntitySchemaModel,
    EntitySchemaVersionModel,
)
from app.modules.entities.infrastructure.repository import SqlAlchemyEntitySchemaRepository
from app.shared.db.models import DictionaryModel, MunicipalityModel, OutboxEventModel

logger = logging.getLogger(__name__)


DEFAULT_STYLES: dict[MapGeometryType, MapStyle] = {
    MapGeometryType.POINT: MapStyle(fill="#16a34a", stroke="#166534", stroke_width=2, point_size=10, opacity=0.85),
    MapGeometryType.LINE_STRING: MapStyle(
        fill="#3b82f6", stroke="#1d4ed8", stroke_width=3, point_size=8, opacity=0.9
    ),
    MapGeometryType.POLYGON: MapStyle(fill="#60a5fa", stroke="#2563eb", stroke_width=2, point_size=8, opacity=0.35),
}


class CreateEntitySchemaService:
    def __init__(self, session: AsyncSession, redis: Redis) -> None:
        self._session = session
        self._redis = redis
        self._repository = SqlAlchemyEntitySchemaRepository(session)

    async def execute(self, command: EntityCreate, actor_id: UUID | None) -> EntityRead:
        code = command.code or slugify(command.name, separator="_", lowercase=True, max_length=120) or "entity"
        try:
            async with self._session.begin():
                code = await self.resolve_entity_code(code, explicit=command.code is not None)
                await self.validate_references(command)
                entity = self.build_entity(command, code)
                self._repository.add(entity)
                await self._session.flush()

                snapshot = self.snapshot(entity)
                self._session.add(
                    EntitySchemaVersionModel(
                        entity_schema_id=entity.id,
                        version=1,
                        snapshot=snapshot,
                        created_by=actor_id,
                    )
                )
                self._session.add(
                    OutboxEventModel(
                        aggregate_type="entity_schema",
                        aggregate_id=entity.id,
                        event_type="entity_schema.created.v1",
                        payload={
                            "entityId": str(entity.id),
                            "code": entity.code,
                            "version": 1,
                            "actorId": str(actor_id) if actor_id else None,
                        },
                    )
                )
        except IntegrityError as error:
            if getattr(error.orig, "sqlstate", None) == "23505":
                raise EntityCodeAlreadyExists(code) from error
            raise

        response = self.to_response(entity)
        await self.cache_schema(response)
        return response

    async def resolve_entity_code(self, base: str, *, explicit: bool) -> str:
        if not await self._repository.code_exists(base):
            return base
        if explicit:
            raise EntityCodeAlreadyExists(base)
        suffix = 2
        while await self._repository.code_exists(f"{base}_{suffix}"):
            suffix += 1
        return f"{base}_{suffix}"

    async def validate_references(self, command: EntityCreate) -> None:
        if command.scope_municipality_id is not None:
            municipality_id = await self._session.scalar(
                select(MunicipalityModel.id).where(
                    MunicipalityModel.id == command.scope_municipality_id
                )
            )
            if municipality_id is None:
                raise EntityFieldReferenceError(
                    "Муниципалитет из scopeMunicipalityId не найден"
                )

        if command.parent_entity_id is not None:
            parent_entity_id = await self._session.scalar(
                select(EntitySchemaModel.id).where(
                    EntitySchemaModel.id == command.parent_entity_id
                )
            )
            if parent_entity_id is None:
                raise EntityFieldReferenceError(
                    "Родительская сущность из parentEntityId не найдена"
                )

        dictionary_ids = {
            field.enum_id for field in command.fields if field.enum_id is not None
        }
        if dictionary_ids:
            found_dictionary_ids = set(
                await self._session.scalars(
                    select(DictionaryModel.id).where(
                        DictionaryModel.id.in_(dictionary_ids)
                    )
                )
            )
            missing_dictionary_ids = dictionary_ids - found_dictionary_ids
            if missing_dictionary_ids:
                missing = ", ".join(
                    str(item) for item in sorted(missing_dictionary_ids, key=str)
                )
                raise EntityFieldReferenceError(
                    f"Справочники из enumId не найдены: {missing}"
                )

        entity_ids = {
            field.reference_entity_id
            for field in command.fields
            if field.reference_entity_id is not None
        }
        if entity_ids:
            found_entity_ids = set(
                await self._session.scalars(
                    select(EntitySchemaModel.id).where(
                        EntitySchemaModel.id.in_(entity_ids)
                    )
                )
            )
            missing_entity_ids = entity_ids - found_entity_ids
            if missing_entity_ids:
                missing = ", ".join(
                    str(item) for item in sorted(missing_entity_ids, key=str)
                )
                raise EntityFieldReferenceError(
                    f"Связанные сущности из referenceEntityId не найдены: {missing}"
                )

    def build_entity(self, command: EntityCreate, code: str) -> EntitySchemaModel:
        enabled_geometry_types = self._enabled_geometry_types(command)
        primary_geometry_type = command.geometry_type
        if primary_geometry_type == GeometryType.NONE and enabled_geometry_types:
            primary_geometry_type = GeometryType(enabled_geometry_types[0].value)
        elif primary_geometry_type != GeometryType.NONE:
            primary_map_geometry_type = MapGeometryType(primary_geometry_type.value)
            if primary_map_geometry_type not in enabled_geometry_types:
                enabled_geometry_types.insert(0, primary_map_geometry_type)

        entity = EntitySchemaModel(
            code=code,
            name=command.name.strip(),
            description=command.description.strip() if command.description else None,
            parent_entity_schema_id=command.parent_entity_id,
            geometry_type=primary_geometry_type.value,
            clustering_enabled=command.map_settings.clustering_enabled if command.map_settings else False,
            status="draft",
            current_version=1,
            scope_municipality_id=command.scope_municipality_id,
            layer_selectable=(
                command.map_settings.selectable if command.map_settings else True
            ),
            layer_visible_by_default=(
                command.map_settings.visible_by_default
                if command.map_settings
                else True
            ),
        )

        field_commands = list(command.fields)
        if command.include_address and not any(field.type == FieldType.ADDRESS for field in field_commands):
            field_commands.insert(
                0,
                EntityFieldCreate(
                    code="address",
                    name="Адрес",
                    type=FieldType.ADDRESS,
                    required=False,
                    list_visible=True,
                    card_visible=True,
                    searchable=True,
                    filterable=True,
                ),
            )
        entity.fields = self.build_fields(field_commands)
        entity.geometry_types = [
            EntityAllowedGeometryTypeModel(geometry_type=item.value, sort_order=index)
            for index, item in enumerate(enabled_geometry_types, start=1)
        ]

        requested_styles = command.map_settings.styles.as_dict() if command.map_settings else {}
        entity.map_styles = [
            EntityMapStyleModel(
                geometry_type=geometry_type.value,
                fill=style.fill,
                stroke=style.stroke,
                stroke_width=style.stroke_width,
                point_size=style.point_size,
                opacity=style.opacity,
            )
            for geometry_type in (MapGeometryType.POINT, MapGeometryType.LINE_STRING, MapGeometryType.POLYGON)
            for style in [requested_styles.get(geometry_type, DEFAULT_STYLES[geometry_type])]
        ]

        fields_by_code = {field.code: field for field in entity.fields}
        entity.color_rules = []
        for index, rule in enumerate(command.map_settings.color_rules if command.map_settings else [], start=1):
            field = fields_by_code.get(rule.field_code)
            if field is None:
                raise EntityFieldReferenceError(
                    "Правило цвета карты ссылается на неизвестное поле "
                    f"«{rule.field_code}»"
                )
            entity.color_rules.append(
                EntityMapColorRuleModel(
                    entity_field_id=field.id,
                    name=rule.name,
                    operator=rule.operator.value,
                    value=rule.value,
                    color=rule.color,
                    sort_order=index,
                )
            )
        return entity

    def build_fields(self, commands: Iterable[EntityFieldCreate]) -> list[EntityFieldModel]:
        result: list[EntityFieldModel] = []
        used_codes: set[str] = set()
        for index, field in enumerate(commands, start=1):
            base_code = field.code or slugify(field.name, separator="_", lowercase=True, max_length=120)
            base_code = base_code or f"field_{index}"
            code = self._unique_code(base_code, used_codes)
            used_codes.add(code)
            result.append(
                EntityFieldModel(
                    id=uuid4(),
                    code=code,
                    name=field.name.strip(),
                    field_type=field.type.value,
                    required=field.required,
                    list_visible=field.list_visible,
                    card_visible=field.card_visible,
                    searchable=field.searchable,
                    filterable=field.filterable,
                    sort_order=index,
                    dictionary_id=field.enum_id,
                    reference_entity_schema_id=field.reference_entity_id,
                )
            )
        return result

    @staticmethod
    def _unique_code(base: str, used: set[str]) -> str:
        if base not in used:
            return base
        suffix = 2
        while f"{base}_{suffix}" in used:
            suffix += 1
        return f"{base}_{suffix}"

    @staticmethod
    def _enabled_geometry_types(command: EntityCreate) -> list[MapGeometryType]:
        if command.map_settings:
            return list(command.map_settings.enabled_geometry_types)
        return [] if command.geometry_type == GeometryType.NONE else [MapGeometryType(command.geometry_type.value)]

    def snapshot(self, entity: EntitySchemaModel) -> dict[str, object]:
        return self.to_response(entity).model_dump(mode="json", by_alias=True)

    def to_response(self, entity: EntitySchemaModel) -> EntityRead:
        field_by_id = {field.id: field for field in entity.fields}
        styles = {
            MapGeometryType(style.geometry_type): MapStyle(
                fill=style.fill,
                stroke=style.stroke,
                stroke_width=float(style.stroke_width),
                point_size=float(style.point_size),
                opacity=float(style.opacity),
            )
            for style in entity.map_styles
        }
        return EntityRead(
            id=entity.id,
            code=entity.code,
            name=entity.name,
            description=entity.description,
            geometry_type=GeometryType(entity.geometry_type),
            parent_entity_id=entity.parent_entity_schema_id,
            map_settings=MapSettingsRead(
                enabled_geometry_types=[
                    MapGeometryType(item.geometry_type)
                    for item in sorted(
                        entity.geometry_types,
                        key=lambda item: item.sort_order,
                    )
                ],
                clustering_enabled=entity.clustering_enabled,
                styles=MapStylesRead(
                    point=styles[MapGeometryType.POINT],
                    line_string=styles[MapGeometryType.LINE_STRING],
                    polygon=styles[MapGeometryType.POLYGON],
                ),
                color_rules=[
                    MapColorRuleRead(
                        id=rule.id,
                        name=rule.name,
                        field_code=field_by_id[rule.entity_field_id].code,
                        operator=MapRuleOperator(rule.operator),
                        value=rule.value,
                        color=rule.color,
                    )
                    for rule in sorted(entity.color_rules, key=lambda item: item.sort_order)
                ],
                selectable=entity.layer_selectable,
                visible_by_default=entity.layer_visible_by_default,
            ),
            fields=[
                EntityFieldRead(
                    id=field.id,
                    code=field.code,
                    name=field.name,
                    type=FieldType(field.field_type),
                    required=field.required,
                    list_visible=field.list_visible,
                    card_visible=field.card_visible,
                    searchable=field.searchable,
                    filterable=field.filterable,
                    order=field.sort_order,
                    enum_id=field.dictionary_id,
                    reference_entity_id=field.reference_entity_schema_id,
                )
                for field in sorted(entity.fields, key=lambda item: item.sort_order)
            ],
            status=entity.status,
            version=entity.current_version,
            scope_municipality_id=entity.scope_municipality_id,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )

    async def cache_schema(self, entity: EntityRead) -> None:
        try:
            await self._redis.set(
                f"entity-schema:{entity.id}:v{entity.version}",
                json.dumps(entity.model_dump(mode="json", by_alias=True), ensure_ascii=False),
                ex=settings.redis_schema_ttl_seconds,
            )
        except Exception:
            logger.exception(
                "Схема сущности создана, но обновить кэш Redis не удалось"
            )
