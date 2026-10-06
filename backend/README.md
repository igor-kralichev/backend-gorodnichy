# Backend муниципальной low-code GIS-платформы

FastAPI-приложение реализует конструктор схем, универсальный CRUD объектов, справочники, PostGIS-геометрию, файлы MinIO, Excel-обмен, фоновые импорты, права, аудит и прикладные процессы. Точные контракты доступны в OpenAPI; сценарии frontend описаны в [FRONTEND_INTEGRATION.md](../docs/FRONTEND_INTEGRATION.md), устройство модулей — в [ARCHITECTURE.md](ARCHITECTURE.md).

## Запуск

Из корня репозитория последовательно запустите инфраструктуру и backend:

```bash
docker compose -f postgres/stack.yml up --build -d
docker compose -f keycloak/stack.yml up --build -d
docker compose -f minio/stack.yml up --build -d
docker compose -f rabbitmq/stack.yml up --build -d
docker compose -f nominatim/stack.yml up --build -d
docker compose -f backend/stack.yml up --build -d
```

`postgres/stack.yml` первым создаёт внешнюю сеть `municipal_low_code_network`. Миграции Alembic выполняет контейнер `migrate`; API и workers стартуют после успешной миграции.

```bash
docker compose -f backend/stack.yml ps
docker compose -f backend/stack.yml logs -f api import-worker outbox-worker migrate
curl http://localhost:8000/health/ready
```

Интерфейсы:

- API Swagger — <http://localhost:8000/docs>
- основной OpenAPI — <http://localhost:8000/openapi.json>
- динамический Swagger active-сущностей — <http://localhost:8000/api/v1/generated/docs>
- динамический OpenAPI — <http://localhost:8000/api/v1/openapi.json>
- каталог ресурсов — <http://localhost:8000/api/v1/generated/catalog>
- Swagger одной сущности — `/api/v1/generated/{entityCode}/docs`

## Авторизация и роли

Все `/api/v1` маршруты защищены JWT Keycloak; публичны только health-checks. SPA и Swagger используют Authorization Code + PKCE, realm `municipal-low-code`, public client `municipal-spa`. Backend проверяет подпись/issuer токена и получает пользователя из `sub`, ФИО из `name`, email из `email`, роли из `realm_access.roles`.

Refresh token остаётся между Keycloak и SPA: frontend хранит его только в памяти `keycloak-js` и получает новый токен при ротации. FastAPI принимает только bearer access token и не содержит `/auth/refresh`. Realm выдаёт access token на 5 минут, завершает idle session через 30 минут, ограничивает session 12 часами и запрещает повторное использование refresh token.

Realm role `Admin` требуется для управления схемами, справочниками, пользователями Keycloak, организациями/правами, типами связей, формами и аудитом. Runtime-действия дополнительно проверяются предметными permission grants. Старые `X-Actor-*` заголовки не используются.

Локальные `admin/admin` — bootstrap-администратор Keycloak realm `master`, а не пользователь приложения. Для Swagger создайте пользователя в `municipal-low-code` и назначьте realm role.

## Основные модули

- `entities` — схемы, поля, версии, статусы `draft/active/archived`, слои карты;
- `dictionaries` — справочники enum и нормализация значений;
- `objects` — JSONB-объекты, PostGIS, вложенность, фильтры, кластеры и динамический OpenAPI;
- `excel` — preview/import/export XLSX и пользовательские профили сопоставления;
- `imports` — состояния и команды фоновых задач, WebSocket прогресса;
- `change_sets` — проверка и атомарное применение пакета изменений;
- `attachments` — метаданные и версии файлов в MinIO;
- `access` — организации, memberships, permission grants и saved views;
- `relations` — типы связей схем и связи объектов;
- `workflows` — формы, сбор/актуализация, поручения и межведомственный обмен;
- `users` — Keycloak Admin API;
- `audit` — единый журнал действий;
- `search`, `geocoding` — trigram-подсказки, DaData и Nominatim.

## Сущности и справочники

`POST /api/v1/entities` создаёт черновик, поля и версию схемы атомарно. Поля поддерживают `string`, `text`, `integer`, `decimal`, `boolean`, `date`, `datetime`, `address`, `enum`, `reference`, `file`, `phone`, `email`, `url`, `calculated`. Для `enum` обязателен существующий `enumId`, для `reference` — `referenceEntityId`, для `calculated` — формула.

Код генерируется из названия; при совпадении автоматически добавляется `_2`, `_3` и т. д. Явно переданный занятый код возвращает `409`. `includeAddress=true` добавляет системное адресное поле. `parentEntityId` задаёт универсальную вложенность схем.

```text
GET/POST          /api/v1/entities
GET/PATCH/DELETE  /api/v1/entities/{identifier}
POST              /api/v1/entities/{identifier}/publish|archive|restore|duplicate

GET/POST          /api/v1/dictionaries
GET/PATCH/DELETE  /api/v1/dictionaries/{dictionaryId}
POST              /api/v1/dictionaries/{dictionaryId}/archive|restore
```

Публикация схемы с геометрией создаёт/синхронизирует `entity_layers`. Архив скрывает сущность из runtime и выключает слой; восстановление возвращает `draft`. Полное удаление проверяет связи и корректно очищает зависимые данные.

## Универсальный API объектов

