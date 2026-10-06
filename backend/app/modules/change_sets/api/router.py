from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.security import CurrentActor
from app.modules.access.api.schemas import PermissionAction
from app.modules.access.application.service import (
    AccessDenied,
    AccessResourceNotFound,
    AuthorizationService,
)
from app.modules.change_sets.api.schemas import ChangeSetCreate, ChangeSetDecision, ChangeSetRead
from app.modules.change_sets.application.service import (
    ChangeSetConflict,
    ChangeSetNotFound,
    ChangeSetService,
    ChangeSetValidationError,
)
from app.modules.entities.infrastructure.models import EntitySchemaModel
from app.shared.db.models import ChangeSetItemModel, ChangeSetModel

router = APIRouter(prefix="/changeSets", tags=["Наборы изменений"])
ChangeSetId = Annotated[UUID, Path(alias="changeSetId")]


@router.post("", response_model=ChangeSetRead, status_code=201, summary="Подготовить набор изменений")
async def create_change_set(
    payload: ChangeSetCreate,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ChangeSetRead:
    await _authorize_items(session, actor, payload.entity_code, payload.items)
    return await _execute(ChangeSetService(session).create(payload, actor))


@router.get("/{changeSetId}", response_model=ChangeSetRead, summary="Получить набор изменений")
async def get_change_set(
    change_set_id: ChangeSetId,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ChangeSetRead:
    await _authorize_existing(session, actor, change_set_id, decision=False)
    return await _execute(ChangeSetService(session).get(change_set_id))


@router.post("/{changeSetId}/decision", response_model=ChangeSetRead, summary="Применить или отклонить набор")
async def decide_change_set(
    change_set_id: ChangeSetId,
    payload: ChangeSetDecision,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ChangeSetRead:
    await _authorize_existing(
        session,
        actor,
        change_set_id,
        decision=True,
        decision_action=payload.action,
    )
    service = ChangeSetService(session)
    if payload.action == "apply":
        return await _execute(service.apply(change_set_id, actor))
    return await _execute(
        service.decide(
            change_set_id,
            "rejected" if payload.action == "reject" else "cancelled",
            actor,
        )
    )


async def _execute(awaitable):
    try:
        return await awaitable
    except ChangeSetNotFound as error:
        raise HTTPException(status_code=404, detail="Набор изменений не найден") from error
    except ChangeSetConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except IntegrityError as error:
        raise HTTPException(status_code=409, detail="Набор с таким Idempotency-Key уже существует") from error
    except ChangeSetValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


async def _authorize_items(session, actor, entity_code: str, items) -> None:
    authorization = AuthorizationService(session)
    checked: set[tuple[str, UUID | None]] = set()
    try:
        for item in items:
            action = _permission_for_operation(item.operation)
            key = (action, item.object_id)
            if key in checked:
                continue
            await authorization.require_entity_code(
                actor,
                action,
                entity_code,
                object_id=item.object_id,
            )
            checked.add(key)
    except AccessDenied as error:
        raise HTTPException(status_code=403, detail="Недостаточно прав для набора изменений") from error
    except AccessResourceNotFound as error:
        raise HTTPException(status_code=404, detail="Сущность или объект не найден") from error
    finally:
        await session.rollback()


async def _authorize_existing(
    session,
    actor,
    change_set_id: UUID,
    *,
    decision: bool,
    decision_action: str | None = None,
) -> None:
    row = (
        await session.execute(
            select(ChangeSetModel, EntitySchemaModel.code)
            .join(EntitySchemaModel, EntitySchemaModel.id == ChangeSetModel.entity_schema_id)
            .where(ChangeSetModel.id == change_set_id)
        )
    ).one_or_none()
    if row is None:
        await session.rollback()
        raise HTTPException(status_code=404, detail="Набор изменений не найден")
    change_set, entity_code = row
    items = list(
        await session.scalars(
            select(ChangeSetItemModel).where(ChangeSetItemModel.change_set_id == change_set.id)
        )
    )
    if not decision:
        read_targets = [item.object_id for item in items if item.object_id is not None] or [None]
        proxy_items = [type("ReadTarget", (), {"operation": "read", "object_id": value}) for value in read_targets]
        await _authorize_items(session, actor, entity_code, proxy_items)
        return
    if decision_action == "apply":
        await _authorize_items(session, actor, entity_code, items)
        return
    try:
        await AuthorizationService(session).require_entity_code(actor, "review", entity_code)
    except AccessDenied as error:
        raise HTTPException(status_code=403, detail="Недостаточно прав для решения по набору") from error
    except AccessResourceNotFound as error:
        raise HTTPException(status_code=404, detail="Сущность не найдена") from error
    finally:
        await session.rollback()


def _permission_for_operation(operation: str) -> PermissionAction:
    return {
        "create": "create",
        "update": "update",
        "archive": "archive",
        "confirm": "confirm",
        "read": "read",
    }[operation]
