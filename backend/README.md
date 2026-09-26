# Backend муниципальной low-code GIS-платформы

Backend реализует полный жизненный цикл схем сущностей, универсальный CRUD их объектов, материализованные слои карты, авторизацию через Keycloak и динамическую OpenAPI-документацию. Он запускается в Docker и использует PostgreSQL/PostGIS, Redis, RabbitMQ, Alembic, Keycloak, MinIO и опциональный pgAdmin.

Подробные границы будущих микросервисов, владение данными, outbox и схема runtime API описаны в [ARCHITECTURE.md](ARCHITECTURE.md).

## Запуск

```bash
docker compose -f postgres/stack.yml up --build -d
docker compose -f keycloak/stack.yml up --build -d
docker compose -f minio/stack.yml up --build -d
docker compose -f nominatim/stack.yml up --build -d
docker compose -f rabbitmq/stack.yml up --build -d
docker compose -f backend/stack.yml up --build -d
```

Порядок важен: `postgres/stack.yml` создаёт общую Docker-сеть `municipal_low_code_network`, а остальные проекты подключаются к ней как к внешней сети.

После запуска:

- Swagger UI: `http://localhost:8000/docs`
- OpenAPI: `http://localhost:8000/openapi.json`
- Swagger для опубликованных low-code сущностей: `http://localhost:8000/api/v1/generated/docs`
- каталог сгенерированных ресурсов: `http://localhost:8000/api/v1/generated/catalog`
- Keycloak: `http://localhost:8080`
- MinIO API: `http://localhost:9000`
- MinIO Console: `http://localhost:9001`
- Nominatim: `http://localhost:8081`
- RabbitMQ Management: `http://localhost:15672`
- pgAdmin, если запущен профиль `tools`: `http://localhost:5050`
- liveness: `http://localhost:8000/health/live`
- readiness PostgreSQL + Redis: `http://localhost:8000/health/ready`

Все маршруты под `/api/v1` требуют JWT Keycloak. Дополнительная проверка роли `Admin` включена только для административных операций, где она явно нужна. Открытыми остаются только технические `/health/live` и `/health/ready`.

Локальный вход в Keycloak admin console: `admin` / `admin`. Это администратор realm `master`, а не пользователь приложения. Realm приложения называется `municipal-low-code`. Файл `keycloak/import/municipal-low-code-realm.json` нужен только для первичной настройки realm: роли `Admin` и `User`, OAuth2 clients для API/Swagger/SPA и service account backend. Обычные пользователи в этот файл не добавляются.

Локальный вход в pgAdmin: `admin@local.dev` / `admin`. Для запуска:

```bash
docker compose -f postgres/stack.yml --profile tools up -d pgadmin
```

Файл `postgres/pgadmin/servers.json` — только настройка автодобавления сервера в интерфейс pgAdmin. Он не создаёт таблицы и не хранит данные приложения. При первом подключении используйте пароль `lowcode`. Внутри Docker имя хоста прикладной БД — `postgres`, с компьютера — `localhost:5432`.

Для просмотра логов:

```bash
docker compose -f backend/stack.yml logs -f api import-worker migrate
```

Обычная пересборка не удаляет данные:

```bash
docker compose -f backend/stack.yml up --build -d
```

Локальный вход в MinIO Console: `lowcode` / `lowcode-secret`. Для файлов используется один bucket `municipal-attachments`.

Локальный вход в RabbitMQ Management: `lowcode` / `lowcode`. Очередь фонового импорта объектов называется `object-imports`.

Для DaData задайте переменные окружения `DADATA_API_KEY` и `DADATA_SECRET_KEY` в локальном `.env` или в CI/CD secrets. Значения ключей не должны храниться в репозитории.

Прикладная PostgreSQL/PostGIS хранится в именованном volume `municipal_low_code_postgres_data`. Keycloak использует свою отдельную PostgreSQL в проекте `keycloak/` и отдельный volume `municipal_low_code_keycloak_postgres_data`. Redis хранится в `municipal_low_code_redis_data`, pgAdmin — в `municipal_low_code_pgadmin_data`, MinIO — в `municipal_low_code_minio_data`, Nominatim — в `municipal_low_code_nominatim_data` и `municipal_low_code_nominatim_flatnode`, RabbitMQ — в `municipal_low_code_rabbitmq_data`. Не используйте `docker compose down -v`, если данные требуется сохранить. Команда с `-v` удаляет volumes намеренно.

