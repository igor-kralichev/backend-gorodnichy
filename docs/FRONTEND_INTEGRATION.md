# Интеграция frontend с API муниципальной low-code платформы

Документ дополняет `openapi.json`: OpenAPI является источником истины по точным телам запросов и ответов, а здесь описаны последовательности вызовов, состояния интерфейса и связи между модулями.

## 1. Общие правила клиента

- Base URL локально: `http://localhost:8000`.
- Все прикладные маршруты начинаются с `/api/v1`. Обычный CRUD требует `Authorization: Bearer <accessToken>`. Только read-only маршруты `/api/v1/generated/{entityCode}/objects...` дополнительно принимают `X-API-Key`.
- JSON-поля и параметры используют `camelCase`; уже существующие URL переименовывать не требуется.
- Даты приходят в ISO 8601. Форматировать их для пользователя следует на frontend в выбранной временной зоне.
- GeoJSON хранится в WGS 84 (`EPSG:4326`), координаты передаются в порядке `[longitude, latitude]`.
- Для трассировки frontend может отправлять `X-Request-ID`; backend возвращает его в одноимённом заголовке и в теле ошибки.
- При `401` клиент обновляет токен или отправляет пользователя на вход. `403` означает, что пользователь опознан, но не имеет права на операцию.
- Realm-роль `Admin` в Keycloak включает составные глобальные роли `permission_*`, поэтому даёт полный доступ ко всем прикладным операциям, включая Excel. Backend не делает специального исключения для имени `Admin`: он проверяет полученные из JWT глобальные роли и предметные permission grants.

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

Access token клиента `municipal-spa` содержит audience `municipal-api`.
Backend проверяет подпись, issuer, audience, срок действия и тип `Bearer`; ID token
или access token другого API использовать для запросов нельзя.

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
Каталог уже отфильтрован backend по effective permissions текущего пользователя:
недоступные сущности и поля в него не попадают. Поэтому frontend не должен
восстанавливать скрытые пункты меню из локального кэша или полного каталога Admin.

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

Справочники бывают `global`, `organization` и `entity`. При запросе с
`entityId` и `includeShared=true` backend добавляет доступные общие и
организационные списки. Для редактирования используйте возвращённую `revision`:
атомарные `POST/PATCH/DELETE .../items` отклоняют устаревшую ревизию как `409`.
`capabilities` содержит `read` и, если разрешено, `manage`.

Значения из XLSX загружаются в два шага:

1. `POST /dictionaries/{dictionaryId}/excel/preview` с `file`, `sheetName`,
   `headerRow`, `valueColumn` — показать new/existing/duplicate/empty.
2. `POST /dictionaries/{dictionaryId}/excel/import` с теми же полями и
   `revision` — выполнить merge без удаления прежних значений.

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

В `PATCH` поле `revision` обязательно. При `409` не перезаписывайте данные молча: перечитайте карточку, покажите различия и предложите повторить изменение. `values` в PATCH содержит только изменяемые динамические поля; `readOnly`, архивные и запрещённые ролью поля backend отклоняет с `422`.

### Геометрия

Допустимы `Point`, `MultiPoint`, `LineString`, `MultiLineString`, `Polygon`, `MultiPolygon`, `GeometryCollection`. Для коллекции используйте `geometries`, для остальных типов — `coordinates`.

### Фильтрация и карта

Для простого списка применяйте query-параметры `filters[fieldCode][operator]=value`, `logic=and|or`, `sort=fieldCode|-fieldCode`, `limit`, `offset`, `parentObjectId`, `bbox=minLon,minLat,maxLon,maxLat`. Для сложного фильтра используйте `POST .../objects/search`.

Операторы: `equals`, `notEquals`, `contains`, `startsWith`, `endsWith`, `greaterThan`, `greaterOrEqual`, `lessThan`, `lessOrEqual`, `in`, `notIn`, `filled`, `empty`, `today`, `beforeToday`, `afterToday`.

