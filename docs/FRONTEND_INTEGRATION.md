# Интеграция frontend с API муниципальной low-code платформы

Документ дополняет `openapi.json`: OpenAPI является источником истины по точным телам запросов и ответов, а здесь описаны последовательности вызовов, состояния интерфейса и связи между модулями.

## 1. Общие правила клиента

- Base URL локально: `http://localhost:8000`.
- Все прикладные маршруты начинаются с `/api/v1` и требуют `Authorization: Bearer <accessToken>`.
- JSON-поля и параметры используют `camelCase`; уже существующие URL переименовывать не требуется.
- Даты приходят в ISO 8601. Форматировать их для пользователя следует на frontend в выбранной временной зоне.
- GeoJSON хранится в WGS 84 (`EPSG:4326`), координаты передаются в порядке `[longitude, latitude]`.
- Для трассировки frontend может отправлять `X-Request-ID`; backend возвращает его в одноимённом заголовке и в теле ошибки.
- При `401` клиент обновляет токен или отправляет пользователя на вход. `403` означает, что пользователь опознан, но не имеет права на операцию.

Единый контракт ошибки:

```json
{
  "code": "validation_error",
  "message": "Параметры запроса не прошли проверку",
  "requestId": "d6fd00d7-6e72-4f99-a8c0-f6405df20f5d",
  "details": []
}
```

Ключевые коды HTTP: `401` — токен отсутствует или недействителен; `403` — недостаточно прав; `404` — ресурс или опубликованная схема не найдены; `409` — конфликт кода, ревизии или связи; `413` — слишком большой файл; `422` — ошибка контракта/данных; `502` — ошибка внешнего провайдера.

## 2. Вход через Keycloak

SPA использует Authorization Code Flow с PKCE:

1. Перенаправить пользователя на realm `municipal-low-code`, client `municipal-spa`.
2. После callback обменять authorization code на access/refresh token средствами OIDC-библиотеки.
3. Передавать access token во всех `/api/v1` запросах.
4. Роли читать из `realm_access.roles`; backend всё равно повторно проверяет права.
5. Временный пароль Keycloak потребует сменить при следующем входе автоматически.

Не используйте `X-Actor-Id` и `X-Actor-Role`: пользователь, ФИО, email и роли берутся только из JWT.

### Refresh token

Refresh token полностью обслуживается Keycloak и frontend OIDC-клиентом. FastAPI принимает только access token и не предоставляет собственную ручку refresh.

Для Vue SPA рекомендуется официальный пакет `keycloak-js`:

```ts
import Keycloak from 'keycloak-js'

export const keycloak = new Keycloak({
  url: 'http://localhost:8080',
  realm: 'municipal-low-code',
  clientId: 'municipal-spa',
})

await keycloak.init({
  onLoad: 'login-required',
  pkceMethod: 'S256',
})
```

Access, ID и refresh token должны оставаться только в памяти адаптера. Не сохраняйте их в `localStorage`, `sessionStorage`, IndexedDB, persisted Pinia или cookie, доступной JavaScript. После обновления страницы новый набор токенов получается через активную SSO-сессию Keycloak.

Realm настроен на следующие интервалы:

- access token действует 5 минут;
- idle timeout пользовательской/client session — 30 минут;
- максимальная продолжительность session — 12 часов;
- `Revoke Refresh Token` включён, повторное применение использованного refresh token запрещено.

Перед API-запросом вызывайте `updateToken(30)`. Параллельные запросы должны ожидать один общий refresh Promise: из-за ротации два одновременных refresh-запроса с одним токеном приведут к отклонению второго.

```ts
let refreshPromise: Promise<boolean> | null = null

export async function getAccessToken(): Promise<string> {
  refreshPromise ??= keycloak.updateToken(30).finally(() => {
    refreshPromise = null
  })

  try {
    await refreshPromise
  } catch {
    keycloak.clearToken()
    await keycloak.login()
    throw new Error('Сессия завершена')
  }

  if (!keycloak.token) throw new Error('Access token отсутствует')
  return keycloak.token
}
```

