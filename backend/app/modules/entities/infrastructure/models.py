from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.shared.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class EntitySchemaModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "entity_schemas"
    __table_args__ = (
        CheckConstraint("status in ('draft', 'active', 'archived')", name="status"),
        CheckConstraint("geometry_type in ('none', 'point', 'lineString', 'polygon')", name="geometry_type"),
        Index("ix_entity_schemas_status_updated_at", "status", "updated_at"),
        Index(
            "ix_entity_schemas_scope",
            "scope_municipality_id",
            postgresql_where=text("scope_municipality_id is not null"),
        ),
        Index(
            "ix_entity_schemas_parent",
            "parent_entity_schema_id",
            postgresql_where=text("parent_entity_schema_id is not null"),
        ),
    )

    code: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    parent_entity_schema_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="SET NULL")
    )
    owner_organization_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    scope_municipality_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("municipalities.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    geometry_type: Mapped[str] = mapped_column(String(20), nullable=False, default="none", server_default="none")
    clustering_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft", server_default="draft")
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    layer_selectable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    layer_visible_by_default: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )

    fields: Mapped[list[EntityFieldModel]] = relationship(
        back_populates="entity_schema",
        cascade="all, delete-orphan",
        order_by="EntityFieldModel.sort_order",
        lazy="selectin",
        foreign_keys="EntityFieldModel.entity_schema_id",
    )
    geometry_types: Mapped[list[EntityAllowedGeometryTypeModel]] = relationship(
        back_populates="entity_schema", cascade="all, delete-orphan", lazy="selectin"
    )
    map_styles: Mapped[list[EntityMapStyleModel]] = relationship(
        back_populates="entity_schema", cascade="all, delete-orphan", lazy="selectin"
    )
    color_rules: Mapped[list[EntityMapColorRuleModel]] = relationship(
        back_populates="entity_schema", cascade="all, delete-orphan", lazy="selectin"
    )
    versions: Mapped[list[EntitySchemaVersionModel]] = relationship(
        back_populates="entity_schema", cascade="all, delete-orphan", lazy="raise"
    )
    layer: Mapped[EntityLayerModel | None] = relationship(
        back_populates="entity_schema",
        cascade="all, delete-orphan",
        lazy="selectin",
        uselist=False,
    )


class EntitySchemaVersionModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "entity_schema_versions"
    __table_args__ = (
        UniqueConstraint("entity_schema_id", "version", name="entity_schema_version"),
        Index("ix_entity_schema_versions_schema_created", "entity_schema_id", "created_at"),
    )

    entity_schema_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    entity_schema: Mapped[EntitySchemaModel] = relationship(back_populates="versions")


class EntityFieldModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "entity_fields"
    __table_args__ = (
        UniqueConstraint("entity_schema_id", "code", name="entity_field_code"),
        UniqueConstraint("entity_schema_id", "sort_order", name="entity_field_order"),
        CheckConstraint(
            "field_type in ('string', 'text', 'integer', 'decimal', 'boolean', 'date', 'datetime', "
            "'address', 'enum', 'reference', 'file', 'phone', 'email', 'url', 'calculated')",
            name="field_type",
        ),
        CheckConstraint("sort_order > 0", name="positive_sort_order"),
        Index("ix_entity_fields_schema_searchable", "entity_schema_id", postgresql_where=text("searchable")),
        Index("ix_entity_fields_schema_filterable", "entity_schema_id", postgresql_where=text("filterable")),
    )

    entity_schema_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    field_type: Mapped[str] = mapped_column(String(24), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    list_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    card_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    searchable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    filterable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    hint: Mapped[str | None] = mapped_column(Text)
    default_value: Mapped[Any | None] = mapped_column(JSONB)
    group_name: Mapped[str | None] = mapped_column(String(255))
    min_length: Mapped[int | None] = mapped_column(Integer)
    max_length: Mapped[int | None] = mapped_column(Integer)
    min_value: Mapped[float | None] = mapped_column(Numeric)
    max_value: Mapped[float | None] = mapped_column(Numeric)
    unique_value: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    multiple: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    access_rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    formula: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    dictionary_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("dictionaries.id", ondelete="RESTRICT")
    )
    reference_entity_schema_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="RESTRICT")
    )

    entity_schema: Mapped[EntitySchemaModel] = relationship(back_populates="fields", foreign_keys=[entity_schema_id])


class EntityAllowedGeometryTypeModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "entity_allowed_geometry_types"
    __table_args__ = (
        UniqueConstraint("entity_schema_id", "geometry_type", name="entity_allowed_geometry_type"),
        UniqueConstraint("entity_schema_id", "sort_order", name="entity_allowed_geometry_order"),
        CheckConstraint("geometry_type in ('point', 'lineString', 'polygon')", name="geometry_type"),
    )

    entity_schema_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="CASCADE"), nullable=False
    )
    geometry_type: Mapped[str] = mapped_column(String(20), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)

    entity_schema: Mapped[EntitySchemaModel] = relationship(back_populates="geometry_types")


class EntityMapStyleModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "entity_map_styles"
    __table_args__ = (
        UniqueConstraint("entity_schema_id", "geometry_type", name="entity_map_style_geometry"),
        CheckConstraint("geometry_type in ('point', 'lineString', 'polygon')", name="geometry_type"),
        CheckConstraint("stroke_width > 0", name="positive_stroke_width"),
        CheckConstraint("point_size > 0", name="positive_point_size"),
        CheckConstraint("opacity >= 0 and opacity <= 1", name="valid_opacity"),
    )

    entity_schema_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="CASCADE"), nullable=False
    )
    geometry_type: Mapped[str] = mapped_column(String(20), nullable=False)
    fill: Mapped[str] = mapped_column(String(20), nullable=False)
    stroke: Mapped[str] = mapped_column(String(20), nullable=False)
    stroke_width: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    point_size: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    opacity: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)

    entity_schema: Mapped[EntitySchemaModel] = relationship(back_populates="map_styles")


class EntityMapColorRuleModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "entity_map_color_rules"
    __table_args__ = (
        CheckConstraint(
            "operator in ('equals', 'notEquals', 'contains', 'filled', 'empty', 'before', 'after')", name="operator"
        ),
        Index("ix_entity_map_color_rules_schema_order", "entity_schema_id", "sort_order"),
    )

    entity_schema_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="CASCADE"), nullable=False
    )
    entity_field_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_fields.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    operator: Mapped[str] = mapped_column(String(24), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    color: Mapped[str] = mapped_column(String(20), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)

    entity_schema: Mapped[EntitySchemaModel] = relationship(back_populates="color_rules")


class EntityLayerModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "entity_layers"
    __table_args__ = (
        UniqueConstraint(
            "entity_schema_id",
            name="uq_entity_layers_entity_schema_id",
        ),
        UniqueConstraint("code", name="uq_entity_layers_code"),
        CheckConstraint(
            "opacity >= 0 and opacity <= 1",
            name="valid_opacity",
        ),
        Index("ix_entity_layers_active_visible", "active", "visible_by_default"),
    )

    entity_schema_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("entity_schemas.id", ondelete="CASCADE"),
        nullable=False,
    )
    code: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    style: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    opacity: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    selectable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    visible_by_default: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    entity_schema: Mapped[EntitySchemaModel] = relationship(back_populates="layer")
