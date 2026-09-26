from typing import Any, Literal

import httpx

from app.core.config import settings
from app.modules.geocoding.api.schemas import AddressSuggestionPage, AddressSuggestionRead


class GeocodingConfigurationError(Exception):
    """Геокодер не настроен."""


class AddressSuggestionService:
    """Единая точка интеграции с DaData и локальным Nominatim."""

    async def suggest(
        self,
        *,
        query: str,
        source: Literal["dadata", "nominatim"],
        limit: int,
    ) -> AddressSuggestionPage:
        if source == "dadata":
            data = await self._suggest_dadata(query=query, limit=limit)
        else:
            data = await self._suggest_nominatim(query=query, limit=limit)
        return AddressSuggestionPage(query=query, returned=len(data), data=data)

    async def _suggest_dadata(self, *, query: str, limit: int) -> list[AddressSuggestionRead]:
        if not settings.dadata_api_key:
            raise GeocodingConfigurationError("DaData не настроена: отсутствует DADATA_API_KEY")
        headers = {
            "Authorization": f"Token {settings.dadata_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if settings.dadata_secret_key:
            headers["X-Secret"] = settings.dadata_secret_key
        payload = {
            "query": query,
            "count": limit,
            "locations": [{"region": "Нижегородская"}],
            "restrict_value": False,
        }
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                settings.dadata_suggestions_url,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
        suggestions = response.json().get("suggestions", [])
        return [self._dadata_item(item) for item in suggestions]

    async def _suggest_nominatim(self, *, query: str, limit: int) -> list[AddressSuggestionRead]:
        params = {
            "q": query,
            "format": "jsonv2",
            "addressdetails": "1",
            "countrycodes": "ru",
            "limit": str(limit),
            "bounded": "1",
            "viewbox": "41.75,57.45,47.75,54.45",
        }
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(
                f"{settings.nominatim_url.rstrip('/')}/search.php",
                params=params,
                headers={"User-Agent": "municipal-low-code-backend/1.0"},
            )
            response.raise_for_status()
        items: list[dict[str, Any]] = response.json()
        return [
            AddressSuggestionRead(
                source="nominatim",
                value=str(item.get("display_name") or ""),
                unrestricted_value=str(item.get("display_name") or ""),
                latitude=self._float_or_none(item.get("lat")),
                longitude=self._float_or_none(item.get("lon")),
                quality=str(item.get("type") or item.get("class") or "") or None,
                raw=item,
            )
            for item in items
            if item.get("display_name")
        ]

    @staticmethod
    def _dadata_item(item: dict[str, Any]) -> AddressSuggestionRead:
        data = item.get("data") or {}
        return AddressSuggestionRead(
            source="dadata",
            value=str(item.get("value") or ""),
            unrestricted_value=item.get("unrestricted_value"),
            latitude=AddressSuggestionService._float_or_none(data.get("geo_lat")),
            longitude=AddressSuggestionService._float_or_none(data.get("geo_lon")),
            quality=data.get("qc_geo"),
            raw=item,
        )

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
