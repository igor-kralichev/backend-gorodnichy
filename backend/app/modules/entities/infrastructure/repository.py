from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.entities.infrastructure.models import EntitySchemaModel


class SqlAlchemyEntitySchemaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def code_exists(self, code: str) -> bool:
        statement = select(exists().where(EntitySchemaModel.code == code))
        return bool(await self._session.scalar(statement))

    def add(self, entity: EntitySchemaModel) -> None:
        self._session.add(entity)

