from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import text

from app.core.database import session_factory

router = APIRouter(prefix="/health", tags=["Состояние сервиса"])


@router.get("/live", include_in_schema=False)
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", include_in_schema=False)
async def readiness(request: Request) -> dict[str, str]:
    try:
        async with session_factory() as session:
            await session.execute(text("select 1"))
        await request.app.state.redis.ping()
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Зависимости сервиса ещё не готовы",
        ) from error
    return {"status": "ok", "postgres": "ok", "redis": "ok"}
