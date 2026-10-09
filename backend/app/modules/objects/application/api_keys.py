from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import httpx

from app.core.config import settings


class GeneratedApiKeyError(Exception):
    """Keycloak не выполнил операцию с ключом сгенерированного API."""


class GeneratedApiKeyNotFound(GeneratedApiKeyError):
    """Для сущности ещё не создан API-ключ."""


@dataclass(frozen=True, slots=True)
class GeneratedApiKeyValue:
    entity_id: UUID
    entity_code: str
    client_id: str
    api_key: str


class GeneratedApiKeyClient:
    """Хранит и проверяет ключи как secrets confidential clients в Keycloak."""

    async def create(self, entity_id: UUID, entity_code: str) -> GeneratedApiKeyValue:
        async with httpx.AsyncClient(timeout=15) as client:
            headers = await self._admin_headers(client)
            client_id = self._client_id(entity_id)
            internal_id = await self._internal_client_id(client, headers, client_id)
            if internal_id is None:
                response = await client.post(
                    f"{settings.keycloak_admin_realm_url}/clients",
                    headers=headers,
                    json={
                        "clientId": client_id,
                        "name": f"Read-only API: {entity_code}",
                        "description": (
                            "Ключ только для чтения сгенерированного API сущности "
                            f"{entity_code}"
                        ),
                        "enabled": True,
                        "protocol": "openid-connect",
                        "publicClient": False,
                        "serviceAccountsEnabled": True,
                        "standardFlowEnabled": False,
                        "directAccessGrantsEnabled": False,
                        "attributes": {
                            "generatedApi": "true",
                            "entityId": str(entity_id),
                            "entityCode": entity_code,
                        },
                    },
                )
                if response.status_code == 403:
                    raise GeneratedApiKeyError(
                        "Service account Keycloak не имеет разрешения создавать generated clients"
                    )
                if response.status_code not in {201, 409}:
                    raise GeneratedApiKeyError("Keycloak не создал клиент API-ключа")
                internal_id = await self._internal_client_id(client, headers, client_id)
            if internal_id is None:
                raise GeneratedApiKeyError("Keycloak не вернул созданный клиент API-ключа")
            secret = await self._secret(client, headers, internal_id)
        return self._value(entity_id, entity_code, client_id, secret)

    async def get(self, entity_id: UUID, entity_code: str) -> GeneratedApiKeyValue:
        async with httpx.AsyncClient(timeout=15) as client:
            headers = await self._admin_headers(client)
            client_id = self._client_id(entity_id)
            internal_id = await self._internal_client_id(client, headers, client_id)
            if internal_id is None:
                raise GeneratedApiKeyNotFound("API-ключ сущности ещё не создан")
            secret = await self._secret(client, headers, internal_id)
        return self._value(entity_id, entity_code, client_id, secret)

    async def rotate(self, entity_id: UUID, entity_code: str) -> GeneratedApiKeyValue:
        async with httpx.AsyncClient(timeout=15) as client:
            headers = await self._admin_headers(client)
            client_id = self._client_id(entity_id)
            internal_id = await self._internal_client_id(client, headers, client_id)
            if internal_id is None:
                raise GeneratedApiKeyNotFound("API-ключ сущности ещё не создан")
            response = await client.post(
                f"{settings.keycloak_admin_realm_url}/clients/{internal_id}/client-secret",
                headers=headers,
            )
            if response.status_code != 200:
                raise GeneratedApiKeyError("Keycloak не перегенерировал API-ключ")
            secret = self._secret_from_payload(response.json())
        return self._value(entity_id, entity_code, client_id, secret)

    async def delete(self, entity_id: UUID) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            headers = await self._admin_headers(client)
            client_id = self._client_id(entity_id)
            internal_id = await self._internal_client_id(client, headers, client_id)
            if internal_id is None:
                raise GeneratedApiKeyNotFound("API-ключ сущности ещё не создан")
            response = await client.delete(
                f"{settings.keycloak_admin_realm_url}/clients/{internal_id}",
                headers=headers,
            )
            if response.status_code != 204:
                raise GeneratedApiKeyError("Keycloak не удалил API-ключ")

    async def validate(self, entity_id: UUID, api_key: str) -> bool:
        client_id, separator, secret = api_key.strip().partition(".")
        if not separator or not secret or client_id != self._client_id(entity_id):
            return False
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                settings.keycloak_internal_token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": secret,
                },
            )
        if response.status_code in {400, 401}:
            return False
        if response.status_code != 200:
            raise GeneratedApiKeyError("Keycloak временно не может проверить API-ключ")
        return isinstance(response.json().get("access_token"), str)

    async def _admin_headers(self, client: httpx.AsyncClient) -> dict[str, str]:
        response = await client.post(
            settings.keycloak_internal_token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": settings.keycloak_api_client_id,
                "client_secret": settings.keycloak_api_client_secret,
            },
        )
        if response.status_code != 200:
            raise GeneratedApiKeyError("Keycloak не выдал административный токен")
        token = response.json().get("access_token")
        if not isinstance(token, str) or not token:
            raise GeneratedApiKeyError("Ответ Keycloak не содержит access_token")
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    async def _internal_client_id(
        client: httpx.AsyncClient,
        headers: dict[str, str],
        client_id: str,
    ) -> str | None:
        response = await client.get(
            f"{settings.keycloak_admin_realm_url}/clients",
            headers=headers,
            params={"clientId": client_id},
        )
        if response.status_code != 200:
            if response.status_code == 403:
                raise GeneratedApiKeyError(
                    "Service account Keycloak не имеет разрешения просматривать generated clients"
                )
            raise GeneratedApiKeyError("Keycloak не вернул список клиентов")
        payload = response.json()
        if not isinstance(payload, list):
            raise GeneratedApiKeyError("Ответ Keycloak со списком клиентов некорректен")
        for item in payload:
            if isinstance(item, dict) and item.get("clientId") == client_id:
                internal_id = item.get("id")
                return internal_id if isinstance(internal_id, str) else None
        return None

    async def _secret(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        internal_id: str,
    ) -> str:
        response = await client.get(
            f"{settings.keycloak_admin_realm_url}/clients/{internal_id}/client-secret",
            headers=headers,
        )
        if response.status_code != 200:
            raise GeneratedApiKeyError("Keycloak не вернул API-ключ")
        return self._secret_from_payload(response.json())

    @staticmethod
    def _secret_from_payload(payload: object) -> str:
        if not isinstance(payload, dict):
            raise GeneratedApiKeyError("Ответ Keycloak с API-ключом некорректен")
        secret = payload.get("value")
        if not isinstance(secret, str) or not secret:
            raise GeneratedApiKeyError("Keycloak вернул пустой API-ключ")
        return secret

    @staticmethod
    def _client_id(entity_id: UUID) -> str:
        return f"municipal-generated-{entity_id.hex}"

    @staticmethod
    def _value(
        entity_id: UUID,
        entity_code: str,
        client_id: str,
        secret: str,
    ) -> GeneratedApiKeyValue:
        return GeneratedApiKeyValue(
            entity_id=entity_id,
            entity_code=entity_code,
            client_id=client_id,
            api_key=f"{client_id}.{secret}",
        )