При первом `401` допустимо один раз вызвать `updateToken(-1)` и повторить исходный запрос. Повторный `401` завершает локальную сессию и запускает вход; бесконечный retry запрещён. При `onAuthRefreshError` используется тот же сценарий.

WebSocket импорта открывается с актуальным access token. После его истечения соединение следует переподключить с новым access token; refresh token в URL или WebSocket-сообщениях не передаётся.

## 3. Старт приложения

После авторизации рекомендуемый bootstrap:

1. `GET /api/v1/entities` — получить доступные схемы для административного интерфейса.
2. `GET /api/v1/generated/catalog` — получить опубликованные runtime-ресурсы и их поля.
3. `GET /api/v1/organizations` — получить организационный контекст пользователя.
4. При необходимости `GET /api/v1/saved-views` — восстановить пользовательские фильтры и колонки.

Не кэшируйте динамическую схему навсегда: после публикации новой версии перечитайте сущность/каталог и перестройте форму.

## 4. Сущности и публикация схемы

Административный флоу:

1. `POST /api/v1/entities` создаёт `draft`.
2. `PATCH /api/v1/entities/{identifier}` меняет поля и настройки карты.
3. Для `enum` сначала создайте справочник, затем укажите его `enumId`. Для `reference` укажите `referenceEntityId`.
4. `POST /api/v1/entities/{identifier}/publish` переводит схему в `active` и создаёт/синхронизирует слой карты.
5. После публикации перечитайте `GET /api/v1/generated/catalog/{entityCode}`.

Поддерживаются поля `string`, `text`, `integer`, `decimal`, `boolean`, `date`, `datetime`, `address`, `enum`, `reference`, `file`, `phone`, `email`, `url`, `calculated`. Для вычисляемого поля формула обязательна, а значение доступно только для чтения.

Операции: список/создание — `/entities`; чтение, изменение и полное удаление — `/entities/{identifier}`; действия — `/archive`, `/restore`, `/duplicate`, `/publish`. Восстановленная сущность становится черновиком и требует повторной публикации. Копия получает новый id/code, статус `draft`, схему и настройки, но не объекты.

Если `includeAddress=true`, backend добавит системное поле адреса, если его нет. Для иерархии «школа → корпус → помещение» дочерняя схема содержит `parentEntityId`.

## 5. Справочники

Маршруты `/api/v1/dictionaries` поддерживают список, создание, чтение, изменение, архив, восстановление и полное удаление. Создание/изменение доступны роли `Admin`.

Поле объекта типа `enum` может получить UUID, `code` или название элемента. Backend сопоставляет значение со справочником, сохраняет стабильный `dictionaryItem.code`, а в `displayValues` возвращает название для UI. Пробелы по краям удаляются; регистр отображаемого названия не меняется.

Для select-компонента используйте `items[].code` как value и `items[].name` как label. Архивные справочники по умолчанию не возвращаются; для страницы архива передайте `includeArchived=true`.

## 6. Объекты: единый CRUD

Новые маршруты физически не генерируются на каждую сущность. Один runtime-контроллер обслуживает опубликованные схемы:

```text
GET    /api/v1/entities/{entityCode}/objects
POST   /api/v1/entities/{entityCode}/objects
POST   /api/v1/entities/{entityCode}/objects/search
GET    /api/v1/entities/{entityCode}/objects/clusters
GET    /api/v1/entities/{entityCode}/objects/{objectId}
PATCH  /api/v1/entities/{entityCode}/objects/{objectId}
POST   /api/v1/entities/{entityCode}/objects/{objectId}/copy
POST   /api/v1/entities/{entityCode}/objects/{objectId}/archive
POST   /api/v1/entities/{entityCode}/objects/{objectId}/restore
DELETE /api/v1/entities/{entityCode}/objects/{objectId}
```

### Создание одного или многих объектов

