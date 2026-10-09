import re
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Security, status
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.security import APIKeyHeader
from fastapi.responses import HTMLResponse, ORJSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.core.security import (
    ActorContext,
    AdminActor,
    CurrentActor,
    actor_from_token,
    oauth2_scheme,
)
from app.modules.access.application.service import (
    AccessDenied,
    AccessResourceNotFound,
    AuthorizationService,
)
from app.modules.audit.application.service import AuditService
from app.modules.entities.infrastructure.models import EntitySchemaModel
from app.modules.objects.api.schemas import (
    EntityObjectPage,
    EntityObjectRead,
    GeneratedApiKeyDeleteRead,
    GeneratedApiKeyRead,
    ObjectFilter,
    ObjectSearch,
    RegistryTreePage,
    RegistryTreeSearch,
)
from app.modules.objects.application.api_keys import (
    GeneratedApiKeyClient,
    GeneratedApiKeyError,
    GeneratedApiKeyNotFound,
    GeneratedApiKeyValue,
)
from app.modules.objects.application.service import (
    RuntimeEntityNotFound,
    RuntimeObjectNotFound,
    RuntimeObjectService,
    RuntimeValidationError,
)

router = APIRouter(tags=["Сгенерированный API"])
read_router = APIRouter(tags=["Сгенерированный API"])
EntityCode = Annotated[str, Path(alias="entityCode", description="Уникальный код опубликованной сущности")]
ObjectId = Annotated[UUID, Path(alias="objectId", description="Идентификатор объекта")]
FILTER_PATTERN = re.compile(r"^filters\[([^]]+)]\[([^]]+)]$")
OPERATOR_ALIASES = {
    "eq": "equals",
    "ne": "notEquals",
    "neq": "notEquals",
    "starts": "startsWith",
    "ends": "endsWith",
    "gt": "greaterThan",
    "gte": "greaterOrEqual",
    "lt": "lessThan",
    "lte": "lessOrEqual",
    "notNull": "filled",
    "null": "empty",
}
generated_api_key = APIKeyHeader(
    name="X-API-Key",
    scheme_name="GeneratedApiKey",
    description="Ключ только для чтения конкретного сгенерированного API",
    auto_error=False,
)


@dataclass(frozen=True, slots=True)
class GeneratedReadAccess:
    actor: ActorContext | None
    via_api_key: bool


async def generated_read_access(
    entity_code: EntityCode,
    session: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[str | None, Depends(oauth2_scheme)] = None,
    api_key: Annotated[str | None, Security(generated_api_key)] = None,
) -> GeneratedReadAccess:
    """Разрешить чтение по JWT пользователя либо ключу конкретной сущности."""

    if token:
        return GeneratedReadAccess(actor=actor_from_token(token), via_api_key=False)
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Передайте Bearer JWT или заголовок X-API-Key",
            headers={"WWW-Authenticate": "Bearer, ApiKey"},
        )
    schema = await _active_schema(session, entity_code)
    try:
        valid = await GeneratedApiKeyClient().validate(schema.id, api_key)
    except GeneratedApiKeyError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    if not valid:
        raise HTTPException(status_code=401, detail="X-API-Key недействителен")
    return GeneratedReadAccess(actor=None, via_api_key=True)


@router.get(
    "/generated/catalog",
    response_class=ORJSONResponse,
    summary="Получить каталог сгенерированных ресурсов",
    description=(
        "Возвращает read-only маршруты и поля "
        "всех опубликованных low-code сущностей."
    ),
)
async def generated_catalog(
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, Any]]:
    schemas = await _accessible_active_schemas(session, actor)
    return [_resource(schema, actor.roles) for schema in schemas]


