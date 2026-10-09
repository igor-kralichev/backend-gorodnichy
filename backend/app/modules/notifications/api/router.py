from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.security import CurrentActor
from app.modules.notifications.api.schemas import (
    NotificationPage,
    NotificationRead,
    NotificationReadState,
)
from app.modules.notifications.application.service import NotificationNotFound, NotificationService

router = APIRouter(prefix="/notifications", tags=["Уведомления"])


@router.get("", response_model=NotificationPage, response_model_by_alias=True, summary="Получить свои уведомления")
async def list_notifications(
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    unread_only: Annotated[bool, Query(alias="unreadOnly")] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> NotificationPage:
    return await NotificationService(session).list_page(
        user_id=actor.id,
        unread_only=unread_only,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/{notificationId}",
    response_model=NotificationRead,
    response_model_by_alias=True,
    summary="Изменить состояние прочтения уведомления",
)
async def update_notification(
    notification_id: Annotated[UUID, Path(alias="notificationId")],
    payload: NotificationReadState,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> NotificationRead:
    try:
        return await NotificationService(session).set_read(notification_id, actor.id, payload.read)
    except NotificationNotFound as error:
        raise HTTPException(status_code=404, detail="Уведомление не найдено") from error


@router.post("/readAll", response_model=dict[str, int], summary="Отметить все уведомления прочитанными")
async def read_all_notifications(
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, int]:
    return {"updated": await NotificationService(session).mark_all_read(actor.id)}
