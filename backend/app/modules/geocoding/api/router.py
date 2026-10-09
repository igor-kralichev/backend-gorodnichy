from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from httpx import HTTPError

from app.core.security import CurrentActor
from app.modules.geocoding.api.schemas import (
    AddressSuggestionPage,
    BuildingGeometryPage,
    ReverseGeocodingRead,
)
from app.modules.geocoding.application.service import (
    AddressSuggestionService,
    GeocodingConfigurationError,
)

router = APIRouter(prefix="/geocoding", tags=["Геокодирование"])


@router.get(
    "/addressSuggestions",
    response_model=AddressSuggestionPage,
    response_model_by_alias=True,
    summary="Получить подсказки адресов",
    description=(
        "Возвращает адресные подсказки через DaData или локальный Nominatim. "
        "Доступно авторизованным пользователям."
    ),
)
async def suggest_addresses(
    _actor: CurrentActor,
    query: Annotated[
        str,
        Query(
            alias="q",
            min_length=3,
            max_length=300,
            description="Ввод пользователя в адресной строке",
        ),
    ],
    source: Annotated[
        Literal["dadata", "nominatim"],
        Query(description="Источник подсказок"),
    ] = "dadata",
    limit: Annotated[int, Query(ge=1, le=20, description="Количество подсказок")] = 10,
) -> AddressSuggestionPage:
    try:
        return await AddressSuggestionService().suggest(
            query=query,
            source=source,
            limit=limit,
        )
    except GeocodingConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except HTTPError as error:
        raise HTTPException(
            status_code=502,
            detail="Внешний сервис геокодирования временно недоступен",
        ) from error


@router.get(
    "/reverse",
    response_model=ReverseGeocodingRead,
    response_model_by_alias=True,
    summary="Определить адрес и контур по координатам",
)
async def reverse_geocode(
    _actor: CurrentActor,
    longitude: Annotated[float, Query(ge=-180, le=180)],
    latitude: Annotated[float, Query(ge=-90, le=90)],
) -> ReverseGeocodingRead:
    try:
        return await AddressSuggestionService().reverse(
            longitude=longitude,
            latitude=latitude,
        )
    except GeocodingConfigurationError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except HTTPError as error:
        raise HTTPException(status_code=502, detail="Nominatim временно недоступен") from error


@router.get(
    "/buildings",
    response_model=BuildingGeometryPage,
    response_model_by_alias=True,
    summary="Получить контуры зданий в ограниченной области",
)
async def list_buildings(
    _actor: CurrentActor,
    bbox: Annotated[str, Query(description="minLon,minLat,maxLon,maxLat")],
    query: Annotated[str | None, Query(alias="q", min_length=2, max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> BuildingGeometryPage:
    try:
        values = tuple(float(value.strip()) for value in bbox.split(","))
        if len(values) != 4:
            raise ValueError
        min_lon, min_lat, max_lon, max_lat = values
        if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
            raise ValueError
        if (max_lon - min_lon) * (max_lat - min_lat) > 4:
            raise HTTPException(status_code=422, detail="Площадь bbox слишком велика")
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Некорректный bbox") from error
    try:
        return await AddressSuggestionService().buildings(
            bbox=(min_lon, min_lat, max_lon, max_lat),
            query=query,
            limit=limit,
        )
    except HTTPError as error:
        raise HTTPException(status_code=502, detail="Nominatim временно недоступен") from error
