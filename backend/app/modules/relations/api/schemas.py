from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.modules.entities.api.schemas import ApiModel


class RelationFieldDefinition(ApiModel):
    id: UUID
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=120)
    name: str = Field(min_length=1, max_length=255)
    type: Literal["string", "text", "integer", "decimal", "boolean", "date", "datetime", "url", "file"]
    required: bool = False


class EntityRelationCreate(ApiModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=120)
    name: str = Field(min_length=1, max_length=255)
    source_entity_schema_id: UUID
    target_entity_schema_id: UUID
    source_cardinality: Literal["one", "many"] = "many"
    target_cardinality: Literal["one", "many"] = "many"
    fields: list[RelationFieldDefinition] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_fields(self) -> "EntityRelationCreate":
        if len({field.id for field in self.fields}) != len(self.fields):
            raise ValueError("UUID полей связи должны быть уникальны")
        if len({field.code for field in self.fields}) != len(self.fields):
            raise ValueError("Коды полей связи должны быть уникальны")
        return self


class EntityRelationUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    fields: list[RelationFieldDefinition] | None = Field(default=None, max_length=100)
    active: bool | None = None


class EntityRelationRead(ApiModel):
    id: UUID
    code: str
    name: str
    source_entity_schema_id: UUID
    target_entity_schema_id: UUID
    source_cardinality: str
    target_cardinality: str
    fields: list[RelationFieldDefinition]
    active: bool
    created_at: datetime
    updated_at: datetime


class ObjectRelationCreate(ApiModel):
    relation_id: UUID
    source_object_id: UUID
    target_object_id: UUID
    values: dict[str, Any] = Field(default_factory=dict)


class ObjectRelationPatch(ApiModel):
    values: dict[str, Any]
    revision: int = Field(ge=1)


class ObjectRelationRead(ApiModel):
    id: UUID
    relation_id: UUID
    source_object_id: UUID
    target_object_id: UUID
    values: dict[str, Any]
    attachment_paths: list[str]
    revision: int
    created_by: UUID | None
    updated_by: UUID | None
    created_at: datetime
    updated_at: datetime


class RelationDeleteRead(ApiModel):
    id: UUID
    deleted: bool = True
