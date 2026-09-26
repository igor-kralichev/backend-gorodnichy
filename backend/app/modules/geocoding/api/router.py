from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from httpx import HTTPError

from app.core.security import CurrentActor
from app.modules.geocoding.api.schemas import AddressSuggestionPage
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