`POST .../objects` всегда принимает JSON-массив, в том числе для одного объекта:

```json
[
  {
    "values": {
      "address": "г. Нижний Новгород, пр. Кирова, д. 29а",
      "nazvanie": "МАОУ Лицей № 36",
      "kolichestvo_uchashchikhsya": 724
    },
    "parentObjectId": null,
    "ownerOrganizationId": null,
    "responsibleId": null,
    "geometry": {
      "type": "Point",
      "coordinates": [43.87, 56.24]
    }
  }
]
```

- До `IMPORT_ASYNC_THRESHOLD` элементов запрос обрабатывается синхронно и возвращает `201`.
- Больший массив сериализуется backend во временный JSON в MinIO, ставится в RabbitMQ и возвращает `202` с объектом job.
- Frontend не загружает `.json` файл в эту ручку.
- Временный файл удаляется worker-ом при успехе, ошибке или отмене.

Объект с валидными обязательными полями автоматически получает опубликованный статус. Неполный объект сохраняется черновиком с `dataQuality` и `validationErrors`; UI должен подсветить ошибки рядом с соответствующими `fieldCode`.

Для дочерней сущности `parentObjectId` обязателен, для корневой запрещён. Фильтрация по родителю: `?parentObjectId=<uuid>`.

### Изменение и конфликт редактирования

Передавайте в `PATCH` последнюю `revision`. При `409` не перезаписывайте данные молча: перечитайте карточку, покажите различия и предложите повторить изменение. `values` в PATCH содержит только изменяемые динамические поля.

### Геометрия

Допустимы `Point`, `MultiPoint`, `LineString`, `MultiLineString`, `Polygon`, `MultiPolygon`, `GeometryCollection`. Для коллекции используйте `geometries`, для остальных типов — `coordinates`.

### Фильтрация и карта

Для простого списка применяйте query-параметры `filters[fieldCode][operator]=value`, `logic=and|or`, `sort=fieldCode|-fieldCode`, `limit`, `offset`, `parentObjectId`, `bbox=minLon,minLat,maxLon,maxLat`. Для сложного фильтра используйте `POST .../objects/search`.

Операторы: `equals`, `notEquals`, `contains`, `startsWith`, `endsWith`, `greaterThan`, `greaterOrEqual`, `lessThan`, `lessOrEqual`, `in`, `notIn`, `filled`, `empty`, `today`, `beforeToday`, `afterToday`.

Для карты на небольшом масштабе используйте `/clusters` с `bbox` и `zoom`; после приближения запрашивайте обычные объекты по bbox.

## 7. Фоновые импорты

Job имеет статусы `queued`, `running`, `paused`, `completed`, `failed`, `cancelled` и счётчики `totalRows`, `processedRows`, `errorRows`.

```text
GET  /api/v1/importJobs/{jobId}
POST /api/v1/importJobs/{jobId}/command   {"command":"pause|resume|cancel"}
WS   /api/v1/importJobs/{jobId}/ws?token=ACCESS_TOKEN
```

Рекомендуемый UI: сразу показать job в центре задач, открыть WebSocket и обновлять прогресс из сообщений; при разрыве соединения продолжить polling GET. Команды WebSocket/HTTP должны отображаться только пока допустимы текущим статусом. Просматривать job может только его инициатор.

## 8. Excel

Все Excel-ручки работают только с `.xlsx` без макросов.

### Предпросмотр

`POST /api/v1/entities/{entityCode}/excel/preview` — `multipart/form-data`:

- `file` — XLSX;
- `mapping` — необязательная JSON-строка `{"sheetName":"Лист1","headerRow":1,"columns":{"Название":"nazvanie"}}`;
- `previewRows` — 1–100.

Ответ содержит листы, заголовки, предложенное сопоставление, число строк, preview и ошибки ячеек. Записи в БД не создаются.

### Импорт

