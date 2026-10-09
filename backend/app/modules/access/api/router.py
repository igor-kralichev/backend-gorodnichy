from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.security import AdminActor, CurrentActor
from app.modules.access.api.schemas import (
    DeleteRead,
    EffectiveCapabilitiesRead,
    MembershipCreate,
    MembershipRead,
    MembershipUpdate,
    OrganizationCreate,
    OrganizationPage,
    OrganizationRead,
    OrganizationUpdate,
    PermissionGrantCreate,
    PermissionGrantRead,
    SavedViewCreate,
    SavedViewRead,
    SavedViewUpdate,
)
from app.modules.access.application.service import (
    AccessManagementService,
    AccessResourceNotFound,
    SavedViewService,
    AuthorizationService,
)

router = APIRouter(tags=["Организации и права"])


@router.get(
    "/me/capabilities",
    response_model=EffectiveCapabilitiesRead,
    response_model_by_alias=True,
    summary="Получить эффективные возможности текущего пользователя",
)
async def get_current_capabilities(
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    entity_code: Annotated[str | None, Query(alias="entityCode", max_length=120)] = None,
    object_id: Annotated[UUID | None, Query(alias="objectId")] = None,
) -> EffectiveCapabilitiesRead:
    try:
        return await AuthorizationService(session).capabilities(
            actor,
            entity_code=entity_code,
            object_id=object_id,
        )
    except AccessResourceNotFound as error:
        raise HTTPException(status_code=404, detail="Ресурс не найден") from error


def _handle_access_error(error: Exception) -> None:
    if isinstance(error, AccessResourceNotFound):
        raise HTTPException(status_code=404, detail="Ресурс не найден") from error
    if isinstance(error, IntegrityError):
        raise HTTPException(status_code=409, detail="Нарушена уникальность или связь данных") from error
    raise error


@router.post("/organizations", response_model=OrganizationRead, status_code=201, summary="Создать организацию")
async def create_organization(
    payload: OrganizationCreate,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OrganizationRead:
    try:
        return await AccessManagementService(session).create_organization(payload, actor)
    except Exception as error:
        _handle_access_error(error)
        raise


@router.get("/organizations", response_model=OrganizationPage, summary="Получить организации")
async def list_organizations(
    _actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    include_inactive: Annotated[bool, Query(alias="includeInactive")] = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OrganizationPage:
    return await AccessManagementService(session).list_organizations(
        include_inactive=include_inactive, limit=limit, offset=offset
    )


@router.patch("/organizations/{organizationId}", response_model=OrganizationRead, summary="Изменить организацию")
async def update_organization(
    organization_id: Annotated[UUID, Path(alias="organizationId")],
    payload: OrganizationUpdate,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OrganizationRead:
    try:
        return await AccessManagementService(session).update_organization(organization_id, payload, actor)
    except Exception as error:
        _handle_access_error(error)
        raise


@router.post("/memberships", response_model=MembershipRead, status_code=201, summary="Добавить пользователя в организацию")
async def create_membership(
    payload: MembershipCreate,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MembershipRead:
    try:
        return await AccessManagementService(session).create_membership(payload, actor)
    except Exception as error:
        _handle_access_error(error)
        raise


@router.patch("/memberships/{membershipId}", response_model=MembershipRead, summary="Изменить активность членства")
async def update_membership(
    membership_id: Annotated[UUID, Path(alias="membershipId")],
    payload: MembershipUpdate,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MembershipRead:
    try:
        return await AccessManagementService(session).update_membership(membership_id, payload, actor)
    except Exception as error:
        _handle_access_error(error)
        raise


@router.delete("/memberships/{membershipId}", response_model=DeleteRead, summary="Удалить членство")
async def delete_membership(
    membership_id: Annotated[UUID, Path(alias="membershipId")],
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DeleteRead:
    try:
        await AccessManagementService(session).delete_membership(membership_id, actor)
        return DeleteRead(id=membership_id)
    except Exception as error:
        _handle_access_error(error)
        raise


@router.post("/permission-grants", response_model=PermissionGrantRead, status_code=201, summary="Выдать предметное право")
async def create_permission_grant(
    payload: PermissionGrantCreate,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PermissionGrantRead:
    try:
        return await AccessManagementService(session).create_grant(payload, actor)
    except Exception as error:
        _handle_access_error(error)
        raise


@router.get("/permission-grants", response_model=list[PermissionGrantRead], summary="Получить предметные права")
async def list_permission_grants(
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[PermissionGrantRead]:
    return await AccessManagementService(session).list_grants()


@router.delete("/permission-grants/{grantId}", response_model=DeleteRead, summary="Отозвать предметное право")
async def delete_permission_grant(
    grant_id: Annotated[UUID, Path(alias="grantId")],
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DeleteRead:
    try:
        await AccessManagementService(session).delete_grant(grant_id, actor)
        return DeleteRead(id=grant_id)
    except Exception as error:
        _handle_access_error(error)
        raise


@router.post("/saved-views", response_model=SavedViewRead, status_code=201, summary="Сохранить представление реестра")
async def create_saved_view(
    payload: SavedViewCreate,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SavedViewRead:
    return await SavedViewService(session).create(payload, actor.id)


@router.get("/saved-views", response_model=list[SavedViewRead], summary="Получить представления реестра")
async def list_saved_views(
    entity_id: Annotated[UUID, Query(alias="entityId")],
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[SavedViewRead]:
    return await SavedViewService(session).list(entity_id, actor.id)


@router.patch("/saved-views/{viewId}", response_model=SavedViewRead, summary="Изменить представление реестра")
async def update_saved_view(
    view_id: Annotated[UUID, Path(alias="viewId")],
    payload: SavedViewUpdate,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SavedViewRead:
    try:
        return await SavedViewService(session).update(view_id, payload, actor.id)
    except AccessResourceNotFound as error:
        raise HTTPException(status_code=404, detail="Представление не найдено") from error


@router.delete("/saved-views/{viewId}", response_model=DeleteRead, summary="Удалить представление реестра")
async def delete_saved_view(
    view_id: Annotated[UUID, Path(alias="viewId")],
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DeleteRead:
    try:
        await SavedViewService(session).delete(view_id, actor.id)
        return DeleteRead(id=view_id)
    except AccessResourceNotFound as error:
        raise HTTPException(status_code=404, detail="Представление не найдено") from error