Backend применяет тип поля из схемы: числа, boolean, date и datetime сравниваются как типы PostgreSQL, а не как строки. `datetime` требует ISO 8601 с `Z` или смещением часового пояса и сохраняется в UTC. Для `multiple=true` операторы работают по отдельным элементам JSON-массива; сортировка множественного поля запрещена.

Один уровень связанного фильтра задаётся через точку: `school.district`. Для поля родительского объекта дочерней сущности используйте `parent.count`. При пользовательской JWT-авторизации backend проверяет право чтения связанной сущности.

### Реестр вместе с подреестрами

`POST /api/v1/entities/{entityCode}/objects/queryTree` возвращает выбранные колонки родителя и непосредственных подреестров. В теле передаются `columns`, `filters`, `sort`, пагинация и массив `children`; у каждого child свои `entityCode`, `columns`, `filters`, `sort`, `limitPerParent`. Пустой `columns` означает поля `listVisible`.

Дочерние записи находятся в `data[].children[childEntityCode]`, поэтому frontend не должен самостоятельно выполнять N+1 запрос на каждого родителя. Полный пример находится в [GENERATED_API_FILTERING.md](GENERATED_API_FILTERING.md).

Для карты на небольшом масштабе используйте `/clusters` с `bbox` и `zoom`; после приближения запрашивайте обычные объекты по bbox.

## 7. Фоновые импорты

Job имеет статусы `queued`, `running`, `paused`, `completed`, `failed`, `cancelled` и счётчики `totalRows`, `processedRows`, `errorRows`.

```text
GET  /api/v1/importJobs
GET  /api/v1/importJobs/{jobId}
POST /api/v1/importJobs/{jobId}/command   {"command":"pause|resume|cancel"}
WS   /api/v1/importJobs/{jobId}/ws?token=ACCESS_TOKEN
```

Рекомендуемый UI: сразу показать job в центре задач, открыть WebSocket и обновлять прогресс из сообщений; при разрыве соединения продолжить polling GET. Команды WebSocket/HTTP должны отображаться только пока допустимы текущим статусом. Просматривать job может только его инициатор.

## 8. Excel

Все Excel-ручки работают только с `.xlsx` без макросов.

Для импорта нужен предметный grant с действием `import` либо глобальная realm-роль `permission_import`, для экспорта — grant `export` либо роль `permission_export`. Составная роль `Admin` наследует оба права в Keycloak. После изменения ролей необходимо обновить access token либо войти повторно, потому что роли зафиксированы внутри JWT.

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

`GET /api/v1/entities/{entityCode}/excel/export` скачивает пользовательский XLSX без технической строки с UUID полей. Первая строка содержит понятные названия колонок, данные начинаются со второй строки. Для последующего импорта frontend передаёт отдельный JSON `mapping`, связывающий заголовки Excel с кодами полей сущности; backend не ожидает служебных метаданных внутри выгрузки.

Синхронный экспорт ограничен 50 000 строками. Если число доступных
пользователю строк превышает этот лимит, backend возвращает `422`, а не
неполный файл. Строки с началом `+`, `-`, `=` и `@` остаются исходным текстом
после скачивания и повторного чтения XLSX.

`POST /api/v1/entities/{entityCode}/excel/exportSelection` принимает контракт
`queryTree` и выгружает выбранные колонки с учётом фильтров. Для явного выбора
передайте `selectionMode=ids` и `objectIds`; для выборки по фильтру —
`selectionMode=filter`, фильтры и при необходимости `excludedIds`. Родитель
находится на первом листе; каждый подреестр — на отдельном листе с выбранными
колонками родителя. UUID/revision/schemaVersion в книгу для человека не
добавляются.

Для больших выборок используйте фоновый экспорт:

```text
POST /api/v1/entities/{entityCode}/excel/exportJobs
GET  /api/v1/excel/exportJobs?status=queued|running|completed|failed|cancelled
GET  /api/v1/excel/exportJobs/{jobId}
POST /api/v1/excel/exportJobs/{jobId}/command   {"command":"cancel"}
GET  /api/v1/excel/exportJobs/{jobId}/download
```

