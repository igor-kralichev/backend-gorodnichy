from fastapi import APIRouter

from app.core.security import CurrentActor
from app.modules.entities.api.schemas import ALLOWED_MARKER_ICONS, ApiModel


class UnitRead(ApiModel):
    code: str
    name: str
    symbol: str


router = APIRouter(prefix="/metadata", tags=["Метаданные платформы"])

UNITS = (
    UnitRead(code="count", name="Количество", symbol="шт."),
    UnitRead(code="person", name="Человек", symbol="чел."),
    UnitRead(code="meter", name="Метр", symbol="м"),
    UnitRead(code="kilometer", name="Километр", symbol="км"),
    UnitRead(code="square_meter", name="Квадратный метр", symbol="м²"),
    UnitRead(code="hectare", name="Гектар", symbol="га"),
    UnitRead(code="percent", name="Процент", symbol="%"),
    UnitRead(code="ruble", name="Российский рубль", symbol="₽"),
)


@router.get(
    "/units",
    response_model=list[UnitRead],
    response_model_by_alias=True,
    summary="Получить каталог единиц измерения",
)
async def list_units(_actor: CurrentActor) -> list[UnitRead]:
    return list(UNITS)


@router.get(
    "/markerIcons",
    response_model=list[str],
    summary="Получить разрешённые иконки маркеров",
)
async def list_marker_icons(_actor: CurrentActor) -> list[str]:
    return sorted(ALLOWED_MARKER_ICONS)
