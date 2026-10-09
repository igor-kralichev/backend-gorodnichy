class DictionaryError(Exception):
    """Ошибка управления справочником."""


class DictionaryAlreadyExists(DictionaryError):
    """Справочник с таким кодом уже существует у сущности."""


class DictionaryEntityNotFound(DictionaryError):
    """Сущность для справочника не найдена."""


class DictionaryNotFound(DictionaryError):
    """Справочник не найден."""


class DictionaryConflict(DictionaryError):
    """Справочник изменён параллельно или содержит конфликтующее значение."""


class DictionaryItemNotFound(DictionaryError):
    """Элемент справочника не найден."""
