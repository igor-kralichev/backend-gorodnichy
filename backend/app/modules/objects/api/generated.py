from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, ORJSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.modules.entities.infrastructure.models import EntityFieldModel, EntitySchemaModel

router = APIRouter(tags=["Сгенерированный API"])
EntityCode = Annotated[str, Path(alias="entityCode", description="Уникальный код опубликованной сущности")]


@router.get(
    "/generated/catalog",
    response_class=ORJSONResponse,
    summary="Получить каталог сгенерированных ресурсов",
    description=(
        "Возвращает CRUD-маршруты и поля "
        "всех опубликованных low-code сущностей."
    ),
)
async def generated_catalog(session: Annotated[AsyncSession, Depends(get_session)]) -> list[dict[str, Any]]:
    schemas = await _active_schemas(session)
    return [_resource(schema) for schema in schemas]


@router.get(
    "/generated/catalog/{entityCode}",
    response_class=ORJSONResponse,
    summary="Получить сгенерированный ресурс сущности",
    description=(
        "Возвращает CRUD-маршруты и поля одной опубликованной "
        "low-code сущности по её коду."
    ),
)
async def generated_entity_catalog(
    entity_code: EntityCode,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    schema = await _active_schema(session, entity_code)
    return _resource(schema)


@router.get("/openapi.json", response_class=ORJSONResponse, include_in_schema=False)
async def generated_openapi(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    schemas = await _active_schemas(session)
    return _openapi_document(schemas)


@router.get(
    "/generated/{entityCode}/openapi.json",
    response_class=ORJSONResponse,
    include_in_schema=False,
)
async def generated_entity_openapi(
    entity_code: EntityCode,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
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


def _openapi_document(
    schemas: list[EntitySchemaModel],
    *,
    title: str = "Сгенерированный API муниципальной платформы",
) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for schema in schemas:
        base = f"/api/v1/entities/{schema.code}/objects"
        object_schema = _object_payload_schema(schema.fields)
        create_schema = {"type": "array", "items": object_schema, "minItems": 1}
        paths[base] = {
            "get": _operation(f"Список: {schema.name}", "list", schema.code, response_status="200"),
            "post": _operation(
                f"Создание: {schema.name}",
                "create",
                schema.code,
                request_schema=create_schema,
                response_status="201",
                requires_auth=True,
            ),
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
        paths[f"{base}/{{objectId}}"] = {
            "get": _operation(f"Карточка: {schema.name}", "get", schema.code, item=True, response_status="200"),
            "patch": _operation(
                f"Изменение: {schema.name}",
                "update",
                schema.code,
                item=True,
                request_schema=object_schema,
                response_status="200",
                requires_auth=True,
            ),
            "delete": _operation(
                f"Удаление: {schema.name}",
                "delete",
                schema.code,
                item=True,
                response_status="200",
                requires_auth=True,
            ),
        }
        paths[f"{base}/{{objectId}}/archive"] = {
            "post": _operation(
                f"Архивация: {schema.name}",
                "archive",
                schema.code,
                item=True,
                response_status="200",
                requires_auth=True,
            )
        }
        paths[f"{base}/{{objectId}}/restore"] = {
            "post": _operation(
                f"Восстановление: {schema.name}",
                "restore",
                schema.code,
                item=True,
                response_status="200",
                requires_auth=True,
            )
        }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": title,
            "description": (
                "CRUD-маршруты, автоматически построенные "
                "из опубликованных схем сущностей."
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
                }
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


def _resource(schema: EntitySchemaModel) -> dict[str, Any]:
    base = f"/api/v1/entities/{schema.code}/objects"
    return {
        "id": str(schema.id),
        "code": schema.code,
        "name": schema.name,
        "version": schema.current_version,
        "fields": [{"code": field.code, "name": field.name, "type": field.field_type} for field in schema.fields],
        "endpoints": [
            {"method": "GET", "path": base},
            {"method": "POST", "path": base},
            {"method": "POST", "path": f"{base}/search"},
            {"method": "GET", "path": f"{base}/{{objectId}}"},
            {"method": "PATCH", "path": f"{base}/{{objectId}}"},
            {"method": "POST", "path": f"{base}/{{objectId}}/archive"},
            {"method": "DELETE", "path": f"{base}/{{objectId}}"},
            {"method": "POST", "path": f"{base}/{{objectId}}/restore"},
        ],
    }


def _operation(
    summary: str,
    action: str,
    entity_code: str,
    *,
    item: bool = False,
    request_schema: dict[str, Any] | None = None,
    response_status: str,
    requires_auth: bool = False,
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
    if requires_auth:
        operation["security"] = [{"Keycloak": ["openid", "profile", "email"]}]
    return operation


def _object_payload_schema(fields: list[EntityFieldModel]) -> dict[str, Any]:
    properties = {field.code: _field_schema(field) for field in fields}
    required = [field.code for field in fields if field.required]
    values: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        values["required"] = required
    return {
        "type": "object",
        "properties": {
            "parentObjectId": {
                "oneOf": [
                    {"type": "string", "format": "uuid"},
                    {"type": "null"},
                ],
                "description": "UUID родительского объекта для дочерних сущностей",
            },
            "values": values,
            "geometry": {
                "oneOf": [
                    {"type": "object", "additionalProperties": True},
                    {"type": "null"},
                ]
            },
        },
        "required": ["values"],
        "additionalProperties": False,
    }


def _field_schema(field: EntityFieldModel) -> dict[str, Any]:
    mapping: dict[str, dict[str, Any]] = {
        "integer": {"type": "integer"},
        "decimal": {"type": "number"},
        "boolean": {"type": "boolean"},
        "date": {"type": "string", "format": "date"},
        "datetime": {"type": "string", "format": "date-time"},
        "reference": {"type": "string", "format": "uuid"},
    }
    return {"title": field.name, **mapping.get(field.field_type, {"type": "string"})}
