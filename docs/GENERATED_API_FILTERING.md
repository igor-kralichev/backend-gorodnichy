# Типобезопасная фильтрация и read-only Generated API

Документ фиксирует контракт фильтров, составной выдачи «реестр + подреестры» и доступа к автоматически сформированному API. Точные JSON Schema всегда берутся из `openapi.json`.

## 1. Типобезопасная фильтрация JSONB

Значения объектов остаются в `entity_objects.values JSONB`, но сравнение строится с учётом `field.type` из опубликованной схемы.

- `integer` приводится к PostgreSQL `bigint`;
- `decimal` приводится к `numeric`;
- `boolean` принимает только `true` или `false` и сравнивается как boolean;
- `date` разбирается как `YYYY-MM-DD` и сравнивается как `date`;
- `datetime` требует ISO 8601 с `Z` или смещением `±HH:MM`, при сохранении нормализуется к UTC и сравнивается как `timestamptz`;
- `reference`, `id` и `parentObjectId` проверяются как UUID;
- `enum` перед сравнением нормализуется к стабильному `dictionaryItem.code`;
- текстовые операторы экранируют `%` и `_`, поэтому пользовательский ввод не превращается в произвольный SQL LIKE-шаблон.

Операторы диапазона разрешены только для чисел, даты и даты-времени. `today`, `beforeToday`, `afterToday` разрешены только для `date`/`datetime`. Неприменимый оператор возвращает `422 invalid_filter`, а не неявное строковое сравнение.

### Множественные поля

Для `multiple=true` backend использует JSONB-массив и операторы PostgreSQL:

- `equals` — массив содержит переданное значение;
- `notEquals` — массив не содержит значение;
- `in` — массив содержит хотя бы одно из значений, перечисленных через запятую;
- `notIn` — массив не содержит ни одного значения;
- `contains`, `startsWith`, `endsWith` — условие проверяется для каждого отдельного элемента массива;
- `filled`/`empty` — проверяется длина массива.

Это применяется к множественным `enum`, `reference` и `file`. Сортировка множественного поля запрещена, потому что порядок JSON-массива не является значением колонки.

### Связанные поля

Поддерживается один уровень связи:

```json
{"field":"school.district","operator":"equals","value":"Автозаводский"}
```

`school` должен быть фильтруемым полем типа `reference`. Для дочерней сущности доступен родитель:

```json
{"field":"parent.count","operator":"greaterThan","value":"20"}
```

При JWT backend дополнительно проверяет право `read` на связанную сущность. Через `X-API-Key` связанный фильтр выполняется только внутри read-only generated API.

### Индексы

Общий GIN-индекс `ix_entity_objects_values_gin` используется операторами JSONB `@>`, в том числе для множественных значений. Приведение произвольного динамического поля к `numeric`, `date` или `timestamptz` не может эффективно использовать один общий GIN-индекс. Для действительно горячих полей на больших реестрах expression-индексы должны добавляться отдельной миграцией после анализа реальных запросов; создавать индекс на каждое low-code поле автоматически не следует из-за стоимости записи и объёма БД.

## 2. Составная выдача реестра и подреестров

`POST /api/v1/entities/{entityCode}/objects/queryTree` принимает выбранные колонки, фильтры и сортировку родителя и каждого непосредственного подреестра.

```json
{
  "columns": ["nazvanie", "address"],
  "filters": [
    {"field": "status", "operator": "equals", "value": "published"}
  ],
  "sort": "nazvanie",
  "limit": 100,
  "offset": 0,
  "children": [
    {
      "entityCode": "korpusa",
      "columns": ["nazvanie", "etazhnost"],
      "filters": [
        {"field": "etazhnost", "operator": "greaterOrEqual", "value": "2"}
      ],
      "sort": "nazvanie",
      "limitPerParent": 100
    }
  ]
}
```

Пустой `columns` означает все неархивные поля с `listVisible=true`. В JSON дочерние строки группируются в `children[entityCode]`; родительские записи не размножаются. Backend загружает каждый подреестр одним оконным SQL-запросом, а не отдельным запросом на каждого родителя. Защитный предел составной выборки — 100 000 потенциальных дочерних строк.

Для XLSX используется `POST /api/v1/entities/{entityCode}/excel/exportSelection`
с тем же телом. Первый лист содержит родительский реестр. Каждый подреестр
получает отдельный лист с выбранными человекочитаемыми колонками родителя и
дочернего объекта. Технические UUID, revision и schemaVersion в файл для
пользователя не добавляются; при обратном импорте frontend передаёт системное
сопоставление отдельным JSON. Такое разделение листов исключает декартово
умножение нескольких подреестров.

## 3. Read-only Generated API

Сгенерированные маршруты чтения отделены от изменяющего runtime CRUD:

```text
GET  /api/v1/generated/{entityCode}/objects
POST /api/v1/generated/{entityCode}/objects/search
POST /api/v1/generated/{entityCode}/objects/queryTree
GET  /api/v1/generated/{entityCode}/objects/{objectId}
```

Они принимают один из двух вариантов авторизации:

```http
Authorization: Bearer <accessToken>
```

или

```http
X-API-Key: municipal-generated-<entityUuidWithoutDashes>.<keycloakClientSecret>
```

Ключ привязан к одной active-сущности. Для `queryTree` он также разрешает чтение её непосредственных подреестров в рамках составного запроса. `X-API-Key` не принимается ручками создания, изменения, архивации, восстановления и удаления.

### Управление ключом

Только пользователь с realm role `Admin` может вызвать:

```text
POST   /api/v1/generated/apiKeys/{entityCode}         создать
GET    /api/v1/generated/apiKeys/{entityCode}         посмотреть полное значение
POST   /api/v1/generated/apiKeys/{entityCode}/rotate  перегенерировать
DELETE /api/v1/generated/apiKeys/{entityCode}         отозвать
```

Secret хранится в Keycloak confidential client. Backend не пишет его в аудит; журнал содержит только действие и сущность. После `rotate` прежний ключ перестаёт проходить проверку Keycloak.

Важно: автоматическое создание/просмотр/ротация clients требует отдельного делегирования Keycloak service account прав на управление generated clients. Не выдавайте realm-wide `manage-clients` без согласованной модели администрирования; для production предпочтительны Fine-Grained Admin Permissions либо отдельный изолированный realm для машинных клиентов.

`X-API-Key` предназначен для server-to-server интеграций. Его нельзя встраивать в SPA, хранить в localStorage или публиковать в клиентском bundle: любой пользователь браузера сможет извлечь ключ.