`POST /api/v1/entities/{entityCode}/excel/import` принимает `file` и подтверждённый `mapping`, всегда возвращает `202` и job. XLSX временно хранится в MinIO, обрабатывается import-worker через RabbitMQ, проходит через ChangeSet и удаляется после завершения.

Формулы backend не вычисляет: он читает сохранённый Excel cached value. Если workbook не был пересчитан Excel/LibreOffice и cached value отсутствует, строка получает ошибку `formula_result_missing`.

Ограничения по умолчанию: 30 МБ, 20 листов, 50 000 строк, 200 колонок и 200 МБ распакованного содержимого.

### Экспорт и повторный импорт

`GET /api/v1/entities/{entityCode}/excel/export` скачивает XLSX. В первой технической строке находятся `__objectId`, `__revision`, `__schemaVersionId`, `__action` и UUID полей, во второй — пользовательские заголовки, данные начинаются с третьей строки. Не удаляйте техническую строку при повторной загрузке: она нужна для изменения существующих объектов и защиты от конфликтов.

Геометрии и бинарные файлы в текущий XLSX-экспорт не входят.

### Профили импорта

`POST/GET /api/v1/importProfiles` и `DELETE /api/v1/importProfiles/{profileId}` сохраняют сопоставление колонок для конкретного пользователя и сущности.

## 9. ChangeSet

ChangeSet — атомарная группа команд, а не отдельная копия объекта. Он нужен для Excel, форм, актуализации и пакетного редактирования UI:

1. `POST /api/v1/changeSets` с `entityCode`, `source`, необязательным `idempotencyKey` и массивом `items`.
2. `GET /api/v1/changeSets/{changeSetId}` показывает проверку каждой строки.
3. `POST /api/v1/changeSets/{changeSetId}/decision` с `apply`, `reject` или `cancel`.

Операции элемента: `create`, `update`, `archive`, `confirm`. Для update обязательны `objectId` и `baseRevision`. Применение атомарно: конфликт не должен оставлять частично записанный пакет. Повтор того же `idempotencyKey` безопасен; другой payload с тем же ключом возвращает `409`.

## 10. Файлы объекта

Маршруты находятся под `/entities/{entityCode}/objects/{objectId}/attachments`. Поддерживаются список, upload, карточка, download, изменение метаданных, замена файла, версии и удаление.

- Upload — multipart `file` и query `kind=photo|document`.
- Фото ограничены изображениями; документы — PDF, DOC, DOCX.
- Пользователю показывайте `originalName`, не технический MinIO key.
- Замена создаёт новую неизменяемую версию под тем же attachment id.
- Скачивание выполняйте через backend `/download`, прямой доступ к bucket не требуется.

## 11. Организации, права и представления

- `/organizations` — иерархические организации.
- `/memberships` — членство пользователя и роль в организации.
- `/permission-grants` — права пользователя или realm role на организацию, сущность, поле или объект.
- `/saved-views` — сохранённые фильтры/колонки текущего пользователя.

Действия прав: `read`, `create`, `update`, `archive`, `delete`, `manage_schema`, `manage_access`, `export`, `import`, `request`, `review`, `confirm`. Не скрывайте данные только на клиенте: frontend использует права для UX, окончательное решение всегда принимает backend.

## 12. Связи и вложенность

Композиция хранится через `parentEntityId`/`parentObjectId`. Для произвольных перекрёстных связей используйте:

- `/entityRelations` — Admin задаёт тип связи схем;
- `/objectRelations` — создаётся связь конкретных объектов и её значения.

При обновлении object relation передавайте `revision`; конфликт возвращает `409`.

## 13. Формы и процессы

### Формы

`/forms`: создать, получить, изменить, опубликовать, архивировать, восстановить. Опубликованная версия фиксируется и используется процессами; не редактируйте уже отправленный payload по новой версии формы.

### Сбор и актуализация

