# PostgreSQL/PostGIS

Прикладная база low-code платформы. Образ основан на multi-arch PostgreSQL 17 и устанавливает PostGIS, поэтому Apple Silicon не требует amd64-эмуляции.

## Запуск

```bash
docker compose -f postgres/stack.yml up --build -d
```

Этот проект создаёт общую сеть `municipal_low_code_network`, поэтому его запускают первым. Данные находятся в volume `municipal_low_code_postgres_data`; таблицы создаёт Alembic-контейнер `migrate` из проекта backend.

Локальные значения по умолчанию: база `lowcode`, пользователь `lowcode`, пароль `lowcode`, порт `5432`. Внутри Docker hostname — `postgres`, с хоста — `localhost`.

## pgAdmin

```bash
docker compose -f postgres/stack.yml --profile tools up -d pgadmin
```

Интерфейс: <http://localhost:5050>, локальный вход `admin@local.dev/admin`. `pgadmin/servers.json` только добавляет описание сервера в UI; он не содержит данные приложения, не выполняет миграции и не создаёт таблицы. Пароль БД при первом подключении — `lowcode`, если окружение не переопределено.

Не используйте `down -v`, если требуется сохранить прикладную БД или настройки pgAdmin.
