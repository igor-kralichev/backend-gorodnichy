from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.security import AdminActor
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
