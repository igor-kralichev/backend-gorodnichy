# Backend муниципальной low-code GIS-платформы

FastAPI-приложение реализует конструктор схем, универсальный CRUD объектов, справочники, PostGIS-геометрию, файлы MinIO, Excel-обмен, фоновые импорты, права, аудит и прикладные процессы. Точные контракты доступны в OpenAPI; сценарии frontend описаны в [FRONTEND_INTEGRATION.md](../docs/FRONTEND_INTEGRATION.md), типобезопасные фильтры и generated API — в [GENERATED_API_FILTERING.md](../docs/GENERATED_API_FILTERING.md), устройство модулей — в [ARCHITECTURE.md](ARCHITECTURE.md).

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

`/health/ready` возвращает `200` только когда доступны PostgreSQL, Redis,
MinIO и RabbitMQ; liveness API остаётся отдельной проверкой процесса.

Интерфейсы:

- API Swagger — <http://localhost:8000/docs>
- основной OpenAPI — <http://localhost:8000/openapi.json>
- динамический Swagger active-сущностей — <http://localhost:8000/api/v1/generated/docs>
- динамический OpenAPI — <http://localhost:8000/api/v1/openapi.json>
- каталог ресурсов — <http://localhost:8000/api/v1/generated/catalog>
- Swagger одной сущности — `/api/v1/generated/{entityCode}/docs`

## Авторизация и роли

Обычные `/api/v1` маршруты защищены JWT Keycloak; публичны только health-checks. Исключение — read-only `/api/v1/generated/{entityCode}/objects...`: они принимают JWT либо scoped `X-API-Key`. Изменение данных по API-ключу невозможно. SPA и Swagger используют Authorization Code + PKCE, realm `municipal-low-code`, public client `municipal-spa`. Backend проверяет подпись/issuer токена и получает пользователя из `sub`, ФИО из `name`, email из `email`, роли из `realm_access.roles`.

Refresh token остаётся между Keycloak и SPA: frontend хранит его только в памяти `keycloak-js` и получает новый токен при ротации. FastAPI принимает только bearer access token и не содержит `/auth/refresh`. Realm выдаёт access token на 5 минут, завершает idle session через 30 минут, ограничивает session 12 часами и запрещает повторное использование refresh token.

