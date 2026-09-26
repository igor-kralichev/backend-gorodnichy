from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.entities.infrastructure.models import EntitySchemaModel
from app.modules.search.api.schemas import ObjectSuggestionPage, ObjectSuggestionRead
from app.shared.db.models import EntityObjectModel, ObjectSearchIndexModel


class ObjectSuggestionService:
    """Поиск подсказок по материализованному индексу значений объектов."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def suggest(
        self,
        *,
        query: str,
        entity_code: str | None,
        field_code: str | None,
        limit: int,
    ) -> ObjectSuggestionPage:
        normalized_query = self._normalize(query)
        if not normalized_query:
            return ObjectSuggestionPage(query=query, returned=0, data=[])

        conditions = [
            EntitySchemaModel.status == "active",
            EntityObjectModel.status != "archived",
            ObjectSearchIndexModel.normalized_value.ilike(f"%{normalized_query}%"),
        ]
        if entity_code is not None:
            conditions.append(EntitySchemaModel.code == entity_code)
        if field_code is not None:
            conditions.append(ObjectSearchIndexModel.field_code == field_code)

        statement = (
            select(
                ObjectSearchIndexModel,
                EntitySchemaModel.code,
                EntitySchemaModel.name,
                EntityObjectModel.updated_at,
            )
            .join(
                EntitySchemaModel,
                EntitySchemaModel.id == ObjectSearchIndexModel.entity_schema_id,
            )
            .join(
                EntityObjectModel,
                EntityObjectModel.id == ObjectSearchIndexModel.object_id,
            )
            .where(and_(*conditions))
            .order_by(
                ObjectSearchIndexModel.normalized_value.asc(),
                EntityObjectModel.updated_at.desc(),
            )
            .limit(limit)
        )
        rows = (await self._session.execute(statement)).all()
        data = [
            ObjectSuggestionRead(
                entity_id=row[0].entity_schema_id,
                entity_code=row[1],
                entity_name=row[2],
                object_id=row[0].object_id,
                field_code=row[0].field_code,
                value=row[0].value,
                updated_at=row[3],
            )
            for row in rows
        ]
        return ObjectSuggestionPage(query=query, returned=len(data), data=data)

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(value.strip().lower().split())
