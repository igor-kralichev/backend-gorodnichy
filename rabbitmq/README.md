# RabbitMQ

Очередь фоновых задач массового импорта объектов.

## Локальный запуск

```bash
docker compose -f rabbitmq/stack.yml up -d --build
```

Management UI: http://localhost:15672

Локальные учётные данные по умолчанию:

- пользователь: `lowcode`
- пароль: `lowcode`

Backend и import-worker подключаются по адресу `amqp://lowcode:lowcode@rabbitmq:5672/`
в общей docker-сети `municipal_low_code_network`.
