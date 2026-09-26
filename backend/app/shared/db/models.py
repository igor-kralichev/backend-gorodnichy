from datetime import datetime
from typing import Any
from uuid import UUID

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MunicipalityModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "municipalities"
    __table_args__ = (Index("ix_municipalities_map_center_gist", "map_center", postgresql_using="gist"),)

    code: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    map_center: Mapped[Any | None] = mapped_column(Geometry("POINT", srid=4326, spatial_index=False))
    map_zoom: Mapped[int] = mapped_column(Integer, nullable=False, default=12, server_default="12")


class DictionaryModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "dictionaries"
    __table_args__ = (
        UniqueConstraint("entity_schema_id", "code", name="dictionary_entity_code"),
        Index("ix_dictionaries_entity_active", "entity_schema_id", "active"),
    )

    entity_schema_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class DictionaryItemModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "dictionary_items"
    __table_args__ = (
        UniqueConstraint("dictionary_id", "code", name="dictionary_item_code"),
        UniqueConstraint("dictionary_id", "normalized_name", name="dictionary_item_normalized_name"),
        Index("ix_dictionary_items_dictionary_active_name", "dictionary_id", "active", "name"),
    )

    dictionary_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("dictionaries.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(500), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class EntityObjectModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "entity_objects"
    __table_args__ = (
        CheckConstraint("status in ('draft', 'published', 'archived')", name="status"),
        CheckConstraint("data_quality in ('complete', 'incomplete')", name="data_quality"),
        Index("ix_entity_objects_entity_status_updated", "entity_schema_id", "status", "updated_at"),
        Index(
            "ix_entity_objects_parent_status_updated",
            "parent_object_id",
            "status",
            "updated_at",
            postgresql_where=text("parent_object_id is not null"),
        ),
        Index("ix_entity_objects_created_by", "created_by"),
        Index("ix_entity_objects_values_gin", "values", postgresql_using="gin"),
        Index("ix_entity_objects_geometry_gist", "geometry", postgresql_using="gist"),
    )

    entity_schema_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="RESTRICT"), nullable=False
    )
    parent_object_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_objects.id", ondelete="RESTRICT")
    )
    municipality_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("municipalities.id", ondelete="RESTRICT")
    )
    values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    geometry: Mapped[Any | None] = mapped_column(Geometry("GEOMETRY", srid=4326, spatial_index=False))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft", server_default="draft")
    data_quality: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="incomplete",
        server_default="incomplete",
    )
    validation_errors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
    )
    attachment_paths: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    updated_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))


class ObjectSearchIndexModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "object_search_index"
    __table_args__ = (
        UniqueConstraint("object_id", "field_code", "value", name="object_search_index_object_field_value"),
        Index("ix_object_search_index_entity_field", "entity_schema_id", "field_code"),
        Index("ix_object_search_index_object", "object_id"),
        Index("ix_object_search_index_normalized_trgm", "normalized_value", postgresql_using="gin", postgresql_ops={"normalized_value": "gin_trgm_ops"}),
    )

    entity_schema_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="CASCADE"), nullable=False
    )
    object_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_objects.id", ondelete="CASCADE"), nullable=False
    )
    field_code: Mapped[str] = mapped_column(String(120), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_value: Mapped[str] = mapped_column(Text, nullable=False)


class ObjectEventModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "object_events"
    __table_args__ = (
        UniqueConstraint("object_id", "revision", name="object_event_revision"),
        Index("ix_object_events_object_occurred", "object_id", "occurred_at"),
        Index("ix_object_events_entity_occurred", "entity_schema_id", "occurred_at"),
    )

    entity_schema_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="RESTRICT"), nullable=False
    )
    object_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_objects.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    before_values: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after_values: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )


class AttachmentModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "attachments"
    __table_args__ = (
        CheckConstraint("kind in ('photo', 'document')", name="kind"),
        Index("ix_attachments_object_kind_created", "object_id", "kind", "created_at"),
    )

    entity_schema_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="RESTRICT"), nullable=False
    )
    object_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_objects.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    original_name: Mapped[str] = mapped_column(String(1024), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))


class ImportJobModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "import_jobs"
    __table_args__ = (
        CheckConstraint("status in ('queued', 'running', 'paused', 'completed', 'failed', 'cancelled')", name="status"),
        Index("ix_import_jobs_entity_status_created", "entity_schema_id", "status", "created_at"),
    )

    entity_schema_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default="queued")
    source_name: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_object_key: Mapped[str | None] = mapped_column(String(1024))
    mapping: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    processed_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))


class ImportRowModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "import_rows"
    __table_args__ = (
        UniqueConstraint("import_job_id", "row_number", name="import_row_number"),
        CheckConstraint("status in ('pending', 'imported', 'incomplete', 'failed')", name="status"),
        Index("ix_import_rows_job_status", "import_job_id", "status"),
    )

    import_job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("import_jobs.id", ondelete="CASCADE"), nullable=False
    )
    object_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_objects.id", ondelete="SET NULL")
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    raw_values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    normalized_values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")


class AuditEventModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_resource_occurred", "resource_type", "resource_id", text("occurred_at desc")),
        Index("ix_audit_events_actor_occurred", "actor_id", text("occurred_at desc")),
        Index("ix_audit_events_action_occurred", "action", text("occurred_at desc")),
    )

    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    resource_code: Mapped[str | None] = mapped_column(String(160))
    resource_name: Mapped[str | None] = mapped_column(String(500))
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    actor_full_name: Mapped[str | None] = mapped_column(String(500))
    actor_email: Mapped[str | None] = mapped_column(String(320))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    old_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )


class OutboxEventModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_events_unpublished", "published_at", "occurred_at"),
        Index("ix_outbox_events_aggregate", "aggregate_type", "aggregate_id"),
    )

    aggregate_type: Mapped[str] = mapped_column(String(120), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