POST принимает тот же выбор колонок, фильтров, `selectionMode`, `objectIds`,
`excludedIds` и подреестров. Ответ `202` содержит job. Опрос можно прекратить
при `completed|failed|cancelled`; при `completed` используйте `downloadUrl`.
Фоновый предел по умолчанию — 500 000 суммарных строк реестра и подреестров,
файл доступен автору задачи 24 часа. Результат формирует export-worker через
RabbitMQ и хранит в MinIO; прямой доступ к бакету frontend не нужен.
Параметры выборки фиксируются в job, а данные читаются в момент начала работы worker.

`mapping.system` отделён от динамических полей и поддерживает родителя,
координаты/GeoJSON и идентификатор существующего объекта:

```json
{
  "sheetName": "Лист1",
  "headerRow": 1,
  "columns": {"Название": "name", "Адрес": "address"},
  "system": {
    "fixedParentObjectId": null,
    "parentObjectColumn": "ID родителя",
    "objectIdColumn": "ID объекта",
    "actionColumn": "Действие",
    "longitudeColumn": "Долгота",
    "latitudeColumn": "Широта"
  }
}
```

Для подтверждаемого импорта используйте `POST
/entities/{entityCode}/excel/plans`, затем пагинированный `GET
/excel/plans/{planId}`. Он возвращает `base/file/current`, состояние строки и
ошибки. `POST /excel/plans/{planId}/decision` принимает `apply/reject/cancel`;
при конкурентном изменении передайте `resolutions` с ключом
`rowIndex.fieldCode` и значением `file|current`. План действует 24 часа.

Геометрии и бинарные файлы в текущий XLSX-экспорт не входят.

## 8.1. Сгенерированный API и X-API-Key

Read-only интеграционные маршруты находятся под `/api/v1/generated/{entityCode}/objects`. Они поддерживают список, сложный search, карточку и `queryTree`. Изменяющие операции остаются только под `/api/v1/entities/...` и всегда требуют пользовательский JWT.

Администратор управляет ключом через `POST/GET /generated/apiKeys/{entityCode}`, `POST .../rotate` и `DELETE`. GET намеренно показывает полное значение только роли `Admin`. Административный UI должен скрывать ключ по умолчанию, давать явную кнопку «Показать/скопировать» и не логировать ответ.

Не передавайте `X-API-Key` из публичной Vue SPA. Он предназначен для серверной интеграции; браузерный интерфейс продолжает использовать Bearer JWT Keycloak.

### Профили импорта

`POST/GET /api/v1/importProfiles` и `DELETE /api/v1/importProfiles/{profileId}` сохраняют сопоставление колонок для конкретного пользователя и сущности.

## 9. ChangeSet

ChangeSet — атомарная группа команд, а не отдельная копия объекта. Он нужен для Excel, форм, актуализации и пакетного редактирования UI:

1. `POST /api/v1/changeSets` с `entityCode`, `source`, необязательным `idempotencyKey` и массивом `items`.
2. `GET /api/v1/changeSets/{changeSetId}` показывает проверку каждой строки.
3. `POST /api/v1/changeSets/{changeSetId}/decision` с `apply`, `reject` или `cancel`.

Операции элемента: `create`, `update`, `archive`, `confirm`. Для update обязательны `objectId` и `baseRevision`. Применение атомарно: конфликт не должен оставлять частично записанный пакет. Backend повторно сверяет текущую версию схемы и валидирует значения при apply; при `409` из-за новой версии схемы нужно заново подготовить ChangeSet. Неизменившийся update не увеличивает ревизию объекта. Повтор того же `idempotencyKey` безопасен; другой payload с тем же ключом возвращает `409`.

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
- `GET /api/v1/geocoding/reverse?longitude=...&latitude=...` — адрес точки.
- `GET /api/v1/geocoding/buildings?bbox=minLon,minLat,maxLon,maxLat` — контуры зданий; не запрашивайте большую область.

## 13. Capabilities, черновик схемы, уведомления и история