Realm role `Admin` в Keycloak является составной ролью и включает глобальные `permission_*`-роли, в том числе права на runtime-действия и Excel-импорт/экспорт. Код авторизации не содержит отдельного обхода для `Admin`: он одинаково проверяет глобальные realm-права и предметные permission grants. Старые `X-Actor-*` заголовки не используются.

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
- `notifications` — персональные in-app уведомления о фоновых операциях;
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
POST/PATCH/DELETE /api/v1/dictionaries/{dictionaryId}/items[/{itemId}]
POST              /api/v1/dictionaries/{dictionaryId}/excel/preview|import
```

Справочник имеет явный `scope`: `global`, `organization` или `entity`. Для
organization/entity backend проверяет владельца и доступ к сущности. Ответ
содержит `revision`; полное и атомарное изменение элементов требует актуальную
ревизию и при параллельном сохранении возвращает `409`. Excel-импорт справочника
работает как idempotent merge: новые значения добавляются, существующие и
введённые вручную не удаляются.

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

`PATCH` требует актуальную `revision` и использует optimistic locking. Запись в
`readOnly`, архивные и закрытые для ролей поля отклоняется сервером независимо
от состояния элементов формы. Поля enum принимают UUID, code или name элемента;
хранится code, а человекочитаемый текст возвращается в `displayValues`.

Фильтры приводят scalar JSONB к типу поля (`bigint`, `numeric`, `boolean`, `date`, `timestamptz`). Множественные enum/reference/file используют JSONB containment и перебор элементов массива. Один уровень связанного фильтра задаётся как `referenceField.targetField`, родитель — `parent.field`. Составная JSON-выдача доступна через `POST .../objects/queryTree`.

```text
GET  /api/v1/importJobs
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
POST /api/v1/entities/{entityCode}/excel/exportSelection
POST /api/v1/entities/{entityCode}/excel/exportJobs
GET  /api/v1/excel/exportJobs
GET  /api/v1/excel/exportJobs/{jobId}
POST /api/v1/excel/exportJobs/{jobId}/command
GET  /api/v1/excel/exportJobs/{jobId}/download
POST /api/v1/entities/{entityCode}/excel/plans
GET  /api/v1/excel/plans/{planId}
POST /api/v1/excel/plans/{planId}/decision
POST/GET /api/v1/importProfiles
DELETE /api/v1/importProfiles/{profileId}
```

Preview и import принимают `multipart/form-data` с `.xlsx` и необязательной JSON-строкой `mapping`. Preview ничего не сохраняет; import всегда создаёт фоновую задачу (`202`). Worker читает cached results формул, но не является Excel calculation engine: книгу с формулами надо предварительно пересчитать и сохранить в Excel/LibreOffice.

Ограничения по умолчанию: 30 МБ, 20 листов, 50 000 строк для синхронной
выгрузки, 500 000 строк для фоновой, 200 колонок и 200 МБ распакованных
данных. Повреждённая или подменённая ZIP-книга отклоняется как `422`. Экспорт не
обрезается молча: превышение лимита возвращается как ошибка задачи либо `422`.
Строки, начинающиеся с `+`, `-`, `=` или `@`, записываются как текст без добавления
видимого апострофа. В пользовательском экспорте нет технической строки с UUID полей.
При импорте frontend отдельно передаёт JSON-сопоставление колонок с кодами полей. Геометрии
и файлы в XLSX пока не экспортируются.

`exportSelection` использует контракт `queryTree`: выбирает объекты в режиме
`ids` либо `filter`, поддерживает `excludedIds`, колонки и фильтры отдельно для
родителя и подреестров. Родитель записывается на первый лист, каждый подреестр —
на отдельный лист с выбранными человекочитаемыми колонками родителя. Технические
UUID/revision/schemaVersion в пользовательскую книгу не добавляются.

Для большой выборки используйте `POST .../excel/exportJobs`. Он возвращает
`202` и job со статусом `queued`. Состояние читается через
`GET /excel/exportJobs/{jobId}`; после `completed` ответ содержит `downloadUrl`.
Файл скачивается только его автором через backend и хранится 24 часа. Команда
`cancel` принимается через `/command`. По завершении или ошибке создаётся
внутреннее уведомление. Колонки и фильтры фиксируются при постановке задачи,
а сами данные читаются в транзакции export-worker в момент начала выполнения.

Системная часть `mapping.system` поддерживает `fixedParentObjectId`,
`parentObjectColumn`, `objectIdColumn`, `actionColumn`, `latitudeColumn`,
`longitudeColumn` и `geometryColumn`. Координаты интерпретируются как WGS84 в
порядке долгота/широта. Excel-plan хранится 24 часа, показывает
`base/file/current`, validation errors и применяется отдельным решением через
атомарный ChangeSet; конфликт ревизии требует явного resolution.

## ChangeSet

`POST /api/v1/changeSets` создаёт атомарный набор `create/update/archive/confirm`; `GET /{id}` возвращает результаты проверки; `POST /{id}/decision` принимает `apply/reject/cancel`. Update требует `baseRevision`. При применении backend сверяет версию схемы и повторно валидирует весь набор; несовместимая схема или ревизия дают `409/422` без частичной записи. Неизменившийся update отмечается применённым, но не увеличивает ревизию и не создаёт ложное событие. `idempotencyKey` защищает повтор запроса, а тот же ключ с другим payload даёт `409`. Excel и формы используют тот же механизм вместо частичной записи строк.

## Файлы

Один bucket `municipal-attachments`; бинарники лежат по техническим UUID внутри папки объекта, PostgreSQL хранит метаданные и версии. Пользователю возвращается оригинальное имя. Фото принимают изображения, документы — PDF/DOC/DOCX; лимит по умолчанию 50 МБ.

Маршруты attachments поддерживают upload/list/read/download/update metadata/replace file/versions/delete. При полном удалении объекта удаляются и бинарники MinIO.

## Поиск и геокодирование

`GET /api/v1/search/objectSuggestions` использует read-model и `pg_trgm`/GIN индексы. `GET /api/v1/geocoding/addressSuggestions` проксирует `source=dadata|nominatim`, не раскрывая ключ DaData браузеру. `GET /api/v1/geocoding/reverse` возвращает адрес точки, `/geocoding/buildings` — ограниченную bbox-выборку контуров зданий с provider/attribution. Региональный Nominatim работает отдельным контейнером.

## Черновики, возможности, уведомления и история

- `GET /api/v1/me/capabilities` возвращает эффективные actions и fieldActions;
- active-схема изменяется через `GET/PATCH /entities/{identifier}/draft`, затем
  `/draft/validation` и `/draft/publish` с optimistic `expectedVersion`;
- `GET /api/v1/notifications`, `PATCH /notifications/{id}` и
  `POST /notifications/readAll` работают только с уведомлениями текущего
  пользователя;
- `GET /api/v1/audit/resources/{resourceType}/{resourceId}` отдаёт историю после
  предметной проверки read-доступа и скрывает недоступные динамические поля.

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
- RabbitMQ `excel-exports` — задания export-worker;
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

Email-уведомления, backup/restore, эксплуатационный мониторинг, автоматические
тесты, demo-набор и серверная отчётность отложены по текущему плану. In-app
уведомления о фоновых импортах реализованы.
