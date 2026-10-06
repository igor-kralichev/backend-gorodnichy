import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.api.router import api_router
from app.core.config import settings
from app.core.database import dispose_database
from app.core.redis import create_redis_client
from app.modules.imports.api.router import websocket_router as import_websocket_router

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    redis = create_redis_client()
    app.state.redis = redis
    try:
        yield
    finally:
        await redis.aclose()
        await dispose_database()


app = FastAPI(
    title=settings.app_name,
    description=(
        "API для настройки схем low-code сущностей и работы "
        "с их объектами, "
        "геометриями, проверками качества и историей изменений."
    ),
    version="0.1.0",
    lifespan=lifespan,
    openapi_tags=[
        {
            "name": "Схемы сущностей",
            "description": (
                "Создание, настройка и публикация "
                "схем low-code сущностей."
            ),
        },
        {
            "name": "Справочники",
            "description": (
                "Создание справочников и значений для полей типа enum."
            ),
        },
        {
            "name": "Объекты сущностей",
            "description": (
                "Универсальный CRUD объектов "
                "опубликованных сущностей."
            ),
        },
        {
            "name": "Файлы объектов",
            "description": (
                "Загрузка, скачивание и удаление фото "
                "и документов объектов через MinIO."
            ),
        },
        {
            "name": "Фоновые импорты",
            "description": (
                "Контроль задач массовой загрузки объектов "
                "через RabbitMQ и import-worker."
            ),
        },
        {
            "name": "Сгенерированный API",
            "description": (
                "Каталог и документация маршрутов, "
                "построенных из опубликованных схем."
            ),
        },
        {
            "name": "Пользователи",
            "description": (
                "Создание пользователей Keycloak и назначение realm roles."
            ),
        },
        {
            "name": "История изменений",
            "description": (
                "Единый журнал изменений сущностей, объектов, "
                "справочников и пользователей."
            ),
        },
        {
            "name": "Поиск и подсказки",
            "description": (
                "Быстрые подсказки по уже сохранённым объектам "
                "через материализованный поисковый индекс."
            ),
        },
        {
            "name": "Геокодирование",
            "description": (
                "Адресные подсказки и геокодинг через DaData "
                "или локальный Nominatim."
            ),
        },
        {
            "name": "Организации и права",
            "description": "Организации, членство, предметные права и сохранённые представления.",
        },
        {
            "name": "Excel",
            "description": "Предпросмотр, фоновый импорт, экспорт и шаблоны сопоставления XLSX.",
        },
        {
            "name": "Связи объектов",
            "description": "Типизированные связи между объектами разных сущностей.",
        },
        {
            "name": "Наборы изменений",
            "description": "Предварительная проверка и атомарное применение группы изменений.",
        },
        {
            "name": "Формы и процессы",
            "description": (
                "Версионируемые формы, сбор и актуализация данных, поручения "
                "и межведомственный обмен."
            ),
        },
    ],
    swagger_ui_init_oauth={
        "clientId": settings.keycloak_swagger_client_id,
        "usePkceWithAuthorizationCodeGrant": True,
        "scopes": "openid profile email",
    },
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, error: HTTPException) -> JSONResponse:
    """Вернуть предметную HTTP-ошибку в едином публичном контракте."""

    detail = error.detail
    if isinstance(detail, dict):
        code = str(detail.get("code") or _error_code(error.status_code))
        message = str(detail.get("message") or "Запрос не выполнен")
        details = {key: value for key, value in detail.items() if key not in {"code", "message"}}
    else:
        code = _error_code(error.status_code)
        message = detail if isinstance(detail, str) else "Запрос не выполнен"
        details = detail if isinstance(detail, list) else None
    return JSONResponse(
        status_code=error.status_code,
        content={
            "code": code,
            "message": message,
            "requestId": getattr(request.state, "request_id", None),
            "details": jsonable_encoder(details),
        },
        headers=error.headers,
    )


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    request: Request,
    error: RequestValidationError,
) -> JSONResponse:
    """Вернуть ошибки Pydantic с путями полей и идентификатором запроса."""

    return JSONResponse(
        status_code=422,
        content={
            "code": "validation_error",
            "message": "Параметры запроса не прошли проверку",
            "requestId": getattr(request.state, "request_id", None),
            "details": jsonable_encoder(error.errors()),
        },
    )


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """Добавить сквозной идентификатор запроса в контекст и ответ."""

    request_id = request.headers.get("X-Request-ID", "").strip() or str(uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


def _error_code(status_code: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        413: "payload_too_large",
        422: "validation_error",
        429: "rate_limit_exceeded",
    }.get(status_code, "http_error")


app.include_router(health_router)
app.include_router(api_router, prefix=settings.api_v1_prefix)
app.include_router(import_websocket_router, prefix=settings.api_v1_prefix)
