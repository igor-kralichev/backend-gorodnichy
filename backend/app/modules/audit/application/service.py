from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import ActorContext
from app.modules.audit.api.schemas import AuditEventPage, AuditEventRead, AuditResourceType
from app.shared.db.models import AuditEventModel


class AuditService:
    """Сервис записи и чтения единой истории изменений."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        actor: ActorContext,
        resource_type: AuditResourceType,
        resource_id: UUID | None,
        resource_code: str | None,
        resource_name: str | None,
        action: str,
        old_value: object | None,
        new_value: object | None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEventRead:
        old_payload = _json_payload(old_value)
        new_payload = _json_payload(new_value)
        event = AuditEventModel(
            resource_type=resource_type,
            resource_id=resource_id,
            resource_code=resource_code,
            resource_name=resource_name,
            action=action,
            actor_id=actor.id,
            actor_full_name=_actor_full_name(actor),
            actor_email=actor.email,
            old_value=old_payload,
            new_value=new_payload,
            changes=_changes(old_payload, new_payload),
            metadata_json=metadata or {},
        )
        self._session.add(event)
        await self._session.commit()
        await self._session.refresh(event)
        return _to_response(event)

    async def list_page(
        self,
        *,
        resource_type: AuditResourceType | None,
        resource_id: UUID | None,
        actor_id: UUID | None,
        action: str | None,
        limit: int,
        offset: int,
    ) -> AuditEventPage:
        conditions = []
        if resource_type is not None:
            conditions.append(AuditEventModel.resource_type == resource_type)
        if resource_id is not None:
            conditions.append(AuditEventModel.resource_id == resource_id)
        if actor_id is not None:
            conditions.append(AuditEventModel.actor_id == actor_id)
        if action is not None:
            conditions.append(AuditEventModel.action == action)

        total = int(
            await self._session.scalar(
                select(func.count()).select_from(AuditEventModel).where(*conditions)
            )
            or 0
        )
        events = (
            await self._session.scalars(
                select(AuditEventModel)
                .where(*conditions)
                .order_by(AuditEventModel.occurred_at.desc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
        return AuditEventPage(
            items=[_to_response(event) for event in events],
            total=total,
            limit=limit,
            offset=offset,
        )

    @staticmethod
    def redact_object_fields(
        page: AuditEventPage,
        allowed_codes: set[str],
    ) -> AuditEventPage:
        """Скрыть динамические значения, недоступные сотруднику."""

        for event in page.items:
            for payload in (event.old_value, event.new_value):
                if not payload:
                    continue
                values = payload.get("values")
                if isinstance(values, dict):
                    payload["values"] = {
                        code: value for code, value in values.items() if code in allowed_codes
                    }
            event.changes = [
                change
                for change in event.changes
                if not change.path.startswith("values.")
                or change.path.split(".", 1)[1] in allowed_codes
            ]
        return page


def _json_payload(value: object | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, dict):
        return value
    return {"value": value}


def _changes(
    old_value: dict[str, Any] | None,
    new_value: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    return _nested_changes(old_value or {}, new_value or {})


def _nested_changes(
    old_value: Any,
    new_value: Any,
    *,
    prefix: str = "",
) -> list[dict[str, Any]]:
    if isinstance(old_value, dict) and isinstance(new_value, dict):
        changes: list[dict[str, Any]] = []
        for key in sorted(set(old_value) | set(new_value)):
            path = f"{prefix}.{key}" if prefix else str(key)
            changes.extend(
                _nested_changes(
                    old_value.get(key),
                    new_value.get(key),
                    prefix=path,
                )
            )
        return changes
    if old_value == new_value:
        return []
    return [
        {
            "path": prefix,
            "oldValue": old_value,
            "newValue": new_value,
        }
    ]


def _actor_full_name(actor: ActorContext) -> str:
    return actor.display_name or actor.username or actor.email or "Неизвестный пользователь"


def _to_response(event: AuditEventModel) -> AuditEventRead:
    return AuditEventRead(
        id=event.id,
        resource_type=event.resource_type,
        resource_id=event.resource_id,
        resource_code=event.resource_code,
        resource_name=event.resource_name,
        action=event.action,
        actor_id=event.actor_id,
        actor_full_name=event.actor_full_name,
        actor_email=event.actor_email,
        occurred_at=event.occurred_at,
        old_value=event.old_value,
        new_value=event.new_value,
        changes=event.changes,
        metadata=event.metadata_json,
    )
