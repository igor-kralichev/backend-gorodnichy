from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications.api.schemas import NotificationPage, NotificationRead
from app.shared.db.models import NotificationModel


class NotificationNotFound(Exception):
    pass


class NotificationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_page(
        self,
        *,
        user_id: UUID,
        unread_only: bool,
        limit: int,
        offset: int,
    ) -> NotificationPage:
        conditions = [NotificationModel.user_id == user_id]
        if unread_only:
            conditions.append(NotificationModel.read_at.is_(None))
        total = int(
            await self._session.scalar(
                select(func.count()).select_from(NotificationModel).where(*conditions)
            )
            or 0
        )
        unread = int(
            await self._session.scalar(
                select(func.count()).select_from(NotificationModel).where(
                    NotificationModel.user_id == user_id,
                    NotificationModel.read_at.is_(None),
                )
            )
            or 0
        )
        rows = (
            await self._session.scalars(
                select(NotificationModel)
                .where(*conditions)
                .order_by(NotificationModel.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
        return NotificationPage(
            items=[self._response(row) for row in rows],
            total=total,
            unread=unread,
            limit=limit,
            offset=offset,
        )

    async def set_read(self, notification_id: UUID, user_id: UUID, read: bool) -> NotificationRead:
        async with self._session.begin():
            row = await self._session.scalar(
                select(NotificationModel)
                .where(
                    NotificationModel.id == notification_id,
                    NotificationModel.user_id == user_id,
                )
                .with_for_update()
            )
            if row is None:
                raise NotificationNotFound
            row.read_at = datetime.now(UTC) if read else None
        return self._response(row)

    async def mark_all_read(self, user_id: UUID) -> int:
        rows = list(
            await self._session.scalars(
                select(NotificationModel).where(
                    NotificationModel.user_id == user_id,
                    NotificationModel.read_at.is_(None),
                )
            )
        )
        now = datetime.now(UTC)
        for row in rows:
            row.read_at = now
        await self._session.commit()
        return len(rows)

    @staticmethod
    def _response(row: NotificationModel) -> NotificationRead:
        return NotificationRead(
            id=row.id,
            type=row.type,
            title=row.title,
            message=row.message,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            metadata=row.metadata_json,
            read_at=row.read_at,
            created_at=row.created_at,
        )
