from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.security import AdminActor, CurrentActor
from app.modules.access.application.service import AccessDenied
from app.modules.relations.api.schemas import (
    EntityRelationCreate,
    EntityRelationRead,
    EntityRelationUpdate,
    ObjectRelationCreate,
    ObjectRelationPatch,
    ObjectRelationRead,
    RelationDeleteRead,
)
from app.modules.relations.application.service import (
    RelationConflict,
    RelationNotFound,
    RelationService,
    RelationValidationError,
)

router = APIRouter(tags=["Связи объектов"])


@router.post("/entityRelations", response_model=EntityRelationRead, status_code=201, summary="Создать тип связи")
async def create_relation_definition(
    payload: EntityRelationCreate,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EntityRelationRead:
    return await _execute(RelationService(session).create_definition(payload, actor))


@router.get("/entityRelations", response_model=list[EntityRelationRead], summary="Получить типы связей")
async def list_relation_definitions(
    _actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    entity_id: Annotated[UUID | None, Query(alias="entityId")] = None,
) -> list[EntityRelationRead]:
    return await RelationService(session).list_definitions(entity_id)


@router.patch("/entityRelations/{relationId}", response_model=EntityRelationRead, summary="Изменить тип связи")
async def update_relation_definition(
    relation_id: Annotated[UUID, Path(alias="relationId")],
    payload: EntityRelationUpdate,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EntityRelationRead:
    return await _execute(
        RelationService(session).update_definition(relation_id, payload, actor)
    )


@router.post("/objectRelations", response_model=ObjectRelationRead, status_code=201, summary="Связать два объекта")
async def create_object_relation(
    payload: ObjectRelationCreate,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ObjectRelationRead:
    return await _execute(RelationService(session).create_link(payload, actor))


@router.get("/objectRelations", response_model=list[ObjectRelationRead], summary="Получить связи объекта")
async def list_object_relations(
    object_id: Annotated[UUID, Query(alias="objectId")],
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    relation_id: Annotated[UUID | None, Query(alias="relationId")] = None,
) -> list[ObjectRelationRead]:
    return await _execute(
        RelationService(session).list_links(
            object_id=object_id,
            relation_id=relation_id,
            actor=actor,
        )
    )


@router.patch("/objectRelations/{linkId}", response_model=ObjectRelationRead, summary="Изменить данные связи")
async def update_object_relation(
    link_id: Annotated[UUID, Path(alias="linkId")],
    payload: ObjectRelationPatch,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ObjectRelationRead:
    return await _execute(RelationService(session).update_link(link_id, payload, actor))


@router.delete("/objectRelations/{linkId}", response_model=RelationDeleteRead, summary="Удалить связь")
async def delete_object_relation(
    link_id: Annotated[UUID, Path(alias="linkId")],
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RelationDeleteRead:
    await _execute(RelationService(session).delete_link(link_id, actor))
    return RelationDeleteRead(id=link_id)


async def _execute(awaitable):
    try:
        return await awaitable
    except RelationNotFound as error:
        raise HTTPException(status_code=404, detail="Связь не найдена") from error
    except RelationValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RelationConflict as error:
        raise HTTPException(status_code=409, detail="Связь была изменена другим пользователем") from error
    except IntegrityError as error:
        raise HTTPException(status_code=409, detail="Такая связь уже существует") from error
    except AccessDenied as error:
        raise HTTPException(status_code=403, detail="Недостаточно прав для связи объектов") from error
