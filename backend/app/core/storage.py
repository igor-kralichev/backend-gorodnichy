from functools import lru_cache

from minio import Minio

from app.core.config import settings


@lru_cache
def get_minio_client() -> Minio:
    """Создать клиент MinIO для S3-совместимого хранилища файлов."""

    return Minio(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


def ensure_bucket_exists(client: Minio, bucket_name: str) -> None:
    """Создать bucket, если он ещё не существует."""

    if not client.bucket_exists(bucket_name):
        client.make_bucket(bucket_name)
