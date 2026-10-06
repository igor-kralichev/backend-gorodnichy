# RabbitMQ

Брокер сообщений для длительных импортов и публикации transactional outbox.

## Запуск

```bash
docker compose -f postgres/stack.yml up -d
docker compose -f rabbitmq/stack.yml up --build -d
```

- AMQP внутри Docker: `amqp://lowcode:lowcode@rabbitmq:5672/`
- Management UI: <http://localhost:15672>
- локальные credentials: `lowcode/lowcode`

Очереди создаются приложением:

- `object-imports` — команды import-worker для больших JSON-массивов и XLSX;
- `platform-events` — события outbox для будущих consumers/read models.

RabbitMQ обеспечивает доставку, но не является источником истины состояния job: прогресс хранится в PostgreSQL. Consumers должны быть идемпотентными, поскольку подтверждённая модель доставки допускает повтор сообщения.

Данные брокера находятся в volume `municipal_low_code_rabbitmq_data`. В production замените credentials, настройте отдельный vhost/права, TLS, dead-letter policy и наблюдаемость очередей.
