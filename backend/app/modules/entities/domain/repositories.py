from typing import Protocol

from app.modules.entities.infrastructure.models import EntitySchemaModel


class EntitySchemaRepository(Protocol):
    async def code_exists(self, code: str) -> bool: ...

    def add(self, entity: EntitySchemaModel) -> None: ...

