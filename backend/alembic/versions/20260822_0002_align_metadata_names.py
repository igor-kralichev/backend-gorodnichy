"""Согласовать ограничения и индексы с ORM-метаданными.

Идентификатор ревизии: 20260822_0002
Предыдущая ревизия: 20260822_0001
Дата создания: 2026-08-22
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260822_0002"
down_revision: str | None = "20260822_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CONSTRAINT_RENAMES = (
    ("attachments", "attachments_object_key_key", "uq_attachments_object_key"),
    ("dictionaries", "uq_dictionary_entity_code", "dictionary_entity_code"),
    ("dictionary_items", "uq_dictionary_item_code", "dictionary_item_code"),
    ("entity_allowed_geometry_types", "uq_entity_allowed_geometry_order", "entity_allowed_geometry_order"),
    ("entity_allowed_geometry_types", "uq_entity_allowed_geometry_type", "entity_allowed_geometry_type"),
    ("entity_fields", "uq_entity_field_code", "entity_field_code"),
    ("entity_fields", "uq_entity_field_order", "entity_field_order"),
    ("entity_map_styles", "uq_entity_map_style_geometry", "entity_map_style_geometry"),
    ("entity_schema_versions", "uq_entity_schema_versions_entity_schema_version", "entity_schema_version"),
    ("entity_schemas", "entity_schemas_code_key", "uq_entity_schemas_code"),
    ("import_rows", "uq_import_row_number", "import_row_number"),
    ("municipalities", "municipalities_code_key", "uq_municipalities_code"),
    ("object_events", "uq_object_event_revision", "object_event_revision"),
)


INDEXES = {
    "ix_attachments_object_kind_created": "attachments (object_id, kind, created_at)",
    "ix_entity_objects_entity_status_updated": "entity_objects (entity_schema_id, status, updated_at)",
    "ix_entity_schema_versions_schema_created": "entity_schema_versions (entity_schema_id, created_at)",
    "ix_entity_schemas_status_updated_at": "entity_schemas (status, updated_at)",
    "ix_import_jobs_entity_status_created": "import_jobs (entity_schema_id, status, created_at)",
    "ix_object_events_entity_occurred": "object_events (entity_schema_id, occurred_at)",
    "ix_object_events_object_occurred": "object_events (object_id, occurred_at)",
}


def upgrade() -> None:
    for table, old_name, new_name in CONSTRAINT_RENAMES:
        op.execute(f'alter table {table} rename constraint "{old_name}" to "{new_name}"')
    for name, definition in INDEXES.items():
        op.execute(f'drop index if exists "{name}"')
        op.execute(f'create index "{name}" on {definition}')


def downgrade() -> None:
    for name, definition in INDEXES.items():
        table, columns = definition.split(" ", maxsplit=1)
        desc_columns = columns[:-1] + " desc)"
        op.execute(f'drop index if exists "{name}"')
        op.execute(f'create index "{name}" on {table} {desc_columns}')
    for table, old_name, new_name in reversed(CONSTRAINT_RENAMES):
        op.execute(f'alter table {table} rename constraint "{new_name}" to "{old_name}"')
