from __future__ import annotations

import asyncio
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session, session_factory
from app.core.security import CurrentActor, actor_from_token
from app.core.storage import get_minio_client
from app.modules.objects.api.schemas import (
    ObjectImportJobCommand,
    ObjectImportJobPage,
    ObjectImportJobRead,
)
from app.modules.objects.application.imports import ObjectImportService
from app.modules.objects.application.service import RuntimeEntityNotFound

router = APIRouter(prefix="/importJobs", tags=["Фоновые импорты"])
websocket_router = APIRouter(prefix="/importJobs", tags=["Фоновые импорты"])
JobId = Annotated[UUID, Path(alias="jobId", description="UUID фоновой задачи импорта")]


@router.get(
    "",
    response_model=ObjectImportJobPage,
    response_model_by_alias=True,
    summary="Получить свои фоновые импорты",
)
async def list_import_jobs(
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
    job_status: Annotated[
        Literal["queued", "running", "paused", "completed", "failed", "cancelled"] | None,
        Query(alias="status"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ObjectImportJobPage:
    return await ObjectImportService(session, get_minio_client()).list_page(
        actor_id=actor.id,
        status=job_status,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{jobId}",
    response_model=ObjectImportJobRead,
    response_model_by_alias=True,
    summary="Получить состояние фонового импорта",
)
async def get_import_job(
    job_id: JobId,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ObjectImportJobRead:
    return await _execute(
        ObjectImportService(session, get_minio_client()).get(job_id, actor.id)
    )


@router.post(
    "/{jobId}/command",
    response_model=ObjectImportJobRead,
    response_model_by_alias=True,
    summary="Управлять фоновым импортом",
    description="Поддерживает команды pause, resume и cancel.",
)
async def command_import_job(
    job_id: JobId,
    payload: ObjectImportJobCommand,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ObjectImportJobRead:
    return await _execute(
        ObjectImportService(session, get_minio_client()).command(
            job_id,
            payload.command,
            actor.id,
        )
    )


@websocket_router.websocket("/{jobId}/ws")
async def import_job_websocket(websocket: WebSocket, job_id: JobId) -> None:
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    try:
        actor = actor_from_token(token)
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    try:
        while True:
            async with session_factory() as session:
                service = ObjectImportService(session, get_minio_client())
                try:
                    job = await service.get(job_id, actor.id)
                except RuntimeEntityNotFound:
                    await websocket.send_json({"error": "import_job_not_found"})
                    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                    return
                await websocket.send_json(job.model_dump(mode="json", by_alias=True))
                if job.status in {"completed", "failed", "cancelled"}:
                    return

                try:
                    message = await asyncio.wait_for(websocket.receive_json(), timeout=1)
                except asyncio.TimeoutError:
                    continue
                command = message.get("command") if isinstance(message, dict) else None
                if command in {"pause", "resume", "cancel"}:
                    updated = await service.command(job_id, command, actor.id)
                    await websocket.send_json(updated.model_dump(mode="json", by_alias=True))
    except WebSocketDisconnect:
        return


async def _execute(awaitable):
    try:
        return await awaitable
    except RuntimeEntityNotFound as error:
        raise HTTPException(status_code=404, detail="Задача импорта не найдена") from error
