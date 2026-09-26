from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.config import settings
from app.modules.entities.api.schemas import to_camel


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class UserCreate(ApiModel):
    """Команда создания пользователя в Keycloak."""

    username: str | None = Field(
        default=None,
        min_length=3,
        max_length=120,
        description="Логин пользователя; если не передан, используется email",
    )
    email: EmailStr = Field(description="Электронная почта пользователя")
    first_name: str | None = Field(
        default=None,
        max_length=120,
        description="Имя пользователя",
    )
    last_name: str | None = Field(
        default=None,
        max_length=120,
        description="Фамилия пользователя",
    )
    enabled: bool = Field(default=True, description="Разрешить пользователю вход")
    temporary_password: str | None = Field(
        default=None,
        min_length=8,
        max_length=200,
        description="Временный пароль; можно не передавать",
    )
    roles: list[str] = Field(
        default_factory=lambda: [settings.default_user_role_name],
        min_length=1,
        max_length=20,
        description="Realm roles пользователя",
    )

    @field_validator("username", "first_name", "last_name")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("roles")
    @classmethod
    def normalize_roles(cls, value: list[str]) -> list[str]:
        roles = []
        for role in value:
            stripped = role.strip()
            if stripped and stripped not in roles:
                roles.append(stripped)
        if not roles:
            raise ValueError("Необходимо передать хотя бы одну роль")
        return roles


class UserRead(ApiModel):
    """Пользователь, созданный в Keycloak."""

    id: UUID = Field(description="UUID пользователя Keycloak")
    username: str = Field(description="Логин пользователя")
    email: EmailStr = Field(description="Электронная почта пользователя")
    enabled: bool = Field(description="Разрешён ли вход")
    roles: list[str] = Field(description="Назначенные realm roles")


class UserListItemRead(ApiModel):
    """Пользователь из списка Keycloak."""

    id: UUID = Field(description="UUID пользователя Keycloak")
    full_name: str = Field(description="ФИО пользователя")
    email: EmailStr | None = Field(default=None, description="Электронная почта пользователя")
    roles: list[str] = Field(description="Realm roles пользователя")


class UserListRead(ApiModel):
    """Страница пользователей Keycloak."""

    items: list[UserListItemRead] = Field(description="Пользователи")
    total: int = Field(ge=0, description="Общее количество найденных пользователей")
    limit: int = Field(ge=1, description="Количество пользователей на странице")
    offset: int = Field(ge=0, description="Смещение от начала списка")


class UserPasswordReset(ApiModel):
    """Команда сброса пароля пользователя."""

    temporary_password: str = Field(
        min_length=8,
        max_length=200,
        description="Новый временный пароль пользователя",
    )
    temporary: bool = Field(
        default=True,
        description="Потребовать смену пароля при следующем входе",
    )


class UserStatusRead(ApiModel):
    """Результат изменения статуса пользователя."""

    id: UUID = Field(description="UUID пользователя Keycloak")
    enabled: bool = Field(description="Разрешён ли вход")


class UserPasswordResetRead(ApiModel):
    """Результат сброса пароля."""

    id: UUID = Field(description="UUID пользователя Keycloak")
    password_reset: bool = Field(description="Пароль сброшен")
    temporary: bool = Field(description="Потребуется ли смена пароля при следующем входе")


class UserDeleteRead(ApiModel):
    """Результат удаления пользователя."""

    id: UUID = Field(description="UUID пользователя Keycloak")
    deleted: bool = Field(description="Пользователь удалён")
