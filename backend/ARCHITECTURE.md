# Архитектура backend

## Текущая форма

Backend — модульный монолит FastAPI, развёрнутый отдельно от инфраструктурных контейнеров. Это не набор независимых бизнес-микросервисов: один API и общая миграционная цепочка владеют прикладной PostgreSQL. Такое устройство сохраняет транзакционность low-code ядра и одновременно фиксирует границы, по которым модули можно выделять позже.

```text
Vue SPA
  -> Keycloak (OIDC Authorization Code + PKCE)
  -> FastAPI
       api -> application -> domain -> infrastructure
       |-> PostgreSQL/PostGIS
       |-> Redis
       |-> MinIO
       |-> RabbitMQ
       |-> Keycloak Admin API
       `-> DaData/Nominatim
```

Каталог `app/modules` содержит предметные модули, а не отдельные deployable-сервисы. Размещение `entities`, `objects` или `excel` в `modules` означает изоляцию предметной области, зависимостей и API; отдельный контейнер требуется только при обоснованной необходимости независимого масштабирования или владения данными.

## Границы модулей

- `entities` владеет схемами, версиями, полями и слоями;
- `dictionaries` — справочниками enum;
- `objects` — runtime-записями, геометрией, ревизиями и динамическим API;
- `excel`, `imports`, `change_sets` — массовым обменом и атомарным применением;
- `attachments` — метаданными и версиями файлов;
- `access` — организациями и предметными правами;
- `relations` — связями схем и объектов;
- `workflows` — формами и процессами;
- `users` — адаптером Keycloak Admin API;
- `audit` — унифицированной историей действий.

Общие технические зависимости находятся в `app/core`, повторно используемые модели/утилиты — в `app/shared`. Предметная логика не должна переезжать в `core` только потому, что её вызывает несколько роутеров.

## Данные low-code

Отдельная таблица на каждую пользовательскую сущность не создаётся. Метаданные живут в `entity_schemas`, `entity_fields` и неизменяемых `entity_schema_versions`; записи — в общей `entity_objects` с `values JSONB` и `geometry(Geometry, 4326)`.

Это даёт стабильный универсальный API и быстрые изменения схемы. Типы, required, enum/reference, geometry и calculated fields проверяются по опубликованной версии. Для часто используемых полей создаются управляемые expression/trigram индексы вместо индексирования каждого JSONB-ключа.

Иерархия моделируется `parent_entity_schema_id` и `parent_object_id`. Произвольные cross-links хранятся отдельно в определениях entity relations и object relation links. Это различает композицию («корпус принадлежит школе») и ассоциацию («объект обслуживается организацией»).

## Транзакции, ревизии и история

Изменение одного объекта и `object_events` записываются одной транзакцией. `(object_id, revision)` уникален; PATCH и ChangeSet используют optimistic locking. Общий `audit_events` хранит административную и предметную историю с actor snapshot, before/after и diff.

ChangeSet группирует до 50 000 команд и применяет их атомарно после проверки. `idempotencyKey` делает retry безопасным. Excel, формы и актуализация используют этот же механизм, поэтому не создают отдельные несовместимые правила записи.

## Фоновые задачи

API не держит HTTP-соединение на длительном импорте:

1. принимает JSON-массив или XLSX;
2. создаёт `import_jobs`;
3. сохраняет временный payload в MinIO;
4. публикует команду в RabbitMQ queue `object-imports`;
5. import-worker читает пакетами, валидирует и применяет изменения;
6. обновляет прогресс и удаляет временный файл в `finally`;
7. frontend получает состояние через WebSocket либо GET fallback.

Пауза, продолжение и отмена передаются worker-у как управляемое состояние job. Redis используется для кэша и координации, но не хранит единственную копию задания.

## Transactional outbox

Бизнес-запись и `outbox_events` создаются одной PostgreSQL-транзакцией. `outbox-worker` выбирает необработанные события через блокировку, публикует их в RabbitMQ queue `platform-events` и только после подтверждения помечает отправленными. Consumer должен дедуплицировать сообщения по `eventId`, потому что доставка at-least-once допускает повтор.

В проекте не используется Redis Streams для outbox.

## Файлы

PostgreSQL хранит attachment metadata, original name, MIME, checksum, текущую и неизменяемые версии. MinIO хранит бинарники в одном bucket по UUID-пути объекта. API не раскрывает credentials MinIO; скачивание идёт через авторизованный backend. Денормализованный `attachmentPaths` ускоряет выдачу объекта, но источником истины остаются attachment tables.

## Безопасность

Общая dependency на API-router требует валидный JWT для каждого `/api/v1` маршрута. Realm role `Admin` защищает платформенные настройки, а `AuthorizationService` проверяет scoped grants для runtime операций. Скрытие кнопки на frontend не считается контролем доступа.

Секреты передаются окружением. JWT проверяется по issuer/JWKS; пароли пользователей остаются в Keycloak. Ошибки наружу имеют стабильные `code/message/requestId/details`, внутренние исключения и secrets в ответ не попадают.

## Индексы

- B-tree — идентификаторы, статусы, внешние ключи, даты;
- GIN — JSONB и поисковые read models;
- GiST — PostGIS geometry и bbox;
- `pg_trgm` — адреса, названия и object suggestions;
- unique constraints — codes, revisions, idempotency keys и связи.

Индексы меняются миграциями Alembic. Создавать index по каждому динамическому полю автоматически нельзя: это раздувает запись и требует статистики реальных запросов.

## Выделение будущих сервисов

Потенциальные deployable-границы: Identity/Access, Entity Schema, Registry, Dictionary, Import, File/Report и Geo Adapter. Выделять их следует, когда появляется отдельное масштабирование, команда-владелец или SLA. Тогда каждый сервис получает собственную схему/БД и пишет только свои таблицы; синхронизация выполняется API и событиями RabbitMQ, без распределённых транзакций и межсервисных JOIN.

До этого модульный монолит проще, надёжнее и обеспечивает атомарность операций схемы, объектов, справочников и ChangeSet.

## Развёртывание и данные

FastAPI, import-worker, outbox-worker, migrate и Redis описаны в `backend/stack.yml`; PostgreSQL/PostGIS, Keycloak с собственной БД, MinIO, RabbitMQ и Nominatim имеют отдельные проекты. Все stateful-контейнеры используют named volumes. `stack.yml` запускается Docker Compose локально; production-поставка может использовать Swarm или Kubernetes без изменения предметной архитектуры.