Каждый инфраструктурный контур лежит в своей папке и имеет собственные `Dockerfile` и `stack.yml`: `backend/`, `postgres/`, `keycloak/`, `minio/`, `nominatim/`, `rabbitmq/`. PostGIS-образ берёт multi-arch `postgres:17-bookworm` и устанавливает пакеты PostGIS, поэтому на Apple Silicon база запускается нативно как `arm64`, без QEMU и предупреждения `image may have poor performance`. API, import-worker, прикладная БД, Keycloak, БД Keycloak, MinIO, Nominatim и RabbitMQ остаются разными контейнерами с разным жизненным циклом.


## Создание сущности

`POST /api/v1/entities` создаёт черновик сущности атомарно: схему, поля, настройки геометрий, стили карты, версию №1 и событие transactional outbox. Если `includeAddress=true` и адрес не передан в `fields`, системное поле адреса добавляется автоматически первым.

Создание и настройка сущностей доступны только пользователю с realm role `Admin`. Старые заголовки `X-Actor-Id` и `X-Actor-Role` не используются: backend берёт пользователя и роли только из JWT Keycloak.

Для вложенных сценариев передайте `parentEntityId`: например, сущность «Корпуса» может быть дочерней к сущности «Школы», а сущность «Помещения» — дочерней к «Корпусам». Это универсальная иерархия, без отдельных таблиц под конкретную предметную область.

Для работы через Swagger создайте пользователя в realm `municipal-low-code`, назначьте ему realm role `Admin`, затем нажмите `Authorize` в Swagger UI. PKCE-flow должен открыть страницу входа Keycloak, после успешного входа он вернёт пользователя обратно в Swagger. Логин `admin` / `admin` подходит только для входа в Keycloak admin console в realm `master`, для Swagger он не подходит, пока вы отдельно не создали такого пользователя в realm `municipal-low-code`.

```bash
curl -X POST http://localhost:8000/api/v1/entities \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer ACCESS_TOKEN' \
  -d '{
    "name": "Учебные заведения",
    "description": "Школы, СПО и вузы",
    "geometryType": "point",
    "includeAddress": true,
    "fields": [
      {
        "name": "Название",
        "type": "string",
        "required": true,
        "listVisible": true,
        "cardVisible": true,
        "searchable": true,
        "filterable": true
      },
      {
        "name": "Количество учащихся",
        "type": "integer",
        "listVisible": true,
        "cardVisible": true,
        "searchable": true,
        "filterable": true
      }
    ],
    "mapSettings": {
      "enabledGeometryTypes": ["point", "polygon"],
      "clusteringEnabled": true,
      "styles": {},
      "colorRules": []
    }
  }'
```

Код сущности и коды полей генерируются из названий. Две сущности могут иметь одинаковый `name`, но не одинаковый `code`: при совпадении автоматически созданного кода добавляется суффикс `_2`, `_3` и так далее. Например, повторное название «Учебные заведения» получит код `uchebnye_zavedeniia_2`. Явно переданный занятый `code` возвращает HTTP 409.

Не передавайте фиктивные UUID из Swagger:

- `enumId` разрешён и обязателен только для поля `type: "enum"`;
- `referenceEntityId` разрешён и обязателен только для `type: "reference"`;
- `scopeMunicipalityId` передаётся только для уже существующего муниципалитета;
- ключи `styles` — только `point`, `lineString`, `polygon`;
- для сущности без карты используйте `geometryType: "none"` и `enabledGeometryTypes: []`.

## Управление схемой сущности

Доступны следующие операции:

```text
POST   /api/v1/entities                         создать черновик
GET    /api/v1/entities                         получить список
GET    /api/v1/entities/{id-or-code}            получить схему
PATCH  /api/v1/entities/{id-or-code}            сохранить изменения
POST   /api/v1/entities/{id-or-code}/archive    переместить в архив
POST   /api/v1/entities/{id-or-code}/restore    восстановить как черновик
DELETE /api/v1/entities/{id-or-code}            полностью удалить
POST   /api/v1/entities/{id-or-code}/duplicate  скопировать без объектов
POST   /api/v1/entities/{id-or-code}/publish    опубликовать
```