Runtime доступен только active-сущностям. `POST /api/v1/entities/{entityCode}/objects` принимает JSON-массив `EntityObjectCreate`, даже если создаётся один объект. Небольшой массив обрабатывается синхронно (`201`); при превышении `IMPORT_ASYNC_THRESHOLD` backend сам сохраняет временный JSON в MinIO, создаёт job, публикует сообщение в RabbitMQ и возвращает `202`.

```json
[
  {
    "values": {"nazvanie": "МАОУ Лицей № 36", "address": "г. Нижний Новгород, пр. Кирова, д. 29а"},
    "geometry": {"type": "Point", "coordinates": [43.87, 56.24]}
  }
]
```

Поддерживаются GeoJSON `Point`, `MultiPoint`, `LineString`, `MultiLineString`, `Polygon`, `MultiPolygon`, `GeometryCollection`, SRID 4326. Объект дочерней сущности обязан иметь `parentObjectId`. Полностью валидная запись публикуется автоматически; неполная сохраняется как draft с `dataQuality` и `validationErrors`.

`PATCH` поддерживает optimistic locking по `revision`. Поля enum принимают UUID, code или name элемента; хранится code, а человекочитаемый текст возвращается в `displayValues`.

```text
GET  /api/v1/importJobs/{jobId}
POST /api/v1/importJobs/{jobId}/command
WS   /api/v1/importJobs/{jobId}/ws?token=ACCESS_TOKEN
```

Команды: `pause`, `resume`, `cancel`. Временный файл удаляется при завершении, ошибке или отмене.

## Excel

```text
POST /api/v1/entities/{entityCode}/excel/preview
POST /api/v1/entities/{entityCode}/excel/import
GET  /api/v1/entities/{entityCode}/excel/export
POST/GET /api/v1/importProfiles
DELETE /api/v1/importProfiles/{profileId}
```

Preview и import принимают `multipart/form-data` с `.xlsx` и необязательной JSON-строкой `mapping`. Preview ничего не сохраняет; import всегда создаёт фоновую задачу (`202`). Worker читает cached results формул, но не является Excel calculation engine: книгу с формулами надо предварительно пересчитать и сохранить в Excel/LibreOffice.

Ограничения по умолчанию: 30 МБ, 20 листов, 50 000 строк, 200 колонок, 200 МБ распакованных данных. Экспорт добавляет техническую строку с object id, revision, schema version, action и UUID полей для безопасного повторного импорта. Геометрии и файлы в XLSX пока не экспортируются.

## ChangeSet

`POST /api/v1/changeSets` создаёт атомарный набор `create/update/archive/confirm`; `GET /{id}` возвращает результаты проверки; `POST /{id}/decision` принимает `apply/reject/cancel`. Update требует `baseRevision`. `idempotencyKey` защищает повтор запроса, а тот же ключ с другим payload даёт `409`. Excel и формы используют тот же механизм вместо частичной записи строк.

## Файлы

Один bucket `municipal-attachments`; бинарники лежат по техническим UUID внутри папки объекта, PostgreSQL хранит метаданные и версии. Пользователю возвращается оригинальное имя. Фото принимают изображения, документы — PDF/DOC/DOCX; лимит по умолчанию 50 МБ.

Маршруты attachments поддерживают upload/list/read/download/update metadata/replace file/versions/delete. При полном удалении объекта удаляются и бинарники MinIO.

## Поиск и геокодирование

`GET /api/v1/search/objectSuggestions` использует read-model и `pg_trgm`/GIN индексы. `GET /api/v1/geocoding/addressSuggestions` проксирует `source=dadata|nominatim`, не раскрывая ключ DaData браузеру. Региональный Nominatim работает отдельным контейнером.

## Ошибки

```json
{
  "code": "conflict",
  "message": "Описание ошибки",
  "requestId": "uuid",
  "details": null
}
```

Frontend должен логировать `requestId`. Основные статусы: `401`, `403`, `404`, `409`, `413`, `422`, `429`, `502`, `503`.

## Хранилища и очереди

- PostgreSQL/PostGIS — источник истины прикладных данных;
- MinIO — бинарники и временные файлы импорта;
- RabbitMQ `object-imports` — задания import-worker;
- RabbitMQ `platform-events` — события transactional outbox;
- Redis — кэш схем и координация, но не источник истины;
- Keycloak с отдельной PostgreSQL — пользователи, пароли, realm roles.

Данные находятся в named volumes. Не используйте `down -v`, если хотите их сохранить. `postgres/pgadmin/servers.json` лишь преднастраивает подключение pgAdmin и не создаёт БД/таблицы.

## Миграции

Схема БД меняется только Alembic, приложение не вызывает `create_all()`:

```bash
docker compose -f backend/stack.yml run --rm api alembic upgrade head
docker compose -f backend/stack.yml run --rm api alembic check
```

В production миграция должна запускаться отдельной release-задачей один раз до переключения API.

## Переменные окружения

Шаблон находится в `backend/.env.example`. Секреты Keycloak, MinIO, RabbitMQ, БД и DaData не следует коммитить. Для frontend разрешённые origins задаются `CORS_ORIGINS`.

## Не реализовано в текущем объёме

In-app/email-уведомления, backup/restore, эксплуатационный мониторинг, автоматические тесты, demo-набор и серверная отчётность отложены по текущему плану.
