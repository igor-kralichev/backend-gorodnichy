from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import ActorContext
from app.modules.entities.infrastructure.models import EntitySchemaModel
from app.modules.access.api.schemas import (
    MembershipCreate,
    MembershipRead,
    MembershipUpdate,
    OrganizationCreate,
    OrganizationPage,
    OrganizationRead,
    OrganizationUpdate,
    PermissionAction,
    PermissionGrantCreate,
    PermissionGrantRead,
    SavedViewCreate,
    SavedViewRead,
    SavedViewUpdate,
)
from app.shared.db.models import (
    MembershipModel,
    AuditEventModel,
    OrganizationModel,
    PermissionGrantModel,
    SavedViewModel,
    EntityObjectModel,
    OutboxEventModel,
)


class AccessResourceNotFound(Exception):
    """Запрашиваемая организация, роль или настройка не найдена."""


class AccessDenied(Exception):
    """У пользователя отсутствует требуемое предметное право."""


class AccessManagementService:
    """Управляет организациями, членством и грантами предметного доступа."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_organization(
        self, payload: OrganizationCreate, actor: ActorContext
    ) -> OrganizationRead:
        async with self._session.begin():
            await self._ensure_organization(payload.parent_id)
            model = OrganizationModel(
                code=payload.code,
                name=payload.name,
                parent_id=payload.parent_id,
            )
            self._session.add(model)
            await self._session.flush()
            await self._session.refresh(model)
            self._track(actor, "organization", model.id, "created", None, self._organization(model))
        return self._organization(model)

    async def list_organizations(
        self, *, include_inactive: bool, limit: int, offset: int
    ) -> OrganizationPage:
        conditions = [] if include_inactive else [OrganizationModel.active.is_(True)]
        total = int(
            await self._session.scalar(
                select(func.count()).select_from(OrganizationModel).where(*conditions)
            )
            or 0
        )
        rows = (
            await self._session.scalars(
                select(OrganizationModel)
                .where(*conditions)
                .order_by(OrganizationModel.name)
                .offset(offset)
                .limit(limit)
            )
        ).all()
        return OrganizationPage(
            items=[self._organization(row) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def update_organization(
        self, organization_id: UUID, payload: OrganizationUpdate, actor: ActorContext
    ) -> OrganizationRead:
        async with self._session.begin():
            model = await self._organization_model(organization_id, for_update=True)
            before = self._organization(model)
            if "parent_id" in payload.model_fields_set:
                if payload.parent_id == organization_id:
                    raise ValueError("Организация не может быть родителем самой себя")
                await self._ensure_organization(payload.parent_id)
                model.parent_id = payload.parent_id
            if payload.name is not None:
                model.name = payload.name.strip()
            if payload.active is not None:
                model.active = payload.active
            await self._session.flush()
            await self._session.refresh(model)
            self._track(actor, "organization", model.id, "updated", before, self._organization(model))
        return self._organization(model)

    async def create_membership(
        self, payload: MembershipCreate, actor: ActorContext
    ) -> MembershipRead:
        async with self._session.begin():
            await self._ensure_organization(payload.organization_id)
            model = MembershipModel(
                user_id=payload.user_id,
                organization_id=payload.organization_id,
                role_code=payload.role_code.strip(),
            )
            self._session.add(model)
            await self._session.flush()
            await self._session.refresh(model)
            self._track(actor, "membership", model.id, "created", None, self._membership(model))
        return self._membership(model)

    async def update_membership(
        self, membership_id: UUID, payload: MembershipUpdate, actor: ActorContext
    ) -> MembershipRead:
        async with self._session.begin():
            model = await self._membership_model(membership_id, for_update=True)
            before = self._membership(model)
            model.active = payload.active
            await self._session.flush()
            await self._session.refresh(model)
            self._track(actor, "membership", model.id, "updated", before, self._membership(model))
        return self._membership(model)

    async def delete_membership(self, membership_id: UUID, actor: ActorContext) -> None:
        async with self._session.begin():
            model = await self._membership_model(membership_id, for_update=True)
            before = self._membership(model)
            self._track(actor, "membership", model.id, "deleted", before, None)
            await self._session.delete(model)

    async def create_grant(
        self, payload: PermissionGrantCreate, actor: ActorContext
    ) -> PermissionGrantRead:
        async with self._session.begin():
            await self._ensure_organization(payload.organization_id)
            model = PermissionGrantModel(
                **payload.model_dump(),
                created_by=actor.id,
            )
            self._session.add(model)
            await self._session.flush()
            self._track(actor, "permission_grant", model.id, "created", None, self._grant(model))
        return self._grant(model)

    async def list_grants(self) -> list[PermissionGrantRead]:
        rows = (
            await self._session.scalars(
                select(PermissionGrantModel).order_by(PermissionGrantModel.created_at.desc())
            )
        ).all()
        return [self._grant(row) for row in rows]

    async def delete_grant(self, grant_id: UUID, actor: ActorContext) -> None:
        async with self._session.begin():
            model = await self._session.get(PermissionGrantModel, grant_id, with_for_update=True)
            if model is None:
                raise AccessResourceNotFound
            before = self._grant(model)
            self._track(actor, "permission_grant", model.id, "deleted", before, None)
            await self._session.delete(model)

    async def _ensure_organization(self, organization_id: UUID | None) -> None:
        if organization_id is None:
            return
        exists = await self._session.scalar(
            select(OrganizationModel.id).where(OrganizationModel.id == organization_id)
        )
        if exists is None:
            raise AccessResourceNotFound

    async def _organization_model(
        self, organization_id: UUID, *, for_update: bool = False
    ) -> OrganizationModel:
        statement = select(OrganizationModel).where(OrganizationModel.id == organization_id)
        if for_update:
            statement = statement.with_for_update()
        model = await self._session.scalar(statement)
        if model is None:
            raise AccessResourceNotFound
        return model

    async def _membership_model(
        self, membership_id: UUID, *, for_update: bool = False
    ) -> MembershipModel:
        statement = select(MembershipModel).where(MembershipModel.id == membership_id)
        if for_update:
            statement = statement.with_for_update()
        model = await self._session.scalar(statement)
        if model is None:
            raise AccessResourceNotFound
        return model

    @staticmethod
    def _organization(model: OrganizationModel) -> OrganizationRead:
        return OrganizationRead.model_validate(model, from_attributes=True)

    @staticmethod
    def _membership(model: MembershipModel) -> MembershipRead:
        return MembershipRead.model_validate(model, from_attributes=True)

    @staticmethod
    def _grant(model: PermissionGrantModel) -> PermissionGrantRead:
        return PermissionGrantRead.model_validate(model, from_attributes=True)

    def _track(
        self,
        actor: ActorContext,
        resource_type: str,
        resource_id: UUID,
        action: str,
        old_value,
        new_value,
    ) -> None:
        old_payload = old_value.model_dump(mode="json", by_alias=True) if old_value else None
        new_payload = new_value.model_dump(mode="json", by_alias=True) if new_value else None
        changes = []
        for key in sorted(set(old_payload or {}) | set(new_payload or {})):
            old = (old_payload or {}).get(key)
            new = (new_payload or {}).get(key)
            if old != new:
                changes.append({"path": key, "oldValue": old, "newValue": new})
        self._session.add(
            AuditEventModel(
                resource_type=resource_type,
                resource_id=resource_id,
                action=action,
                actor_id=actor.id,
                actor_full_name=actor.display_name or actor.username or actor.email,
                actor_email=actor.email,
                old_value=old_payload,
                new_value=new_payload,
                changes=changes,
                metadata_json={},
            )
        )
        self._session.add(
            OutboxEventModel(
                aggregate_type=resource_type,
                aggregate_id=resource_id,
                event_type=f"{resource_type}.{action}.v1",
                payload={"resourceId": str(resource_id), "actorId": str(actor.id)},
            )
        )


class SavedViewService:
    """Хранит персональные и общие представления реестров."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, payload: SavedViewCreate, owner_id: UUID) -> SavedViewRead:
        async with self._session.begin():
            model = SavedViewModel(owner_id=owner_id, **payload.model_dump())
            self._session.add(model)
            await self._session.flush()
            await self._session.refresh(model)
        return SavedViewRead.model_validate(model, from_attributes=True)

    async def list(self, entity_id: UUID, actor_id: UUID) -> list[SavedViewRead]:
        rows = (
            await self._session.scalars(
                select(SavedViewModel)
                .where(
                    SavedViewModel.entity_schema_id == entity_id,
                    or_(SavedViewModel.owner_id == actor_id, SavedViewModel.shared.is_(True)),
                )
                .order_by(SavedViewModel.name)
            )
        ).all()
        return [SavedViewRead.model_validate(row, from_attributes=True) for row in rows]

    async def update(
        self, view_id: UUID, payload: SavedViewUpdate, actor_id: UUID
    ) -> SavedViewRead:
        async with self._session.begin():
            model = await self._owned_view(view_id, actor_id, for_update=True)
            for field in payload.model_fields_set:
                value = getattr(payload, field)
                if field == "name" and value is not None:
                    value = value.strip()
                setattr(model, field, value)
            await self._session.flush()
            await self._session.refresh(model)
        return SavedViewRead.model_validate(model, from_attributes=True)

    async def delete(self, view_id: UUID, actor_id: UUID) -> None:
        async with self._session.begin():
            model = await self._owned_view(view_id, actor_id, for_update=True)
            await self._session.delete(model)

    async def _owned_view(
        self, view_id: UUID, actor_id: UUID, *, for_update: bool
    ) -> SavedViewModel:
        statement = select(SavedViewModel).where(
            SavedViewModel.id == view_id,
            SavedViewModel.owner_id == actor_id,
        )
        if for_update:
            statement = statement.with_for_update()
        model = await self._session.scalar(statement)
        if model is None:
            raise AccessResourceNotFound
        return model


