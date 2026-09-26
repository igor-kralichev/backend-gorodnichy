from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"
    app_name: str = "API муниципальной low-code платформы"
    api_v1_prefix: str = "/api/v1"
    database_url: str = "postgresql+asyncpg://lowcode:lowcode@postgres:5432/lowcode"
    redis_url: str = "redis://redis:6379/0"
    redis_schema_ttl_seconds: int = 3600
    rabbitmq_url: str = "amqp://lowcode:lowcode@rabbitmq:5672/"
    import_queue_name: str = "object-imports"
    import_async_threshold: int = 1000
    import_batch_size: int = 500
    import_temp_prefix: str = "imports"
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:5173"])
    log_level: str = "INFO"
    admin_role_name: str = "Admin"
    default_user_role_name: str = "User"
    keycloak_public_url: str = "http://localhost:8080"
    keycloak_internal_url: str = "http://keycloak:8080"
    keycloak_realm: str = "municipal-low-code"
    keycloak_api_client_id: str = "municipal-api"
    keycloak_api_client_secret: str = "municipal-api-secret"
    keycloak_swagger_client_id: str = "municipal-spa"
    minio_endpoint: str = "minio:9000"
    minio_public_endpoint: str = "localhost:9000"
    minio_access_key: str = "lowcode"
    minio_secret_key: str = "lowcode-secret"
    minio_bucket: str = "municipal-attachments"
    minio_secure: bool = False
    max_attachment_size_bytes: int = 50 * 1024 * 1024
    dadata_api_key: str | None = None
    dadata_secret_key: str | None = None
    dadata_suggestions_url: str = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/address"
    nominatim_url: str = "http://nominatim:8080"
    nominatim_public_url: str = "http://localhost:8081"
    geocoding_default_limit: int = 10

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def keycloak_issuer(self) -> str:
        return f"{self.keycloak_public_url.rstrip('/')}/realms/{self.keycloak_realm}"

    @property
    def keycloak_internal_realm_url(self) -> str:
        return f"{self.keycloak_internal_url.rstrip('/')}/realms/{self.keycloak_realm}"

    @property
    def keycloak_authorization_url(self) -> str:
        return f"{self.keycloak_issuer}/protocol/openid-connect/auth"

    @property
    def keycloak_token_url(self) -> str:
        return f"{self.keycloak_issuer}/protocol/openid-connect/token"

    @property
    def keycloak_internal_token_url(self) -> str:
        return f"{self.keycloak_internal_realm_url}/protocol/openid-connect/token"

    @property
    def keycloak_jwks_url(self) -> str:
        return f"{self.keycloak_internal_realm_url}/protocol/openid-connect/certs"

    @property
    def keycloak_admin_realm_url(self) -> str:
        return f"{self.keycloak_internal_url.rstrip('/')}/admin/realms/{self.keycloak_realm}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
