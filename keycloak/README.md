# Keycloak

Контур идентификации платформы. Он состоит из Keycloak и отдельной PostgreSQL; прикладная БД `lowcode` для него не используется.

## Запуск

```bash
docker compose -f postgres/stack.yml up -d
docker compose -f keycloak/stack.yml up --build -d
```

Admin Console: <http://localhost:8080>. Локальные bootstrap-данные по умолчанию: `admin/admin`. Они относятся к realm `master`.

## Realm приложения

При первом старте `import/municipal-low-code-realm.json` создаёт realm `municipal-low-code`, realm roles `Admin`/`User`, глобальные роли `permission_*`, confidential client `municipal-api`, public PKCE client `municipal-spa` и service account backend. `Admin` является составной ролью и наследует все `permission_*`; backend не содержит отдельного обхода проверки прав по имени `Admin`. Файл является конфигурацией начального окружения, а не постоянным реестром пользователей.

В импортируемом realm настроена следующая политика сессий и токенов:

- access token — 5 минут (`accessTokenLifespan=300`);
- бездействующая SSO/client session — 30 минут;
- максимальная SSO/client session — 12 часов;
- `Revoke Refresh Token` включён;
- `Refresh Token Max Reuse` равен `0`: использованный refresh token повторно применять нельзя, при каждом refresh клиент обязан принять новый токен.

Offline tokens для SPA не используются. `municipal-spa` остаётся public client с Authorization Code Flow + PKCE S256; implicit flow и Direct Access Grants не требуются.

## Read-only API-ключи сущностей

Backend представляет ключ сгенерированного API как secret отдельного confidential client `municipal-generated-<entityUuidWithoutDashes>`. Полное значение передаётся интеграции в заголовке `X-API-Key`; обычные пользовательские CRUD-операции этот заголовок не принимают.

Создание, просмотр, ротация и удаление такого client выполняются только административными backend-ручками. Secret не записывается в аудит. Для этих операций service account `municipal-api` нужны строго контролируемые права Keycloak на generated clients. Realm-wide `manage-clients` даёт доступ ко всем clients realm и не должен назначаться автоматически без отдельного решения по модели безопасности. Для production предпочтительны Fine-Grained Admin Permissions или отдельный realm машинных интеграций.

Импорт realm не перезаписывает уже существующий realm при каждом рестарте. Пользователей приложения создавайте в `municipal-low-code` через Admin Console или защищённые `/api/v1/users` backend. Для Swagger нужен пользователь этого realm; bootstrap-admin realm `master` там не авторизуется.

Если volume Keycloak уже содержит realm, изменение JSON само по себе не обновит его. Настройте те же значения в Admin Console (`Realm settings → Tokens` и `Realm settings → Sessions`) либо примените управляемый импорт/частичное обновление realm. Не удаляйте volume только ради применения настройки, если в нём уже есть нужные пользователи и конфигурация.

## Данные

PostgreSQL Keycloak хранится в volume `municipal_low_code_keycloak_postgres_data`. Обычная пересборка безопасна; `down -v` удалит realm, пользователей и настройки клиентов.

В production обязательно замените bootstrap password, client secret и публичные URL через переменные окружения/secret storage.
