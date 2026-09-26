class EntitySchemaError(Exception):
    """Базовая ошибка модуля схем сущностей."""


class EntityCodeAlreadyExists(EntitySchemaError):
    def __init__(self, code: str) -> None:
        super().__init__(f"Код сущности «{code}» уже существует")
        self.code = code


class EntityFieldReferenceError(EntitySchemaError):
    pass


class EntitySchemaNotFound(EntitySchemaError):
    pass


class EntitySchemaConflict(EntitySchemaError):
    pass
