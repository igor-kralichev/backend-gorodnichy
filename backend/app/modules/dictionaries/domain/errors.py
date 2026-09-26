class DictionaryError(Exception):
    """Ошибка управления справочником."""


class DictionaryAlreadyExists(DictionaryError):
    """Справочник с таким кодом уже существует у сущности."""


class DictionaryEntityNotFound(DictionaryError):
    """Сущность для справочника не найдена."""


class DictionaryNotFound(DictionaryError):
    """Справочник не найден."""
