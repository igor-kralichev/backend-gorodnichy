from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.security import CurrentActor
from app.modules.search.api.schemas import ObjectSuggestionPage
from app.modules.search.application.service import ObjectSuggestionService

router = APIRouter(prefix="/search", tags=["Поиск и подсказки"])


@router.get(
    "/objectSuggestions",
    response_model=ObjectSuggestionPage,
    response_model_by_alias=True,
    summary="Получить подсказки по сохранённым объектам",
    description=(
        "Ищет по материализованному индексу значений объектов. "
        "В выдачу попадают только объекты опубликованных сущностей, "
        "которые не находятся в архиве. Доступно авторизованным пользователям."
    ),
)
async def suggest_objects(
    _actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    query: Annotated[
        str,
        Query(
            alias="q",
            min_length=2,
            max_length=200,
            description="Строка поиска: адрес, название или другое индексируемое поле",
        ),
    ],
    entity_code: Annotated[
        str | None,
        Query(alias="entityCode", max_length=120, description="Ограничить поиск сущностью"),
    ] = None,
    field_code: Annotated[
        str | None,
        Query(alias="fieldCode", max_length=120, description="Ограничить поиск конкретным полем"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=50, description="Количество подсказок")] = 10,
) -> ObjectSuggestionPage:
    return await ObjectSuggestionService(session).suggest(
        query=query,
        entity_code=entity_code,
        field_code=field_code,
        limit=limit,
    )
