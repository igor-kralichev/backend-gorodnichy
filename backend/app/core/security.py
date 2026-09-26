from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2AuthorizationCodeBearer
from jwt import PyJWKClient
from jwt.exceptions import InvalidTokenError, PyJWKClientError

from app.core.config import settings


@dataclass(frozen=True, slots=True)
class ActorContext:
    """Проверенный контекст пользователя для прикладного сценария."""

    id: UUID
    role: str
    roles: frozenset[str]
    username: str | None
    display_name: str | None
    email: str | None
    token: str | None = None


oauth2_scheme = OAuth2AuthorizationCodeBearer(
    authorizationUrl=settings.keycloak_authorization_url,
    tokenUrl=settings.keycloak_token_url,
    scopes={
        "openid": "Идентификатор пользователя",
        "profile": "Профиль пользователя",
        "email": "Электронная почта пользователя",
    },
    scheme_name="Keycloak",
    auto_error=False,
)

_jwks_client = PyJWKClient(settings.keycloak_jwks_url)


async def get_current_actor(
    token: Annotated[str | None, Depends(oauth2_scheme)] = None,
) -> ActorContext:
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Не передан JWT токен Keycloak",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _keycloak_actor(token)


def actor_from_token(token: str) -> ActorContext:
    """Проверить JWT и вернуть пользователя для WebSocket-подключений."""

    return _keycloak_actor(token)


async def require_admin(
    actor: Annotated[ActorContext, Depends(get_current_actor)],
) -> ActorContext:
    if settings.admin_role_name not in actor.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Для выполнения операции требуется роль Admin",
        )
    return actor


def _keycloak_actor(token: str) -> ActorContext:
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=settings.keycloak_issuer,
            options={"verify_aud": False},
        )
    except (InvalidTokenError, PyJWKClientError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="JWT токен Keycloak недействителен",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error

    subject = _required_uuid_claim(payload, "sub")
    roles = _realm_roles(payload)
    return ActorContext(
        id=subject,
        role=settings.admin_role_name if settings.admin_role_name in roles else "",
        roles=roles,
        username=_optional_string_claim(payload, "preferred_username"),
        display_name=_optional_string_claim(payload, "name"),
        email=_optional_string_claim(payload, "email"),
        token=token,
    )


def _realm_roles(payload: dict[str, Any]) -> frozenset[str]:
    realm_access = payload.get("realm_access")
    if not isinstance(realm_access, dict):
        return frozenset()
    roles = realm_access.get("roles")
    if not isinstance(roles, list):
        return frozenset()
    return frozenset(role for role in roles if isinstance(role, str))


def _required_uuid_claim(payload: dict[str, Any], key: str) -> UUID:
    value = payload.get(key)
    if not isinstance(value, str):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"В JWT отсутствует обязательное поле {key}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return UUID(value)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Поле JWT {key} не является UUID",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error


def _optional_string_claim(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


CurrentActor = Annotated[ActorContext, Depends(get_current_actor)]
AdminActor = Annotated[ActorContext, Depends(require_admin)]
