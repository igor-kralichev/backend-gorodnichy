class IdentityProviderError(Exception):
    """Ошибка обмена с Keycloak."""


class UserAlreadyExists(IdentityProviderError):
    """Пользователь с таким username или email уже существует."""


class UserRoleNotFound(IdentityProviderError):
    """Одна из запрошенных realm roles не найдена."""


class UserNotFound(IdentityProviderError):
    """Пользователь Keycloak не найден."""
