# MinIO

S3-совместимое хранилище бинарных файлов объектов и временных payload фонового импорта.

## Запуск

```bash
docker compose -f postgres/stack.yml up -d
docker compose -f minio/stack.yml up --build -d
```

- S3 API: <http://localhost:9000>
- Console: <http://localhost:9001>
- локальные credentials: `lowcode/lowcode-secret`

Backend использует один bucket `municipal-attachments`. Постоянные вложения лежат в папках объектов по UUID; временные JSON/XLSX импорта — под prefix `imports` и удаляются worker-ом после успеха, ошибки или отмены.

Метаданные, оригинальные имена и версии находятся в PostgreSQL. Пользователь скачивает файл через авторизованный backend, поэтому frontend не должен получать root credentials MinIO или напрямую строить S3 URL.

Данные находятся в volume `municipal_low_code_minio_data`. В production замените credentials и настройте bucket policy, TLS, versioning/lifecycle и резервное копирование отдельно.
