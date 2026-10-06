from datetime import date, datetime
from typing import Any
from uuid import UUID

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
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
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MunicipalityModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "municipalities"
    __table_args__ = (Index("ix_municipalities_map_center_gist", "map_center", postgresql_using="gist"),)

    code: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    map_center: Mapped[Any | None] = mapped_column(Geometry("POINT", srid=4326, spatial_index=False))
    map_zoom: Mapped[int] = mapped_column(Integer, nullable=False, default=12, server_default="12")


class OrganizationModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organizations"
    __table_args__ = (
        Index("ix_organizations_parent", "parent_id"),
        Index("ix_organizations_active_name", "active", "name"),
    )

    parent_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    code: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class MembershipModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", "role_code", name="membership_user_organization_role"),
        Index("ix_memberships_user_active", "user_id", "active"),
        Index("ix_memberships_organization_active", "organization_id", "active"),
    )

    user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    organization_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    role_code: Mapped[str] = mapped_column(String(120), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


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
    schema_version_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schema_versions.id", ondelete="RESTRICT"), nullable=False
    )
    parent_object_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_objects.id", ondelete="RESTRICT")
    )
    municipality_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("municipalities.id", ondelete="RESTRICT")
    )
    owner_organization_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    responsible_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
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
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PermissionGrantModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "permission_grants"
    __table_args__ = (
        CheckConstraint("user_id is not null or role_code is not null", name="subject"),
        Index("ix_permission_grants_user", "user_id"),
        Index("ix_permission_grants_role", "role_code"),
        Index("ix_permission_grants_entity", "entity_schema_id"),
        Index("ix_permission_grants_object", "entity_object_id"),
    )

    user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    role_code: Mapped[str | None] = mapped_column(String(120))
    organization_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    entity_schema_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="CASCADE")
    )
    entity_field_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_fields.id", ondelete="CASCADE")
    )
    entity_object_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_objects.id", ondelete="CASCADE")
    )
    actions: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    conditions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SavedViewModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "saved_views"
    __table_args__ = (
        UniqueConstraint("entity_schema_id", "owner_id", "name", name="saved_view_owner_name"),
        Index("ix_saved_views_entity_owner", "entity_schema_id", "owner_id"),
    )

    entity_schema_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="CASCADE"), nullable=False
    )
    owner_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    shared: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")


class EntityUniqueValueModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "entity_unique_values"
    __table_args__ = (
        UniqueConstraint("entity_field_id", "normalized_value", name="entity_unique_field_value"),
        UniqueConstraint("entity_object_id", "entity_field_id", name="entity_unique_object_field"),
        Index("ix_entity_unique_values_entity", "entity_schema_id"),
    )

    entity_schema_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="CASCADE"), nullable=False
    )
    entity_field_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_fields.id", ondelete="CASCADE"), nullable=False
    )
    entity_object_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_objects.id", ondelete="CASCADE"), nullable=False
    )
    normalized_value: Mapped[str] = mapped_column(Text, nullable=False)


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
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    scan_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default="pending")


class AttachmentVersionModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "attachment_versions"
    __table_args__ = (
        UniqueConstraint("attachment_id", "version", name="attachment_version"),
        Index("ix_attachment_versions_attachment", "attachment_id", "version"),
    )

    attachment_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("attachments.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    original_name: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class EntityRelationModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "entity_relations"

    code: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_entity_schema_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="RESTRICT"), nullable=False
    )
    target_entity_schema_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="RESTRICT"), nullable=False
    )
    source_cardinality: Mapped[str] = mapped_column(String(8), nullable=False, default="many", server_default="many")
    target_cardinality: Mapped[str] = mapped_column(String(8), nullable=False, default="many", server_default="many")
    field_definitions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class ObjectRelationModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "object_relations"
    __table_args__ = (
        UniqueConstraint("relation_id", "source_object_id", "target_object_id", name="object_relation"),
    )

    relation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_relations.id", ondelete="RESTRICT"), nullable=False
    )
    source_object_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_objects.id", ondelete="CASCADE"), nullable=False
    )
    target_object_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_objects.id", ondelete="CASCADE"), nullable=False
    )
    values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    attachment_paths: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    updated_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))


class ImportProfileModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "import_profiles"
    __table_args__ = (
        UniqueConstraint("entity_schema_id", "owner_id", "name", name="import_profile_owner_name"),
        Index("ix_import_profiles_entity_owner", "entity_schema_id", "owner_id"),
    )

    entity_schema_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="CASCADE"), nullable=False
    )
    owner_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sheet_name: Mapped[str | None] = mapped_column(String(255))
    header_row: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    mapping: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")


class ChangeSetModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "change_sets"
    __table_args__ = (
        UniqueConstraint("entity_schema_id", "idempotency_key", name="change_set_idempotency"),
        Index("ix_change_sets_entity_status", "entity_schema_id", "status", "created_at"),
    )

    entity_schema_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="RESTRICT"), nullable=False
    )
    schema_version_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schema_versions.id", ondelete="RESTRICT"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft", server_default="draft")
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    decided_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict, server_default="{}")


class ChangeSetItemModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "change_set_items"
    __table_args__ = (Index("ix_change_set_items_set_status", "change_set_id", "status"),)

    change_set_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("change_sets.id", ondelete="CASCADE"), nullable=False
    )
    object_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_objects.id", ondelete="RESTRICT")
    )
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    base_revision: Mapped[int | None] = mapped_column(Integer)
    proposed_values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    proposed_geometry: Mapped[Any | None] = mapped_column(Geometry("GEOMETRY", srid=4326, spatial_index=False))
    validation_errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default="pending")
    result_object_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_objects.id", ondelete="RESTRICT")
    )


class FormDefinitionModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "form_definitions"

    entity_schema_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="RESTRICT"), nullable=False
    )
    owner_organization_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft", server_default="draft")
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))


class FormVersionModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "form_versions"
    __table_args__ = (UniqueConstraint("form_id", "version", name="form_version"),)

    form_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("form_definitions.id", ondelete="CASCADE"), nullable=False
    )
    schema_version_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schema_versions.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class InformationRequestModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "information_requests"

    request_type: Mapped[str] = mapped_column(String(24), nullable=False)
    entity_schema_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_schemas.id", ondelete="RESTRICT"), nullable=False
    )
    form_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("form_definitions.id", ondelete="RESTRICT"), nullable=False
    )
    form_version_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("form_versions.id", ondelete="RESTRICT")
    )
    owner_organization_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewer_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    selected_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    correction_rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft", server_default="draft")
    created_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))


class RequestRecipientModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "request_recipients"
    __table_args__ = (UniqueConstraint("request_id", "organization_id", name="request_recipient"),)

    request_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("information_requests.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    object_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    field_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    contact_email: Mapped[str | None] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="not_started", server_default="not_started")
    reopened_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reopen_reason: Mapped[str | None] = mapped_column(Text)
    last_submission_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("form_submissions.id", ondelete="SET NULL", use_alter=True)
    )


class FormAccessLinkModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "form_access_links"

    recipient_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("request_recipients.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    requires_otp: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    allowed_actions: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class FormSubmissionModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "form_submissions"
    __table_args__ = (UniqueConstraint("recipient_id", "version", name="form_submission_version"),)

    recipient_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("request_recipients.id", ondelete="CASCADE"), nullable=False
    )
    form_version_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("form_versions.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft", server_default="draft")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    source_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    change_set_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("change_sets.id", ondelete="RESTRICT")
    )
    submitted_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_comment: Mapped[str | None] = mapped_column(Text)


class FieldConfirmationModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "field_confirmations"
    __table_args__ = (
        UniqueConstraint("object_id", "entity_field_id", "value_hash", name="field_confirmation_value"),
    )

    object_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_objects.id", ondelete="CASCADE"), nullable=False
    )
    entity_field_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entity_fields.id", ondelete="CASCADE"), nullable=False
    )
    value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    confirmed_by_label: Mapped[str | None] = mapped_column(String(500))
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submission_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("form_submissions.id", ondelete="SET NULL")
    )


class AssignmentModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "assignments"

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    owner_organization_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    created_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    reviewer_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="normal", server_default="normal")
    expected_result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    related_request_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("information_requests.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft", server_default="draft")


class AssignmentExecutionModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "assignment_executions"

    assignment_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False
    )
    assignee_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    assignee_organization_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    object_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="not_started", server_default="not_started")
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    change_set_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("change_sets.id", ondelete="RESTRICT")
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completion_late: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    review_comment: Mapped[str | None] = mapped_column(Text)


class InteragencyRequestModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "interagency_requests"

    sender_organization_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    recipient_organization_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    object_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    field_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    form_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("form_definitions.id", ondelete="RESTRICT")
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expected_format: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft", server_default="draft")
    created_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))


class InteragencyResponseVersionModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "interagency_response_versions"
    __table_args__ = (UniqueConstraint("request_id", "version", name="interagency_response_version"),)

    request_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("interagency_requests.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    change_set_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("change_sets.id", ondelete="RESTRICT")
    )
    submitted_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    accepted_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
