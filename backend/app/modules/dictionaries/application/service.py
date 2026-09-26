from slugify import slugify
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.dictionaries.api.schemas import (
    DictionaryCreate,
    DictionaryDeleteRead,
    DictionaryItemCreate,
    DictionaryItemRead,
    DictionaryListRead,
    DictionaryRead,
    DictionaryStatusRead,
    DictionaryUpdate,
)
from app.modules.dictionaries.domain.errors import (
    DictionaryAlreadyExists,
    DictionaryEntityNotFound,
    DictionaryNotFound,
)
from app.modules.entities.infrastructure.models import EntitySchemaModel
from app.shared.db.models import DictionaryItemModel, DictionaryModel


class DictionaryService:
    """Сервис управления справочниками сущностей."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_page(
        self,
        *,
        entity_id: object | None,
        include_archived: bool,
        limit: int,
        offset: int,
    ) -> DictionaryListRead:
        conditions = []
        if entity_id is not None:
            conditions.append(DictionaryModel.entity_schema_id == entity_id)
        if not include_archived:
            conditions.append(DictionaryModel.active.is_(True))

        total = int(
            await self.session.scalar(select(func.count()).select_from(DictionaryModel).where(*conditions)) or 0
        )
        dictionaries = (
            await self.session.scalars(
                select(DictionaryModel)
                .where(*conditions)
                .order_by(DictionaryModel.name)
                .offset(offset)
                .limit(limit)
            )
        ).all()
        return DictionaryListRead(
            items=[await self._to_response(dictionary) for dictionary in dictionaries],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def create(self, payload: DictionaryCreate) -> DictionaryRead:
        entity_exists = await self.session.scalar(
            select(EntitySchemaModel.id).where(EntitySchemaModel.id == payload.entity_id)
        )
        if entity_exists is None:
            raise DictionaryEntityNotFound("Сущность для справочника не найдена")

        dictionary_code = payload.code or _code_from_name(payload.name)
        await self._ensure_code_available(payload.entity_id, dictionary_code)

        dictionary = DictionaryModel(
            entity_schema_id=payload.entity_id,
            code=dictionary_code,
            name=payload.name,
            active=True,
        )
        self.session.add(dictionary)
        await self.session.flush()

        self._add_items(dictionary.id, payload.items)

        await self.session.commit()
        return await self.get(dictionary.id)

    async def get(self, dictionary_id: object) -> DictionaryRead:
        dictionary = await self._get_model(dictionary_id)
        return await self._to_response(dictionary)

    async def update(self, dictionary_id: object, payload: DictionaryUpdate) -> DictionaryRead:
        async with self.session.begin():
            dictionary = await self._get_model(dictionary_id, for_update=True)
            if payload.code is not None and payload.code != dictionary.code:
                await self._ensure_code_available(dictionary.entity_schema_id, payload.code, exclude_id=dictionary.id)
                dictionary.code = payload.code
            if payload.name is not None:
                dictionary.name = payload.name
            if payload.items is not None:
                await self.session.execute(
                    delete(DictionaryItemModel).where(DictionaryItemModel.dictionary_id == dictionary.id)
                )
                self._add_items(dictionary.id, payload.items)
            await self.session.flush()
            await self.session.refresh(dictionary, attribute_names=["updated_at"])
        return await self.get(dictionary.id)

    async def archive(self, dictionary_id: object) -> DictionaryStatusRead:
        async with self.session.begin():
            dictionary = await self._get_model(dictionary_id, for_update=True)
            dictionary.active = False
            await self.session.flush()
        return DictionaryStatusRead(id=dictionary.id, active=dictionary.active)

    async def restore(self, dictionary_id: object) -> DictionaryStatusRead:
        async with self.session.begin():
            dictionary = await self._get_model(dictionary_id, for_update=True)
            dictionary.active = True
            await self.session.flush()
        return DictionaryStatusRead(id=dictionary.id, active=dictionary.active)

    async def delete(self, dictionary_id: object) -> DictionaryDeleteRead:
        dictionary = await self._get_model(dictionary_id)
        await self.session.delete(dictionary)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise
        return DictionaryDeleteRead(id=dictionary.id, deleted=True)

    async def _get_model(self, dictionary_id: object, *, for_update: bool = False) -> DictionaryModel:
        statement = select(DictionaryModel).where(DictionaryModel.id == dictionary_id)
        if for_update:
            statement = statement.with_for_update()
        dictionary = await self.session.scalar(statement)
        if dictionary is None:
            raise DictionaryNotFound("Справочник не найден")
        return dictionary

    async def _to_response(self, dictionary: DictionaryModel) -> DictionaryRead:
        items = (
            await self.session.scalars(
                select(DictionaryItemModel)
                .where(DictionaryItemModel.dictionary_id == dictionary.id)
                .order_by(DictionaryItemModel.sort_order, DictionaryItemModel.name)
            )
        ).all()
        return DictionaryRead(
            id=dictionary.id,
            entity_id=dictionary.entity_schema_id,
            code=dictionary.code,
            name=dictionary.name,
            active=dictionary.active,
            items=[
                DictionaryItemRead(
                    id=item.id,
                    code=item.code,
                    name=item.name,
                    active=item.active,
                    sort_order=item.sort_order,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
                for item in items
            ],
            created_at=dictionary.created_at,
            updated_at=dictionary.updated_at,
        )

    async def _ensure_code_available(self, entity_id: object, code: str, *, exclude_id: object | None = None) -> None:
        conditions = [
            DictionaryModel.entity_schema_id == entity_id,
            DictionaryModel.code == code,
        ]
        if exclude_id is not None:
            conditions.append(DictionaryModel.id != exclude_id)
        code_exists = await self.session.scalar(select(DictionaryModel.id).where(*conditions))
        if code_exists is not None:
            raise DictionaryAlreadyExists("Справочник с таким кодом уже существует у сущности")

    def _add_items(self, dictionary_id: object, items: list[DictionaryItemCreate]) -> None:
        used_item_codes: set[str] = set()
        used_item_names: set[str] = set()
        for index, item in enumerate(items, start=1):
            item_code = item.code or _unique_code(item.name, used_item_codes)
            if item_code in used_item_codes:
                raise ValueError("Коды элементов справочника должны быть уникальными")
            used_item_codes.add(item_code)
            normalized_name = _normalized_item_name(item.name)
            if normalized_name in used_item_names:
                raise ValueError("Названия элементов справочника должны быть уникальными без учёта регистра и пробелов по краям")
            used_item_names.add(normalized_name)
            sort_order = item.sort_order if "sort_order" in item.model_fields_set else index
            self.session.add(
                DictionaryItemModel(
                    dictionary_id=dictionary_id,
                    code=item_code,
                    name=item.name,
                    normalized_name=normalized_name,
                    active=item.active,
                    sort_order=sort_order,
                )
            )


def _code_from_name(name: str) -> str:
    return slugify(name, separator="_") or "dictionary"


def _normalized_item_name(name: str) -> str:
    return name.strip().casefold()


def _unique_code(name: str, used_codes: set[str]) -> str:
    base_code = _code_from_name(name)
    code = base_code
    suffix = 2
    while code in used_codes:
        code = f"{base_code}_{suffix}"
        suffix += 1
    return code
