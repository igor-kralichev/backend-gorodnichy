from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.security import AdminActor, CurrentActor
from app.modules.access.application.service import (
    AccessDenied,
    AccessResourceNotFound,
    AuthorizationService,
)
from app.modules.entities.infrastructure.models import EntitySchemaModel
from app.shared.db.models import (
    AttachmentModel,
    DictionaryModel,
    EntityObjectModel,
    ImportJobModel,
)
from app.modules.audit.api.schemas import AuditEventPage, AuditResourceType
from app.modules.audit.application.service import AuditService

router = APIRouter(prefix="/audit", tags=["История изменений"])


@router.get(
    "/events",
    response_model=AuditEventPage,
    response_model_by_alias=True,
    summary="Получить историю изменений",
    description=(
        "Возвращает единую историю изменений сущностей, объектов, "
        "справочников и пользователей. Доступно только роли Admin."
    ),
)
async def list_audit_events(
    _actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    resource_type: Annotated[
        AuditResourceType | None,
        Query(alias="resourceType", description="Тип ресурса"),
    ] = None,
    resource_id: Annotated[
        UUID | None,
        Query(alias="resourceId", description="UUID ресурса"),
    ] = None,
    actor_id: Annotated[
        UUID | None,
        Query(alias="actorId", description="UUID пользователя Keycloak"),
    ] = None,
    action: Annotated[
        str | None,
        Query(max_length=120, description="Действие"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500, description="Количество событий на странице")] = 50,
    offset: Annotated[int, Query(ge=0, description="Смещение от начала списка")] = 0,
) -> AuditEventPage:
    return await AuditService(session).list_page(
        resource_type=resource_type,
        resource_id=resource_id,
        actor_id=actor_id,
        action=action,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/resources/{resourceType}/{resourceId}",
    response_model=AuditEventPage,
    response_model_by_alias=True,
    summary="Получить доступную историю конкретного ресурса",
    description="Доступ проверяется теми же предметными правилами, что и чтение ресурса.",
)
async def get_resource_history(
    resource_type: Annotated[AuditResourceType, Path(alias="resourceType")],
    resource_id: Annotated[UUID, Path(alias="resourceId")],
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditEventPage:
    entity_code: str | None = None
    object_id: UUID | None = None
    authorization = AuthorizationService(session)
    try:
        if resource_type == "entity_schema":
            entity_code = await session.scalar(
                select(EntitySchemaModel.code).where(EntitySchemaModel.id == resource_id)
            )
        elif resource_type == "entity_object":
            row = (
                await session.execute(
                    select(EntitySchemaModel.code, EntityObjectModel.id)
                    .join(EntityObjectModel, EntityObjectModel.entity_schema_id == EntitySchemaModel.id)
                    .where(EntityObjectModel.id == resource_id)
                )
            ).one_or_none()
            if row:
                entity_code, object_id = row
        elif resource_type == "attachment":
            row = (
                await session.execute(
                    select(EntitySchemaModel.code, AttachmentModel.object_id)
                    .join(AttachmentModel, AttachmentModel.entity_schema_id == EntitySchemaModel.id)
                    .where(AttachmentModel.id == resource_id)
                )
            ).one_or_none()
            if row:
                entity_code, object_id = row
        elif resource_type == "import_job":
            job = await session.get(ImportJobModel, resource_id)
            if job is None or job.created_by != actor.id:
                raise AccessResourceNotFound
        elif resource_type == "dictionary":
            dictionary = await session.get(DictionaryModel, resource_id)
            if dictionary is None:
                raise AccessResourceNotFound
            if dictionary.entity_schema_id:
                entity_code = await session.scalar(
                    select(EntitySchemaModel.code).where(
                        EntitySchemaModel.id == dictionary.entity_schema_id
                    )
                )
            elif dictionary.owner_organization_id:
                await authorization.require(
                    actor,
                    "read",
                    organization_id=dictionary.owner_organization_id,
                )
        elif resource_type == "user":
            if resource_id != actor.id and "Admin" not in actor.roles:
                raise AccessDenied
        else:
            if "Admin" not in actor.roles:
                raise AccessDenied
        if entity_code is None and resource_type in {"entity_schema", "entity_object", "attachment"}:
            raise AccessResourceNotFound
        if entity_code:
            await authorization.require_entity_code(
                actor,
                "read",
                entity_code,
                object_id=object_id,
            )
    except AccessResourceNotFound as error:
        raise HTTPException(status_code=404, detail="Ресурс не найден") from error
    except AccessDenied as error:
        raise HTTPException(status_code=403, detail="Недостаточно прав для просмотра истории") from error

    page = await AuditService(session).list_page(
        resource_type=resource_type,
        resource_id=resource_id,
        actor_id=None,
        action=None,
        limit=limit,
        offset=offset,
    )
    if entity_code and resource_type == "entity_object":
        capabilities = await authorization.capabilities(
            actor,
            entity_code=entity_code,
            object_id=object_id,
        )
        allowed = {
            code for code, actions in capabilities.field_actions.items() if "read" in actions
        }
        return AuditService.redact_object_fields(page, allowed)
    return page