Архивация является безопасным удалением: связанные объекты, история и настройки сохраняются, слой отключается, а сущность исчезает из рабочего списка и runtime API. Восстановление возвращает её в `draft`; после проверки её нужно опубликовать повторно.

Черновик надо опубликовать, прежде чем станет доступен CRUD объектов:

```bash
curl -X POST http://localhost:8000/api/v1/entities/ENTITY_UUID_OR_CODE/publish \
  -H 'Authorization: Bearer ACCESS_TOKEN'
```

При первой публикации сущности с геометрией создаётся запись `entity_layers` с полным стилем, `opacity`, `selectable` и `visibleByDefault`. Изменение карты синхронизирует слой, архивирование выключает его. Каждое изменение схемы сохраняет неизменяемый снимок в `entity_schema_versions`.

После появления объектов нельзя удалять используемые поля, менять их типы или сужать набор разрешённых геометрий без отдельной миграции данных. API возвращает HTTP 409 вместо повреждения существующих записей.

## Справочники

Справочники используются полями типа `enum`. Создание, изменение, архивирование, восстановление и удаление доступны только роли `Admin`:

```text
GET    /api/v1/dictionaries                         получить список
POST   /api/v1/dictionaries                         создать справочник
GET    /api/v1/dictionaries/{dictionaryId}          получить справочник
PATCH  /api/v1/dictionaries/{dictionaryId}          изменить справочник
POST   /api/v1/dictionaries/{dictionaryId}/archive  переместить в архив
POST   /api/v1/dictionaries/{dictionaryId}/restore  восстановить из архива
DELETE /api/v1/dictionaries/{dictionaryId}          удалить справочник
```

Архивирование делает справочник недоступным для новых значений, но не удаляет его из базы. Физическое удаление отклоняется, если справочник уже используется полем сущности.

## Пользователи

Backend не хранит пароли и пользователей в своей базе. Все операции выполняются через Keycloak Admin API и доступны только роли `Admin`:

```text
GET    /api/v1/users                         список пользователей с UUID, ФИО и ролями
POST   /api/v1/users                         создать пользователя
POST   /api/v1/users/{userId}/activate       активировать пользователя
POST   /api/v1/users/{userId}/deactivate     деактивировать пользователя
POST   /api/v1/users/{userId}/resetPassword  сбросить пароль
DELETE /api/v1/users/{userId}                удалить пользователя
```

## История изменений

Все изменяющие операции по сущностям, объектам, справочникам, пользователям и файлам пишутся в `audit_events`. Событие хранит тип ресурса, id/code/name ресурса, действие, старое и новое состояние, список изменённых полей, UUID пользователя Keycloak, его ФИО и email на момент изменения.

```text
GET /api/v1/audit/events
```

Основные фильтры:

```text
resourceType=entity_schema | entity_object | dictionary | user | attachment
resourceId=UUID
actorId=UUID
action=entity_object.updated
limit=50
offset=0
```

## Универсальный API объектов

После публикации сущности с кодом `shkoly` сразу доступны маршруты:

```text
GET    /api/v1/entities/shkoly/objects
POST   /api/v1/entities/shkoly/objects              загрузить .json файл с одним или несколькими объектами
POST   /api/v1/entities/shkoly/objects/search
GET    /api/v1/entities/shkoly/objects/{objectId}
PATCH  /api/v1/entities/shkoly/objects/{objectId}
POST   /api/v1/entities/shkoly/objects/{objectId}/archive
DELETE /api/v1/entities/shkoly/objects/{objectId}
POST   /api/v1/entities/shkoly/objects/{objectId}/restore
```

Новые Python-файлы и таблицы на каждую сущность не создаются. Маршруты исполняет один безопасный runtime-контроллер, который загружает только опубликованную схему, проверяет поля, геометрию, значения справочников и ссылки, сохраняет значения в JSONB/PostGIS и пишет ревизию в `object_events`. При этом `/api/v1/generated/docs` строит отдельное представление Swagger с названиями и полями всех опубликованных сущностей, а `/api/v1/generated/{entityCode}/docs` показывает Swagger только одной выбранной сущности. Черновики и архивные сущности туда не попадают.

