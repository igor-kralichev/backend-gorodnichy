import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
    ],
    swagger_ui_init_oauth={
        "clientId": settings.keycloak_swagger_client_id,
        "usePkceWithAuthorizationCodeGrant": True,
        "scopes": "openid profile email",
    },
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(health_router)
app.include_router(api_router, prefix=settings.api_v1_prefix)
app.include_router(import_websocket_router, prefix=settings.api_v1_prefix)