- `GET /api/v1/me/capabilities?entityCode=...&objectId=...` — единственный
  источник доступности кнопок и полей; frontend не вычисляет права по имени
  realm-role.
- Для active-схемы сначала `GET /entities/{identifier}/draft`, затем
  `PATCH /draft` с `expectedVersion`; перед публикацией вызовите
  `POST /draft/validation`, после подтверждения — `POST /draft/publish`.
- `GET /notifications`, `PATCH /notifications/{notificationId}` и
  `POST /notifications/readAll` обслуживают bell текущего пользователя.
- `GET /audit/resources/{resourceType}/{resourceId}` отдаёт разрешённую историю
  ресурса; недоступные значения полей backend не возвращает.

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
GET,PATCH         /api/v1/entities/{identifier}/draft
POST              /api/v1/entities/{identifier}/draft/validation
POST              /api/v1/entities/{identifier}/draft/publish
GET               /api/v1/metadata/units
GET               /api/v1/metadata/markerIcons

# Справочники
GET,POST          /api/v1/dictionaries
GET,PATCH,DELETE  /api/v1/dictionaries/{dictionaryId}
POST              /api/v1/dictionaries/{dictionaryId}/archive
POST              /api/v1/dictionaries/{dictionaryId}/restore
POST              /api/v1/dictionaries/{dictionaryId}/items
PATCH,DELETE      /api/v1/dictionaries/{dictionaryId}/items/{itemId}
POST              /api/v1/dictionaries/{dictionaryId}/excel/preview
POST              /api/v1/dictionaries/{dictionaryId}/excel/import

# Объекты и карта
GET,POST          /api/v1/entities/{entityCode}/objects
POST              /api/v1/entities/{entityCode}/objects/search
GET               /api/v1/entities/{entityCode}/objects/clusters
POST              /api/v1/entities/{entityCode}/objects/clusters/search
POST              /api/v1/entities/{entityCode}/objects/queryTree
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
POST              /api/v1/entities/{entityCode}/excel/exportSelection
POST              /api/v1/entities/{entityCode}/excel/exportJobs
GET               /api/v1/excel/exportJobs
GET               /api/v1/excel/exportJobs/{jobId}
POST              /api/v1/excel/exportJobs/{jobId}/command
GET               /api/v1/excel/exportJobs/{jobId}/download
POST              /api/v1/entities/{entityCode}/excel/plans
GET               /api/v1/excel/plans/{planId}
POST              /api/v1/excel/plans/{planId}/decision
GET,POST          /api/v1/importProfiles
DELETE            /api/v1/importProfiles/{profileId}
GET               /api/v1/importJobs
GET               /api/v1/importJobs/{jobId}
POST              /api/v1/importJobs/{jobId}/command
WS                /api/v1/importJobs/{jobId}/ws

# ChangeSet
POST              /api/v1/changeSets
GET               /api/v1/changeSets/{changeSetId}
POST              /api/v1/changeSets/{changeSetId}/decision

# Организации и права
GET               /api/v1/me/capabilities
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
GET               /api/v1/audit/resources/{resourceType}/{resourceId}
GET               /api/v1/notifications
PATCH             /api/v1/notifications/{notificationId}
POST              /api/v1/notifications/readAll

# Поиск, геокодирование и dynamic API
GET               /api/v1/search/objectSuggestions
GET               /api/v1/geocoding/addressSuggestions
GET               /api/v1/geocoding/reverse
GET               /api/v1/geocoding/buildings
GET               /api/v1/generated/catalog
GET               /api/v1/generated/catalog/{entityCode}
GET               /api/v1/openapi.json
GET               /api/v1/generated/docs
GET               /api/v1/generated/{entityCode}/openapi.json
GET               /api/v1/generated/{entityCode}/docs
```

Dynamic OpenAPI/Swagger URL могут отсутствовать в основном `openapi.json`, потому что служебные docs-маршруты помечены `includeInSchema=false`; они всё равно доступны после авторизации и строятся только по опубликованным сущностям.