`POST /api/v1/entities/{entityCode}/objects` принимает `multipart/form-data` с `.json` файлом. Внутри файла всегда должен быть JSON-массив: для одного объекта передайте массив из одного элемента, для массовой загрузки — массив из многих элементов. Если сущность дочерняя, у каждого создаваемого объекта нужно передать `parentObjectId`; для корневой сущности это поле запрещено. Список дочерних объектов можно получить через `GET /api/v1/entities/{entityCode}/objects?parentObjectId=<uuid>`.

Небольшие файлы backend обрабатывает сразу и возвращает `201` с созданными объектами. Если количество строк больше `IMPORT_ASYNC_THRESHOLD`, тот же endpoint временно кладёт JSON в MinIO, создаёт запись `import_jobs`, публикует сообщение в RabbitMQ и возвращает `202` с `jobId`. После успеха, ошибки или отмены import-worker удаляет временный файл `imports/<jobId>.json` из MinIO.

Для полей типа `enum` объект может принять `id`, `code` или название элемента справочника. Перед сохранением значение нормализуется к `dictionaryItem.code`, а в ответе объекта дополнительно возвращается `displayValues` с человекочитаемыми названиями.

Объект также возвращает `attachmentPaths` — массив технических ключей файлов в MinIO. Источником истины по файлам остаётся таблица `attachments`, а `attachmentPaths` хранится в объекте как быстрый список путей для выдачи и синхронизируется при загрузке и удалении файлов.

```text
GET /api/v1/generated/catalog
GET /api/v1/openapi.json
GET /api/v1/generated/docs
GET /api/v1/generated/catalog/{entityCode}
GET /api/v1/generated/{entityCode}/openapi.json
GET /api/v1/generated/{entityCode}/docs
```

Пример файла `objects.json`:

```json
[
  {
    "values": {
      "address": "г. Нижний Новгород, пр. Кирова, д. 29а",
      "nazvanie": "МАОУ Лицей № 36",
      "kolichestvo_uchashchikhsya": 724
    },
    "geometry": {
      "type": "Point",
      "coordinates": [43.87, 56.24]
    }
  }
]
```

Пример загрузки:

```bash
curl -X POST http://localhost:8000/api/v1/entities/shkoly/objects \
  -H 'Authorization: Bearer ACCESS_TOKEN' \
  -F 'file=@objects.json;type=application/json'
```

Если ответ `202`, состояние фоновой задачи можно смотреть так:

```text
GET  /api/v1/importJobs/{jobId}
POST /api/v1/importJobs/{jobId}/command
```

Команды управления:

```json
{"command": "pause"}
{"command": "resume"}
{"command": "cancel"}
```

Для live-прогресса и интерактивных команд доступен WebSocket:

```text
ws://localhost:8000/api/v1/importJobs/{jobId}/ws?token=ACCESS_TOKEN
```

Сервер отправляет текущее состояние задачи, а клиент может отправлять команды в том же формате:

```json
{"command": "pause"}
```

REST-команды создания/изменения объектов остаются HTTP. WebSocket используется именно там, где он полезен: live-прогресс, пауза, продолжение и отмена долгой фоновой загрузки.

## Подсказки и геокодирование

Подсказки по уже сохранённым объектам работают через отдельный read-model `object_search_index`. Индекс хранит `entityId`, `objectId`, `fieldCode`, исходное значение и нормализованное значение. По `normalizedValue` создан trigram GIN-индекс `pg_trgm`, поэтому поиск по фрагменту адреса или названия остаётся быстрым без перебора `values JSONB`.

```text
GET /api/v1/search/objectSuggestions?q=кирова&entityCode=shkoly&fieldCode=address
```

Индекс обновляется в транзакции создания, изменения, восстановления и удаления объекта. В выдачу попадают только объекты активных сущностей, которые не находятся в архиве.

Адресные подсказки идут через backend, чтобы не раскрывать ключ DaData в браузере и централизованно контролировать лимиты:

```text
GET /api/v1/geocoding/addressSuggestions?q=нижний новгород кирова 29&source=dadata
GET /api/v1/geocoding/addressSuggestions?q=нижний новгород кирова 29&source=nominatim
```

Для DaData задаются `DADATA_API_KEY` и `DADATA_SECRET_KEY`. Для Nominatim используется отдельный контейнер `nominatim/`; по умолчанию импортируется extract Приволжского федерального округа, а backend дополнительно ограничивает поиск bbox Нижегородской области.

## Файлы объектов