@router.get(
    "/generated/catalog/{entityCode}",
    response_class=ORJSONResponse,
    summary="Получить сгенерированный ресурс сущности",
    description=(
        "Возвращает read-only маршруты и поля одной опубликованной "
        "low-code сущности по её коду."
    ),
)
async def generated_entity_catalog(
    entity_code: EntityCode,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await _authorize_actor(session, actor, entity_code)
    schema = await _active_schema(session, entity_code)
    return _resource(schema, actor.roles)


@router.post(
    "/generated/apiKeys/{entityCode}",
    response_model=GeneratedApiKeyRead,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    summary="Создать API-ключ сгенерированного API",
    description=(
        "Создаёт в Keycloak confidential client для read-only API сущности. "
        "Полное значение X-API-Key доступно только администратору."
    ),
)
async def create_generated_api_key(
    entity_code: EntityCode,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GeneratedApiKeyRead:
    schema = await _active_schema(session, entity_code)
    try:
        value = await GeneratedApiKeyClient().create(schema.id, schema.code)
    except GeneratedApiKeyError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    await _audit_api_key(session, actor, schema, "generated_api_key.created")
    return _api_key_response(value)


@router.get(
    "/generated/apiKeys/{entityCode}",
    response_model=GeneratedApiKeyRead,
    response_model_by_alias=True,
    summary="Посмотреть API-ключ сгенерированного API",
    description="Возвращает X-API-Key только пользователю с realm role Admin.",
)
async def get_generated_api_key(
    entity_code: EntityCode,
    _actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GeneratedApiKeyRead:
    schema = await _active_schema(session, entity_code)
    try:
        return _api_key_response(await GeneratedApiKeyClient().get(schema.id, schema.code))
    except GeneratedApiKeyNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except GeneratedApiKeyError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post(
    "/generated/apiKeys/{entityCode}/rotate",
    response_model=GeneratedApiKeyRead,
    response_model_by_alias=True,
    summary="Перегенерировать API-ключ",
    description="Немедленно отзывает прежний secret в Keycloak и возвращает новый X-API-Key.",
)
async def rotate_generated_api_key(
    entity_code: EntityCode,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GeneratedApiKeyRead:
    schema = await _active_schema(session, entity_code)
    try:
        value = await GeneratedApiKeyClient().rotate(schema.id, schema.code)
    except GeneratedApiKeyNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except GeneratedApiKeyError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    await _audit_api_key(session, actor, schema, "generated_api_key.rotated")
    return _api_key_response(value)


@router.delete(
    "/generated/apiKeys/{entityCode}",
    response_model=GeneratedApiKeyDeleteRead,
    response_model_by_alias=True,
    summary="Отозвать API-ключ",
)
async def delete_generated_api_key(
    entity_code: EntityCode,
    actor: AdminActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GeneratedApiKeyDeleteRead:
    schema = await _active_schema(session, entity_code)
    try:
        await GeneratedApiKeyClient().delete(schema.id)
    except GeneratedApiKeyNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except GeneratedApiKeyError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    await _audit_api_key(session, actor, schema, "generated_api_key.deleted")
    return GeneratedApiKeyDeleteRead(entity_id=schema.id)


@router.get("/openapi.json", response_class=ORJSONResponse, include_in_schema=False)
async def generated_openapi(
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    schemas = await _accessible_active_schemas(session, actor)
    return _openapi_document(schemas)


@router.get(
    "/generated/{entityCode}/openapi.json",
    response_class=ORJSONResponse,
    include_in_schema=False,
)
async def generated_entity_openapi(
    entity_code: EntityCode,
    actor: CurrentActor,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await _authorize_actor(session, actor, entity_code)
    schema = await _active_schema(session, entity_code)
    return _openapi_document([schema], title=f"Сгенерированный API: {schema.name}")


@router.get("/generated/{entityCode}/docs", response_class=HTMLResponse, include_in_schema=False)
async def generated_entity_docs(entity_code: EntityCode) -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url=f"/api/v1/generated/{entity_code}/openapi.json",
        title=f"Сгенерированный API сущности {entity_code}",
        init_oauth={
            "clientId": settings.keycloak_swagger_client_id,
            "usePkceWithAuthorizationCodeGrant": True,
            "scopes": "openid profile email",
        },
    )


@read_router.get(
    "/generated/{entityCode}/objects",
    response_model=EntityObjectPage,
    response_model_by_alias=True,
    summary="Получить объекты через сгенерированный API",
    description="Read-only выдача по Bearer JWT или X-API-Key конкретной сущности.",
)
async def generated_list_objects(
    entity_code: EntityCode,
    request: Request,
    access: Annotated[GeneratedReadAccess, Depends(generated_read_access)],
    session: Annotated[AsyncSession, Depends(get_session)],
    parent_object_id: Annotated[UUID | None, Query(alias="parentObjectId")] = None,
    logic: Literal["and", "or"] = Query(default="and"),
    sort: str | None = Query(default=None, max_length=130),
    limit: int = Query(default=25, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    bbox: str | None = Query(default=None),
) -> EntityObjectPage:
    filters = _query_filters(request)
    await _authorize_generated_filters(session, access, entity_code, filters)
    return await _execute_generated(
        RuntimeObjectService(session).list_page(
            entity_code,
            ObjectSearch(
                logic=logic,
                filters=filters,
                sort=sort,
                parent_object_id=parent_object_id,
                bbox=_parse_bbox(bbox) if bbox else None,
                limit=limit,
                offset=offset,
            ),
            actor=access.actor,
        )
    )


@read_router.post(
    "/generated/{entityCode}/objects/search",
    response_model=EntityObjectPage,
    response_model_by_alias=True,
    summary="Найти объекты через сгенерированный API",
    description="Сложный read-only поиск по Bearer JWT или X-API-Key.",
)
async def generated_search_objects(
    entity_code: EntityCode,
    payload: ObjectSearch,
    access: Annotated[GeneratedReadAccess, Depends(generated_read_access)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EntityObjectPage:
    await _authorize_generated_filters(session, access, entity_code, payload.filters)
    return await _execute_generated(
        RuntimeObjectService(session).list_page(
            entity_code,
            payload,
            actor=access.actor,
        )
    )


@read_router.post(
    "/generated/{entityCode}/objects/queryTree",
    response_model=RegistryTreePage,
    response_model_by_alias=True,
    summary="Получить реестр с подреестрами через сгенерированный API",
    description=(
        "Read-only составная выдача. X-API-Key родительской сущности разрешает "
        "читать её непосредственные подреестры в рамках одного запроса."
    ),
)
async def generated_query_tree(
    entity_code: EntityCode,
    payload: RegistryTreeSearch,
    access: Annotated[GeneratedReadAccess, Depends(generated_read_access)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RegistryTreePage:
    await _authorize_generated_filters(session, access, entity_code, payload.filters)
    for child in payload.children:
        if access.actor is not None:
            await _authorize_actor(session, access.actor, child.entity_code)
        await _authorize_generated_filters(
            session,
            access,
            child.entity_code,
            child.filters,
            allow_parent=access.actor is None,
        )
    return await _execute_generated(
        RuntimeObjectService(session).list_tree(
            entity_code,
            payload,
            actor=access.actor,
        )
    )


@read_router.get(
    "/generated/{entityCode}/objects/{objectId}",
    response_model=EntityObjectRead,
    response_model_by_alias=True,
    summary="Получить объект через сгенерированный API",
    description="Read-only карточка по Bearer JWT или X-API-Key.",
)
async def generated_get_object(
    entity_code: EntityCode,
    object_id: ObjectId,
    access: Annotated[GeneratedReadAccess, Depends(generated_read_access)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EntityObjectRead:
    if access.actor is not None:
        await _authorize_actor(session, access.actor, entity_code, object_id)
    return await _execute_generated(
        RuntimeObjectService(session).get(
            entity_code,
            object_id,
            actor_roles=access.actor.roles if access.actor is not None else None,
        )
    )


async def _authorize_generated_filters(
    session: AsyncSession,
    access: GeneratedReadAccess,
    entity_code: str,
    filters: list[ObjectFilter],
    *,
    allow_parent: bool = False,
) -> None:
    if access.actor is None:
        forbidden = [
            item.field
            for item in filters
            if "." in item.field
            and not (allow_parent and item.field.startswith("parent."))
        ]
        if forbidden:
            raise HTTPException(
                status_code=403,
                detail=(
                    "X-API-Key не разрешает фильтрацию по произвольной "
                    "связанной сущности"
                ),
            )
        return
    await _authorize_actor(session, access.actor, entity_code)
    try:
        related_codes = await RuntimeObjectService(session).filter_dependency_entity_codes(
            entity_code,
            filters,
        )
        authorization = AuthorizationService(session)
        for related_code in related_codes:
            await authorization.require_entity_code(access.actor, "read", related_code)
    except AccessDenied as error:
        raise HTTPException(
            status_code=403,
            detail="Недостаточно прав для фильтрации по связанной сущности",
        ) from error
    except (AccessResourceNotFound, RuntimeEntityNotFound) as error:
        raise HTTPException(status_code=404, detail="Связанная сущность не найдена") from error
    finally:
        await session.rollback()


async def _authorize_actor(
    session: AsyncSession,
    actor: ActorContext,
    entity_code: str,
    object_id: UUID | None = None,
) -> None:
    try:
        await AuthorizationService(session).require_entity_code(
            actor,
            "read",
            entity_code,
            object_id=object_id,
        )
    except AccessDenied as error:
        raise HTTPException(status_code=403, detail="Недостаточно прав для чтения") from error
    except AccessResourceNotFound as error:
        raise HTTPException(status_code=404, detail="Сущность или объект не найден") from error
    finally:
        await session.rollback()


async def _execute_generated[ResultT](awaitable: Awaitable[ResultT]) -> ResultT:
    try:
        return await awaitable
    except RuntimeEntityNotFound as error:
        raise HTTPException(status_code=404, detail="Опубликованная сущность не найдена") from error
    except RuntimeObjectNotFound as error:
        raise HTTPException(status_code=404, detail="Объект не найден") from error
    except RuntimeValidationError as error:
        raise HTTPException(status_code=422, detail=error.issues) from error


def _query_filters(request: Request) -> list[ObjectFilter]:
    filters: list[ObjectFilter] = []
    for key, value in request.query_params.multi_items():
        match = FILTER_PATTERN.match(key)
        if not match:
            continue
        field, raw_operator = match.groups()
        operator = OPERATOR_ALIASES.get(raw_operator, raw_operator)
        try:
            filters.append(
                ObjectFilter(
                    field=field.removeprefix("values."),
                    operator=operator,
                    value=value,
                )
            )
        except ValueError as error:
            raise HTTPException(
                status_code=422,
                detail=f"Оператор фильтра «{raw_operator}» не поддерживается",
            ) from error
    return filters


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    try:
        bbox = tuple(float(part.strip()) for part in value.split(","))
    except ValueError as error:
        raise HTTPException(status_code=422, detail="bbox должен содержать четыре числа") from error
    if len(bbox) != 4:
        raise HTTPException(status_code=422, detail="bbox должен содержать четыре числа")
    min_lon, min_lat, max_lon, max_lat = bbox
    if min_lon >= max_lon or min_lat >= max_lat:
        raise HTTPException(status_code=422, detail="Границы bbox заданы некорректно")
    return min_lon, min_lat, max_lon, max_lat


def _api_key_response(value: GeneratedApiKeyValue) -> GeneratedApiKeyRead:
    return GeneratedApiKeyRead(
        entity_id=value.entity_id,
        entity_code=value.entity_code,
        client_id=value.client_id,
        api_key=value.api_key,
    )


async def _audit_api_key(
    session: AsyncSession,
    actor: ActorContext,
    schema: EntitySchemaModel,
    action: str,
) -> None:
    await AuditService(session).record(
        actor=actor,
        resource_type="generated_api_key",
        resource_id=schema.id,
        resource_code=schema.code,
        resource_name=schema.name,
        action=action,
        old_value=None,
        new_value={
            "entityId": str(schema.id),
            "entityCode": schema.code,
            "secretRecorded": False,
        },
    )


def _openapi_document(
    schemas: list[EntitySchemaModel],
    *,
    title: str = "Сгенерированный API муниципальной платформы",
) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for schema in schemas:
        base = f"/api/v1/generated/{schema.code}/objects"
        paths[base] = {
            "get": _operation(f"Список: {schema.name}", "list", schema.code, response_status="200"),
        }
        paths[f"{base}/search"] = {
            "post": _operation(
                f"Поиск: {schema.name}",
                "search",
                schema.code,
                request_schema={"type": "object", "additionalProperties": True},
                response_status="200",
            )
        }
        paths[f"{base}/queryTree"] = {
            "post": _operation(
                f"Реестр с подреестрами: {schema.name}",
                "query_tree",
                schema.code,
                request_schema={"type": "object", "additionalProperties": True},
                response_status="200",
            )
        }
        paths[f"{base}/{{objectId}}"] = {
            "get": _operation(f"Карточка: {schema.name}", "get", schema.code, item=True, response_status="200"),
        }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": title,
            "description": (
                "Read-only маршруты, автоматически построенные из опубликованных "
                "схем. Авторизация: Keycloak JWT либо X-API-Key сущности."
            ),
            "version": "1.0.0",
        },
        "servers": [{"url": "http://localhost:8000"}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "Keycloak": {
                    "type": "oauth2",
                    "flows": {
                        "authorizationCode": {
                            "authorizationUrl": settings.keycloak_authorization_url,
                            "tokenUrl": settings.keycloak_token_url,
                            "scopes": {
                                "openid": "Идентификатор пользователя",
                                "profile": "Профиль пользователя",
                                "email": "Электронная почта пользователя",
                            },
                        }
                    },
                },
                "GeneratedApiKey": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                    "description": "Read-only ключ выбранной сущности",
                },
            }
        },
    }


@router.get("/generated/docs", response_class=HTMLResponse, include_in_schema=False)
async def generated_docs() -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url="/api/v1/openapi.json",
        title="Сгенерированный API сущностей",
        init_oauth={
            "clientId": settings.keycloak_swagger_client_id,
            "usePkceWithAuthorizationCodeGrant": True,
            "scopes": "openid profile email",
        },
    )


async def _active_schemas(session: AsyncSession) -> list[EntitySchemaModel]:
    return list(
        (
            await session.scalars(
                select(EntitySchemaModel)
                .where(EntitySchemaModel.status == "active")
                .order_by(EntitySchemaModel.name)
            )
        ).all()
    )


async def _accessible_active_schemas(
    session: AsyncSession,
    actor: ActorContext,
) -> list[EntitySchemaModel]:
    """Вернуть только опубликованные сущности, доступные пользователю."""

    authorization = AuthorizationService(session)
    visible: list[EntitySchemaModel] = []
    for schema in await _active_schemas(session):
        try:
            await authorization.require_entity_code(actor, "read", schema.code)
        except (AccessDenied, AccessResourceNotFound):
            continue
        visible.append(schema)
    return visible


async def _active_schema(session: AsyncSession, entity_code: str) -> EntitySchemaModel:
    schema = await session.scalar(
        select(EntitySchemaModel).where(
            EntitySchemaModel.code == entity_code,
            EntitySchemaModel.status == "active",
        )
    )
    if schema is None:
        raise HTTPException(
            status_code=404,
            detail="Опубликованная схема сущности не найдена",
        )
    return schema


def _resource(
    schema: EntitySchemaModel,
    actor_roles: frozenset[str],
) -> dict[str, Any]:
    base = f"/api/v1/generated/{schema.code}/objects"
    return {
        "id": str(schema.id),
        "code": schema.code,
        "name": schema.name,
        "version": schema.current_version,
        "fields": [
            {
                "code": field.code,
                "name": field.name,
                "type": field.field_type,
                "multiple": field.multiple,
                "filterable": field.filterable,
                "referenceEntityId": (
                    str(field.reference_entity_schema_id)
                    if field.reference_entity_schema_id
                    else None
                ),
            }
            for field in schema.fields
            if not field.archived
            and RuntimeObjectService._field_is_readable(field, actor_roles)
        ],
        "endpoints": [
            {"method": "GET", "path": base},
            {"method": "POST", "path": f"{base}/search"},
            {"method": "POST", "path": f"{base}/queryTree"},
            {"method": "GET", "path": f"{base}/{{objectId}}"},
        ],
        "authentication": ["Bearer JWT", "X-API-Key"],
    }


def _operation(
    summary: str,
    action: str,
    entity_code: str,
    *,
    item: bool = False,
    request_schema: dict[str, Any] | None = None,
    response_status: str,
) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "summary": summary,
        "operationId": f"{action}_{entity_code}",
        "tags": ["Объекты опубликованных сущностей"],
        "responses": {response_status: {"description": "Запрос успешно выполнен"}},
    }
    if item:
        operation["parameters"] = [
            {
                "name": "objectId",
                "in": "path",
                "required": True,
                "description": "Идентификатор объекта",
                "schema": {"type": "string", "format": "uuid"},
            }
        ]
    if request_schema is not None:
        operation["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": request_schema}},
        }
    operation["security"] = [
        {"Keycloak": ["openid", "profile", "email"]},
        {"GeneratedApiKey": []},
    ]
    return operation
