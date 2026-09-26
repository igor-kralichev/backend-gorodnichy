from typing import Any, Literal

from app.modules.entities.api.schemas import ApiModel


class AddressSuggestionRead(ApiModel):
    """Подсказка адреса из внешнего геокодера."""

    source: Literal["dadata", "nominatim"]
    value: str
    unrestricted_value: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    quality: str | None = None
    raw: dict[str, Any] | None = None


class AddressSuggestionPage(ApiModel):
    """Список подсказок адресов."""

    query: str
    returned: int
    data: list[AddressSuggestionRead]
