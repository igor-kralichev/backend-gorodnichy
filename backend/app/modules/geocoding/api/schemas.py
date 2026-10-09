from typing import Any, Literal

from app.modules.entities.api.schemas import ApiModel
from app.modules.objects.api.schemas import GeoJsonGeometry


class AddressSuggestionRead(ApiModel):
    """Подсказка адреса из внешнего геокодера."""

    source: Literal["dadata", "nominatim"]
    value: str
    unrestricted_value: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    quality: str | None = None
    provider_id: str | None = None
    geometry: GeoJsonGeometry | None = None
    attribution: str | None = None
    raw: dict[str, Any] | None = None


class AddressSuggestionPage(ApiModel):
    """Список подсказок адресов."""

    query: str
    returned: int
    data: list[AddressSuggestionRead]


class BuildingGeometryRead(ApiModel):
    """Контур здания из картографического провайдера."""

    provider_id: str
    address: str
    latitude: float
    longitude: float
    geometry: GeoJsonGeometry
    provider: Literal["nominatim"] = "nominatim"
    attribution: str = "© OpenStreetMap contributors"


class BuildingGeometryPage(ApiModel):
    returned: int
    data: list[BuildingGeometryRead]


class ReverseGeocodingRead(ApiModel):
    """Адрес и доступная геометрия ближайшего объекта."""

    provider_id: str
    address: str
    latitude: float
    longitude: float
    geometry: GeoJsonGeometry | None = None
    provider: Literal["nominatim"] = "nominatim"
    quality: str | None = None
    attribution: str = "© OpenStreetMap contributors"