class AuthorizationService:
    """Проверяет гранты с учётом realm-ролей и членства в организациях."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def require(
        self,
        actor: ActorContext,
        action: PermissionAction,
        *,
        organization_id: UUID | None = None,
        entity_schema_id: UUID | None = None,
        entity_field_id: UUID | None = None,
        entity_object_id: UUID | None = None,
    ) -> None:
        memberships = (
            await self._session.scalars(
                select(MembershipModel).where(
                    MembershipModel.user_id == actor.id,
                    MembershipModel.active.is_(True),
                )
            )
        ).all()
        organization_ids = {item.organization_id for item in memberships}
        subject_roles = set(actor.roles) | {item.role_code for item in memberships}
        grants = (
            await self._session.scalars(
                select(PermissionGrantModel).where(
                    or_(
                        PermissionGrantModel.user_id == actor.id,
                        PermissionGrantModel.role_code.in_(subject_roles or {""}),
                    )
                )
            )
        ).all()
        for grant in grants:
            if action not in grant.actions:
                continue
            if grant.organization_id is not None and grant.organization_id not in organization_ids:
                continue
            if organization_id is None and grant.organization_id is not None:
                continue
            if organization_id is not None and grant.organization_id not in (None, organization_id):
                continue
            if entity_schema_id is None and grant.entity_schema_id is not None:
                continue
            if entity_schema_id is not None and grant.entity_schema_id not in (None, entity_schema_id):
                continue
            if entity_field_id is None and grant.entity_field_id is not None:
                continue
            if entity_field_id is not None and grant.entity_field_id not in (None, entity_field_id):
                continue
            if entity_object_id is None and grant.entity_object_id is not None:
                continue
            if entity_object_id is not None and grant.entity_object_id not in (None, entity_object_id):
                continue
            return
        raise AccessDenied

    async def require_entity_code(
        self,
        actor: ActorContext,
        action: PermissionAction,
        entity_code: str,
        *,
        object_id: UUID | None = None,
    ) -> None:
        """Проверить предметное право по стабильному коду сущности."""

        schema = await self._session.scalar(
            select(EntitySchemaModel).where(EntitySchemaModel.code == entity_code)
        )
        if schema is None:
            raise AccessResourceNotFound
        organization_id = schema.owner_organization_id
        if object_id is not None:
            record = await self._session.scalar(
                select(EntityObjectModel).where(
                    EntityObjectModel.id == object_id,
                    EntityObjectModel.entity_schema_id == schema.id,
                )
            )
            if record is None:
                raise AccessResourceNotFound
            if record.responsible_id == actor.id and action in {
                "read",
                "update",
                "archive",
                "confirm",
            }:
                return
            organization_id = record.owner_organization_id or organization_id
        await self.require(
            actor,
            action,
            organization_id=organization_id,
            entity_schema_id=schema.id,
            entity_object_id=object_id,
        )