Файлы объектов хранятся в MinIO, а PostgreSQL хранит метаданные: UUID файла, UUID объекта, тип файла, оригинальное имя, технический ключ, MIME-тип, размер, checksum и автора загрузки. Bucket один — `municipal-attachments`, внутри него файлы лежат в папках объектов:

```text
<object_uuid>/<attachment_uuid>
```

Оригинальное имя файла не используется в ключе хранения, поэтому кириллица, пробелы и совпадения имён не ломают storage. Пользователю в API отдаётся `originalName`, а скачивание идёт через backend:

```text
GET    /api/v1/entities/{entityCode}/objects/{objectId}/attachments
POST   /api/v1/entities/{entityCode}/objects/{objectId}/attachments?kind=photo
POST   /api/v1/entities/{entityCode}/objects/{objectId}/attachments?kind=document
GET    /api/v1/entities/{entityCode}/objects/{objectId}/attachments/{attachmentId}
GET    /api/v1/entities/{entityCode}/objects/{objectId}/attachments/{attachmentId}/download
PATCH  /api/v1/entities/{entityCode}/objects/{objectId}/attachments/{attachmentId}
PUT    /api/v1/entities/{entityCode}/objects/{objectId}/attachments/{attachmentId}/file
DELETE /api/v1/entities/{entityCode}/objects/{objectId}/attachments/{attachmentId}
```

Для `photo` разрешены изображения по MIME-типу или расширению. Для `document` разрешены PDF, DOC и DOCX. При полном удалении объекта backend удаляет связанные бинарники из MinIO и затем очищает метаданные.

## Границы архитектуры

Сейчас это модульный сервис с одним процессом FastAPI. Внутри соблюдены границы `api → application → domain → infrastructure`, поэтому контуры можно выделять постепенно:

- schema service — схемы сущностей, поля, версии, публикация;
- registry service — объекты, проверка данных, история изменений;
- dictionary service — справочники и сопоставление импорта;
- import worker — чтение файлов, геокодирование, пакетная запись;
- file/report service — S3/MinIO, фото, документы и отчёты;
- identity service — пользователи, организации, роли и права.

PostgreSQL в локальной среде один, но сервис-владелец должен быть единственным писателем своих таблиц. Для межсервисного обмена уже есть `outbox_events`: бизнес-запись и событие создаются в одной транзакции. Redis используется только как кэш и координационная инфраструктура, а не как источник истины.

## Схема данных

Первая миграция создаёт:

- `entity_schemas`, `entity_schema_versions`, `entity_fields`;
- `entity_allowed_geometry_types`, `entity_map_styles`, `entity_map_color_rules`, `entity_layers`;
- `dictionaries`, `dictionary_items`;
- `entity_objects` с `values JSONB` и `geometry(Geometry, 4326)`;
- `object_events` с последовательной ревизией объекта;
- `attachments`, `import_jobs`, `import_rows`;
- `municipalities`, `outbox_events`.

Для поиска созданы B-tree, GIN (`values`) и GiST (`geometry`) индексы. Схема меняется только миграциями Alembic; приложение не вызывает `create_all()` при старте.

## Новая версия

При изменении ORM-моделей создайте отдельную миграцию и проверьте SQL перед применением:

```bash
docker compose -f backend/stack.yml run --rm api alembic revision --autogenerate -m "describe change"
docker compose -f backend/stack.yml run --rm api alembic upgrade head
```

В production миграция должна выполняться отдельной release-задачей ровно один раз, а не каждой репликой API.

## Docker Compose и Stack

В репозитории используются файлы `stack.yml`, но запуск локально остаётся через Docker Compose: `docker compose -f <project>/stack.yml ...`. Это удобно как промежуточный формат: папки уже разделены по контурам, а later production-вариант можно дополнить секцией `deploy`, registry image, Traefik и CI/CD переменными для Docker Swarm или другого оркестратора.

`backend/stack.yml` намеренно содержит FastAPI, import-worker, миграционную задачу и Redis. Прикладная PostgreSQL/PostGIS находится в `postgres/`, Keycloak и его собственная БД — в `keycloak/`, MinIO — в `minio/`, RabbitMQ — в `rabbitmq/`.

## Проверка реализации

```bash
docker compose -f backend/stack.yml run --rm api alembic check
docker compose -f backend/stack.yml ps
```

Тестовые сценарии в проекте не хранятся.
