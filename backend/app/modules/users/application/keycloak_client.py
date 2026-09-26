from typing import Any
from uuid import UUID

import httpx

from app.core.config import settings
from app.modules.users.api.schemas import (
    UserCreate,
    UserDeleteRead,
    UserListItemRead,
    UserListRead,
    UserPasswordReset,
    UserPasswordResetRead,
    UserRead,
    UserStatusRead,
)
from app.modules.users.domain.errors import (
    IdentityProviderError,
    UserAlreadyExists,
    UserNotFound,
    UserRoleNotFound,
)


class KeycloakAdminClient:
    """Клиент административного API Keycloak для управления пользователями."""

    async def get_user_snapshot(self, user_id: UUID) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15) as client:
            token = await self._service_token(client)
            headers = {"Authorization": f"Bearer {token}"}
            user = await self._user(client, headers, user_id)
            roles = await self._user_realm_roles(client, headers, user_id)
        return {
            "id": str(user_id),
            "username": _optional_text(user.get("username")),
            "email": _optional_text(user.get("email")),
            "fullName": _full_name(user),
            "firstName": _optional_text(user.get("firstName")),
            "lastName": _optional_text(user.get("lastName")),
            "enabled": bool(user.get("enabled")),
            "roles": roles,
        }

    async def list_users(self, *, search: str | None, limit: int, offset: int) -> UserListRead:
        async with httpx.AsyncClient(timeout=15) as client:
            token = await self._service_token(client)
            headers = {"Authorization": f"Bearer {token}"}
            users = await self._users(client, headers, search=search, limit=limit, offset=offset)
            total = await self._users_count(client, headers, search=search)
            items = []
            for user in users:
                user_id = UUID(str(user["id"]))
                roles = await self._user_realm_roles(client, headers, user_id)
                items.append(
                    UserListItemRead(
                        id=user_id,
                        full_name=_full_name(user),
                        email=_optional_text(user.get("email")),
                        roles=roles,
                    )
                )
        return UserListRead(items=items, total=total, limit=limit, offset=offset)

    async def create_user(self, payload: UserCreate) -> UserRead:
        async with httpx.AsyncClient(timeout=15) as client:
            token = await self._service_token(client)
            headers = {"Authorization": f"Bearer {token}"}
            user_id = await self._create_user(client, headers, payload)
            if payload.temporary_password:
                await self._set_password(client, headers, user_id, payload.temporary_password, temporary=True)
            await self._assign_roles(client, headers, user_id, payload.roles)
            return UserRead(
                id=user_id,
                username=payload.username or str(payload.email),
                email=payload.email,
                enabled=payload.enabled,
                roles=payload.roles,
            )

    async def deactivate_user(self, user_id: UUID) -> UserStatusRead:
        async with httpx.AsyncClient(timeout=15) as client:
            token = await self._service_token(client)
            headers = {"Authorization": f"Bearer {token}"}
            await self._update_user_enabled(client, headers, user_id, enabled=False)
        return UserStatusRead(id=user_id, enabled=False)

    async def activate_user(self, user_id: UUID) -> UserStatusRead:
        async with httpx.AsyncClient(timeout=15) as client:
            token = await self._service_token(client)
            headers = {"Authorization": f"Bearer {token}"}
            await self._update_user_enabled(client, headers, user_id, enabled=True)
        return UserStatusRead(id=user_id, enabled=True)

    async def reset_password(self, user_id: UUID, payload: UserPasswordReset) -> UserPasswordResetRead:
        async with httpx.AsyncClient(timeout=15) as client:
            token = await self._service_token(client)
            headers = {"Authorization": f"Bearer {token}"}
            await self._set_password(
                client,
                headers,
                user_id,
                payload.temporary_password,
                temporary=payload.temporary,
            )
        return UserPasswordResetRead(
            id=user_id,
            password_reset=True,
            temporary=payload.temporary,
        )

    async def delete_user(self, user_id: UUID) -> UserDeleteRead:
        async with httpx.AsyncClient(timeout=15) as client:
            token = await self._service_token(client)
            headers = {"Authorization": f"Bearer {token}"}
            response = await client.delete(f"{settings.keycloak_admin_realm_url}/users/{user_id}", headers=headers)
        if response.status_code == 404:
            raise UserNotFound("Пользователь Keycloak не найден")
        if response.status_code != 204:
            raise IdentityProviderError("Keycloak не удалил пользователя")
        return UserDeleteRead(id=user_id, deleted=True)

    async def _service_token(self, client: httpx.AsyncClient) -> str:
        response = await client.post(
            settings.keycloak_internal_token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": settings.keycloak_api_client_id,
                "client_secret": settings.keycloak_api_client_secret,
            },
        )
        if response.status_code != 200:
            raise IdentityProviderError("Keycloak не выдал service-account токен")
        token = response.json().get("access_token")
        if not isinstance(token, str) or not token:
            raise IdentityProviderError("Ответ Keycloak не содержит access_token")
        return token

    async def _create_user(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        payload: UserCreate,
    ) -> UUID:
        response = await client.post(
            f"{settings.keycloak_admin_realm_url}/users",
            headers=headers,
            json={
                "username": payload.username or str(payload.email),
                "email": str(payload.email),
                "firstName": payload.first_name,
                "lastName": payload.last_name,
                "enabled": payload.enabled,
                "emailVerified": False,
            },
        )
        if response.status_code == 409:
            raise UserAlreadyExists("Пользователь с таким username или email уже существует")
        if response.status_code != 201:
            raise IdentityProviderError("Keycloak не создал пользователя")
        location = response.headers.get("location", "")
        raw_user_id = location.rstrip("/").rsplit("/", maxsplit=1)[-1]
        try:
            return UUID(raw_user_id)
        except ValueError as error:
            raise IdentityProviderError("Keycloak вернул некорректный id пользователя") from error

    async def _set_password(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        user_id: UUID,
        password: str,
        *,
        temporary: bool,
    ) -> None:
        response = await client.put(
            f"{settings.keycloak_admin_realm_url}/users/{user_id}/reset-password",
            headers=headers,
            json={
                "type": "password",
                "value": password,
                "temporary": temporary,
            },
        )
        if response.status_code == 404:
            raise UserNotFound("Пользователь Keycloak не найден")
        if response.status_code != 204:
            raise IdentityProviderError("Keycloak не сбросил пароль пользователя")

    async def _update_user_enabled(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        user_id: UUID,
        *,
        enabled: bool,
    ) -> None:
        response = await client.put(
            f"{settings.keycloak_admin_realm_url}/users/{user_id}",
            headers=headers,
            json={"enabled": enabled},
        )
        if response.status_code == 404:
            raise UserNotFound("Пользователь Keycloak не найден")
        if response.status_code != 204:
            raise IdentityProviderError("Keycloak не изменил статус пользователя")

    async def _users(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        *,
        search: str | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        params: dict[str, str | int] = {"first": offset, "max": limit}
        if search:
            params["search"] = search
        response = await client.get(f"{settings.keycloak_admin_realm_url}/users", headers=headers, params=params)
        if response.status_code != 200:
            raise IdentityProviderError("Keycloak не вернул список пользователей")
        users = response.json()
        if not isinstance(users, list):
            raise IdentityProviderError("Ответ Keycloak со списком пользователей некорректен")
        return [user for user in users if isinstance(user, dict) and isinstance(user.get("id"), str)]

    async def _user(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        user_id: UUID,
    ) -> dict[str, Any]:
        response = await client.get(f"{settings.keycloak_admin_realm_url}/users/{user_id}", headers=headers)
        if response.status_code == 404:
            raise UserNotFound("Пользователь Keycloak не найден")
        if response.status_code != 200:
            raise IdentityProviderError("Keycloak не вернул пользователя")
        user = response.json()
        if not isinstance(user, dict):
            raise IdentityProviderError("Ответ Keycloak с пользователем некорректен")
        return user

    async def _users_count(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        *,
        search: str | None,
    ) -> int:
        params = {"search": search} if search else None
        response = await client.get(f"{settings.keycloak_admin_realm_url}/users/count", headers=headers, params=params)
        if response.status_code != 200:
            raise IdentityProviderError("Keycloak не вернул количество пользователей")
        count = response.json()
        if not isinstance(count, int):
            raise IdentityProviderError("Ответ Keycloak с количеством пользователей некорректен")
        return count

    async def _user_realm_roles(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        user_id: UUID,
    ) -> list[str]:
        response = await client.get(
            f"{settings.keycloak_admin_realm_url}/users/{user_id}/role-mappings/realm",
            headers=headers,
        )
        if response.status_code == 404:
            raise UserNotFound("Пользователь Keycloak не найден")
        if response.status_code != 200:
            raise IdentityProviderError("Keycloak не вернул роли пользователя")
        roles = response.json()
        if not isinstance(roles, list):
            raise IdentityProviderError("Ответ Keycloak со списком ролей пользователя некорректен")
        return sorted(role["name"] for role in roles if isinstance(role, dict) and isinstance(role.get("name"), str))

    async def _assign_roles(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        user_id: UUID,
        role_names: list[str],
    ) -> None:
        available_roles = await self._realm_roles(client, headers)
        selected_roles = []
        for role_name in role_names:
            role = available_roles.get(role_name)
            if role is None:
                raise UserRoleNotFound(f"Realm role «{role_name}» не найдена")
            selected_roles.append(role)
        response = await client.post(
            f"{settings.keycloak_admin_realm_url}/users/{user_id}/role-mappings/realm",
            headers=headers,
            json=selected_roles,
        )
        if response.status_code != 204:
            raise IdentityProviderError("Keycloak не назначил роли пользователю")

    async def _realm_roles(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
    ) -> dict[str, dict[str, Any]]:
        response = await client.get(f"{settings.keycloak_admin_realm_url}/roles", headers=headers)
        if response.status_code != 200:
            raise IdentityProviderError("Keycloak не вернул список realm roles")
        roles = response.json()
        if not isinstance(roles, list):
            raise IdentityProviderError("Ответ Keycloak со списком ролей некорректен")
        result = {}
        for role in roles:
            if isinstance(role, dict) and isinstance(role.get("name"), str):
                result[role["name"]] = role
        return result


def _full_name(user: dict[str, Any]) -> str:
    first_name = _optional_text(user.get("firstName"))
    last_name = _optional_text(user.get("lastName"))
    full_name = " ".join(part for part in [last_name, first_name] if part)
    return full_name or _optional_text(user.get("username")) or _optional_text(user.get("email")) or "Без имени"


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