1. Создать `/informationRequests` с `requestType=collection|actualization`, сущностью, формой, периодом, сроком, получателями, объектами и полями.
2. Действием `/informationRequests/{requestId}/action` открыть, закрыть или отменить запрос.
3. Получатель сохраняет/отправляет `/requestRecipients/{recipientId}/submissions`.
4. Проверяющий вызывает `/formSubmissions/{submissionId}/review` с `accept` или `rework`.
5. Принятые изменения могут ссылаться на `changeSetId`; отображайте его как отдельный этап применения.

### Поручения

`/assignments` создаёт поручение и исполнения. Действия самого поручения идут через `/assignments/{assignmentId}/action`, действия исполнителя — `/assignmentExecutions/{executionId}/action`. Не выводите кнопки, которые не соответствуют текущему статусу и роли пользователя.

### Межведомственный обмен

`/interagencyRequests` создаёт запрос между разными организациями; `/action` меняет его состояние, `/responses` добавляет версионированный ответ. Объекты, поля, форма, срок и ожидаемый формат задаются в запросе.

## 14. Пользователи и аудит

Пользователи управляются через `/users`: список, создание, activate, deactivate, resetPassword и DELETE. Все операции Admin-only и выполняются в Keycloak; пароли в прикладной PostgreSQL не сохраняются.

`GET /api/v1/audit/events` доступен Admin и поддерживает фильтры `resourceType`, `resourceId`, `actorId`, `action`, `limit`, `offset`. Событие содержит старое/новое значение, diff, время, UUID, ФИО и email инициатора. Для истории карточки фильтруйте по типу и id ресурса.

## 15. Поиск и адреса

- `GET /api/v1/search/objectSuggestions?q=...` — быстрые подсказки по объектам; дополнительно доступны `entityCode`, `fieldCode`, `limit`.
- `GET /api/v1/geocoding/addressSuggestions?q=...&source=dadata|nominatim` — адресные подсказки через backend. Минимум три символа, ключи DaData в браузер не передаются.

Для выбора адреса сохраняйте структурированный результат провайдера, но разрешайте пользователю подтвердить/исправить значение. Геокодирование не должно блокировать сохранение неполной записи: такая запись остаётся черновиком с ошибками качества.

## 16. Что приложить frontend-команде

Передавайте вместе:

1. этот файл;
2. актуальный `http://localhost:8000/openapi.json`;
3. параметры Keycloak realm/client/redirect URI для нужного окружения;
4. перечень выданных ролей и permission grants;
5. base URL API и WebSocket для окружения.

Не фиксируйте модели из OpenAPI вручную: предпочтительно генерировать TypeScript-типы/клиент в CI и оборачивать их предметными composables/services, где реализованы refresh token, единая ошибка, отмена запросов, прогресс import job и обработка `409`.

## 17. Карта маршрутов

Ниже перечислены прикладные URL текущей версии. Методы, query-параметры и модели следует брать из приложенного `openapi.json`.

