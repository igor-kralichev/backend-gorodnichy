# Муниципальная low-code GIS-платформа

Репозиторий содержит backend и локальную инфраструктуру платформы для создания динамических сущностей, справочников и географических объектов без генерации отдельных таблиц и Python-модулей для каждой предметной области.

## Состав репозитория

| Каталог | Назначение | Основные порты |
|---|---|---|
| `backend/` | FastAPI API, Alembic, import-worker, outbox-worker и Redis | `8000` |
| `postgres/` | PostgreSQL 17 + PostGIS и опциональный pgAdmin | `5432`, `5050` |
| `keycloak/` | Keycloak и его отдельная PostgreSQL | `8080` |
| `minio/` | S3-совместимое файловое хранилище | `9000`, `9001` |
| `rabbitmq/` | Очереди фонового импорта и событий outbox | `5672`, `15672` |
| `nominatim/` | Локальный геокодер по Нижегородской области | `8081` |

Все проекты подключаются к общей Docker-сети `municipal_low_code_network`, которую создаёт проект `postgres/`. Каждый контур имеет собственный `Dockerfile` и `stack.yml`, но локально запускается обычной командой `docker compose`.

## Быстрый запуск

```bash
docker compose -f postgres/stack.yml up --build -d
docker compose -f keycloak/stack.yml up --build -d
docker compose -f minio/stack.yml up --build -d
docker compose -f rabbitmq/stack.yml up --build -d
docker compose -f nominatim/stack.yml up --build -d
docker compose -f backend/stack.yml up --build -d
```

Проверка готовности:

```bash
curl http://localhost:8000/health/live
curl http://localhost:8000/health/ready
```

Основные интерфейсы:

- Swagger API: <http://localhost:8000/docs>
- OpenAPI JSON: <http://localhost:8000/openapi.json>
- Swagger опубликованных low-code сущностей: <http://localhost:8000/api/v1/generated/docs>
- Keycloak: <http://localhost:8080>
- MinIO Console: <http://localhost:9001>
- RabbitMQ Management: <http://localhost:15672>
- pgAdmin: <http://localhost:5050> после запуска профиля `tools`

## Данные и пересборка

PostgreSQL, Keycloak, Redis, MinIO, RabbitMQ и Nominatim используют именованные Docker volumes. Обычные `up --build -d`, остановка и пересоздание контейнеров данные не удаляют. Не запускайте `docker compose down -v`, если volumes требуется сохранить.

Keycloak использует собственную PostgreSQL и не обращается к прикладной базе из `postgres/`.

## Авторизация

Все маршруты `/api/v1` требуют JWT Keycloak. Публичны только `/health/live` и `/health/ready`. Административные операции дополнительно требуют realm role `Admin`.

Локальные `admin/admin` относятся к администратору realm `master` и подходят для Keycloak Admin Console. Для Swagger и SPA нужен пользователь realm `municipal-low-code` с ролью `Admin` или `User`. OAuth-клиент SPA/Swagger — `municipal-spa`, flow — Authorization Code + PKCE.

## Документация

- [Руководство интеграции фронтенда](docs/FRONTEND_INTEGRATION.md) — полный пользовательский и API-флоу, фоновые задачи, ошибки и права.
- [Backend README](backend/README.md) — запуск, конфигурация и возможности API.
- [Архитектура](backend/ARCHITECTURE.md) — модули, данные, очереди и границы будущих сервисов.
- [Nominatim](nominatim/README.md) и [RabbitMQ](rabbitmq/README.md) — эксплуатация отдельных контуров.

## Текущие границы поставки

В текущую реализацию не входят in-app/email-уведомления, резервное копирование и восстановление, эксплуатационный мониторинг, автоматические тесты, демонстрационные данные и серверный модуль отчётности. Эти пункты намеренно отложены и не должны считаться доступными только на основании наличия инфраструктурных модулей.
