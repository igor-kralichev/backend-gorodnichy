from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.security import AdminActor
from app.modules.audit.application.service import AuditService
from app.modules.users.api.schemas import (
    UserCreate,
    UserDeleteRead,
    UserListRead,
    UserPasswordReset,
    UserPasswordResetRead,
    UserRead,
    UserStatusRead,
)
from app.modules.users.application.keycloak_client import KeycloakAdminClient
from app.modules.users.domain.errors import (
    IdentityProviderError,
    UserAlreadyExists,
    UserNotFound,
    UserRoleNotFound,
)

router = APIRouter(prefix="/users", tags=["Пользователи"])

UserId = Annotated[UUID, Path(alias="userId", description="UUID пользователя Keycloak")]


@router.get(
    "",
    response_model=UserListRead,
    response_model_by_alias=True,
    summary="Получить список пользователей",
    description=(
        "Возвращает пользователей Keycloak с UUID, ФИО и realm roles. "
        "Доступно только пользователю с ролью Admin."
    ),
)
async def list_users(
    _actor: AdminActor,
    search: Annotated[
        str | None,
        Query(max_length=255, description="Поиск по логину, email, имени или фамилии"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Количество пользователей на странице")] = 25,
    offset: Annotated[int, Query(ge=0, description="Смещение от начала списка")] = 0,
) -> UserListRead:
    try:
        return await KeycloakAdminClient().list_users(search=search, limit=limit, offset=offset)
    except IdentityProviderError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post(
    "",
    response_model=UserRead,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    summary="Создать пользователя",
    description=(
        "Создаёт пользователя в Keycloak и назначает realm roles. "
        "Доступно только пользователю с ролью Admin."
    ),
)
async def create_user(
    payload: UserCreate,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserRead:
    try:
        response = await KeycloakAdminClient().create_user(payload)
        await AuditService(session).record(
            actor=actor,
            resource_type="user",
            resource_id=response.id,
            resource_code=response.username,
            resource_name=_payload_user_name(payload) or response.username,
            action="user.created",
            old_value=None,
            new_value=response,
        )
        return response
    except UserAlreadyExists as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except UserRoleNotFound as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except IdentityProviderError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


def _payload_user_name(payload: UserCreate) -> str | None:
    full_name = " ".join(
        part for part in [payload.last_name, payload.first_name] if part
    ).strip()
    return full_name or None


@router.post(
    "/{userId}/activate",
    response_model=UserStatusRead,
    response_model_by_alias=True,
    summary="Активировать пользователя",
    description=(
        "Разрешает пользователю вход в Keycloak. "
        "Доступно только пользователю с ролью Admin."
    ),
)
async def activate_user(
    user_id: UserId,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserStatusRead:
    try:
        client = KeycloakAdminClient()
        before = await client.get_user_snapshot(user_id)
        response = await client.activate_user(user_id)
        after = await client.get_user_snapshot(user_id)
        await AuditService(session).record(
            actor=actor,
            resource_type="user",
            resource_id=response.id,
            resource_code=before.get("username"),
            resource_name=before.get("fullName"),
            action="user.activated",
            old_value=before,
            new_value=after,
        )
        return response
    except UserNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except IdentityProviderError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post(
    "/{userId}/deactivate",
    response_model=UserStatusRead,
    response_model_by_alias=True,
    summary="Деактивировать пользователя",
    description=(
        "Запрещает пользователю вход в Keycloak. "
        "Доступно только пользователю с ролью Admin."
    ),
)
async def deactivate_user(
    user_id: UserId,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserStatusRead:
    try:
        client = KeycloakAdminClient()
        before = await client.get_user_snapshot(user_id)
        response = await client.deactivate_user(user_id)
        after = await client.get_user_snapshot(user_id)
        await AuditService(session).record(
            actor=actor,
            resource_type="user",
            resource_id=response.id,
            resource_code=before.get("username"),
            resource_name=before.get("fullName"),
            action="user.deactivated",
            old_value=before,
            new_value=after,
        )
        return response
    except UserNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except IdentityProviderError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post(
    "/{userId}/resetPassword",
    response_model=UserPasswordResetRead,
    response_model_by_alias=True,
    summary="Сбросить пароль пользователя",
    description=(
        "Назначает новый пароль в Keycloak. "
        "По умолчанию пользователь должен сменить его при следующем входе."
    ),
)
async def reset_user_password(
    user_id: UserId,
    payload: UserPasswordReset,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserPasswordResetRead:
    try:
        client = KeycloakAdminClient()
        before = await client.get_user_snapshot(user_id)
        response = await client.reset_password(user_id, payload)
        await AuditService(session).record(
            actor=actor,
            resource_type="user",
            resource_id=response.id,
            resource_code=before.get("username"),
            resource_name=before.get("fullName"),
            action="user.password_reset",
            old_value={"id": str(user_id), "passwordReset": False},
            new_value={
                "id": str(user_id),
                "passwordReset": True,
                "temporary": response.temporary,
            },
            metadata={
                "targetUserEmail": before.get("email"),
                "targetUserFullName": before.get("fullName"),
            },
        )
        return response
    except UserNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except IdentityProviderError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.delete(
    "/{userId}",
    response_model=UserDeleteRead,
    response_model_by_alias=True,
    summary="Удалить пользователя",
    description=(
        "Удаляет пользователя из Keycloak. "
        "Доступно только пользователю с ролью Admin."
    ),
)
async def delete_user(
    user_id: UserId,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserDeleteRead:
    try:
        client = KeycloakAdminClient()
        before = await client.get_user_snapshot(user_id)
        response = await client.delete_user(user_id)
        await AuditService(session).record(
            actor=actor,
            resource_type="user",
            resource_id=response.id,
            resource_code=before.get("username"),
            resource_name=before.get("fullName"),
            action="user.deleted",
            old_value=before,
            new_value=None,
        )
        return response
    except UserNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except IdentityProviderError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