```text
# Сущности
GET,POST          /api/v1/entities
GET,PATCH,DELETE  /api/v1/entities/{identifier}
POST              /api/v1/entities/{identifier}/publish
POST              /api/v1/entities/{identifier}/archive
POST              /api/v1/entities/{identifier}/restore
POST              /api/v1/entities/{identifier}/duplicate

# Справочники
GET,POST          /api/v1/dictionaries
GET,PATCH,DELETE  /api/v1/dictionaries/{dictionaryId}
POST              /api/v1/dictionaries/{dictionaryId}/archive
POST              /api/v1/dictionaries/{dictionaryId}/restore

# Объекты и карта
GET,POST          /api/v1/entities/{entityCode}/objects
POST              /api/v1/entities/{entityCode}/objects/search
GET               /api/v1/entities/{entityCode}/objects/clusters
GET,PATCH,DELETE  /api/v1/entities/{entityCode}/objects/{objectId}
POST              /api/v1/entities/{entityCode}/objects/{objectId}/copy
POST              /api/v1/entities/{entityCode}/objects/{objectId}/archive
POST              /api/v1/entities/{entityCode}/objects/{objectId}/restore

# Вложения и версии
GET,POST          /api/v1/entities/{entityCode}/objects/{objectId}/attachments
GET,PATCH,DELETE  /api/v1/entities/{entityCode}/objects/{objectId}/attachments/{attachmentId}
GET               /api/v1/entities/{entityCode}/objects/{objectId}/attachments/{attachmentId}/download
PUT               /api/v1/entities/{entityCode}/objects/{objectId}/attachments/{attachmentId}/file
GET               /api/v1/entities/{entityCode}/objects/{objectId}/attachments/{attachmentId}/versions
GET               /api/v1/entities/{entityCode}/objects/{objectId}/attachments/{attachmentId}/versions/{version}/download

# Excel и import jobs
POST              /api/v1/entities/{entityCode}/excel/preview
POST              /api/v1/entities/{entityCode}/excel/import
GET               /api/v1/entities/{entityCode}/excel/export
GET,POST          /api/v1/importProfiles
DELETE            /api/v1/importProfiles/{profileId}
GET               /api/v1/importJobs/{jobId}
POST              /api/v1/importJobs/{jobId}/command
WS                /api/v1/importJobs/{jobId}/ws

# ChangeSet
POST              /api/v1/changeSets
GET               /api/v1/changeSets/{changeSetId}
POST              /api/v1/changeSets/{changeSetId}/decision

# Организации и права
GET,POST          /api/v1/organizations
PATCH             /api/v1/organizations/{organizationId}
POST              /api/v1/memberships
PATCH,DELETE      /api/v1/memberships/{membershipId}
GET,POST          /api/v1/permission-grants
DELETE            /api/v1/permission-grants/{grantId}
GET,POST          /api/v1/saved-views
PATCH,DELETE      /api/v1/saved-views/{viewId}

# Связи
GET,POST          /api/v1/entityRelations
PATCH             /api/v1/entityRelations/{relationId}
GET,POST          /api/v1/objectRelations
PATCH,DELETE      /api/v1/objectRelations/{linkId}

# Формы, сбор данных, поручения и обмен
GET,POST          /api/v1/forms
PATCH             /api/v1/forms/{formId}
POST              /api/v1/forms/{formId}/publish
POST              /api/v1/forms/{formId}/archive
POST              /api/v1/forms/{formId}/restore
GET,POST          /api/v1/informationRequests
GET               /api/v1/informationRequests/{requestId}
POST              /api/v1/informationRequests/{requestId}/action
POST              /api/v1/requestRecipients/{recipientId}/submissions
POST              /api/v1/formSubmissions/{submissionId}/review
GET,POST          /api/v1/assignments
GET               /api/v1/assignments/{assignmentId}
POST              /api/v1/assignments/{assignmentId}/action
POST              /api/v1/assignmentExecutions/{executionId}/action
GET,POST          /api/v1/interagencyRequests
GET               /api/v1/interagencyRequests/{requestId}
POST              /api/v1/interagencyRequests/{requestId}/action
POST              /api/v1/interagencyRequests/{requestId}/responses

# Пользователи и аудит
GET,POST          /api/v1/users
POST              /api/v1/users/{userId}/activate
POST              /api/v1/users/{userId}/deactivate
POST              /api/v1/users/{userId}/resetPassword
DELETE            /api/v1/users/{userId}
GET               /api/v1/audit/events

# Поиск, геокодирование и dynamic API
GET               /api/v1/search/objectSuggestions
GET               /api/v1/geocoding/addressSuggestions
GET               /api/v1/generated/catalog
GET               /api/v1/generated/catalog/{entityCode}
GET               /api/v1/openapi.json
GET               /api/v1/generated/docs
GET               /api/v1/generated/{entityCode}/openapi.json
GET               /api/v1/generated/{entityCode}/docs
```

Dynamic OpenAPI/Swagger URL могут отсутствовать в основном `openapi.json`, потому что служебные docs-маршруты помечены `includeInSchema=false`; они всё равно доступны после авторизации и строятся только по опубликованным сущностям.
