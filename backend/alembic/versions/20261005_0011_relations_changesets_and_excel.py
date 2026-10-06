"""Добавляет связи, наборы изменений, Excel-профили и версии файлов.

Revision ID: 20261005_0011
Revises: 20261005_0010
Create Date: 2026-10-05 13:00:00.000000
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261005_0011"
down_revision: str | None = "20261005_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Создать таблицы второго этапа предметной модели."""

    op.execute(
        """
        create table entity_relations (
          id uuid primary key default gen_random_uuid(),
          code varchar(120) not null unique,
          name varchar(255) not null,
          source_entity_schema_id uuid not null references entity_schemas(id) on delete restrict,
          target_entity_schema_id uuid not null references entity_schemas(id) on delete restrict,
          source_cardinality varchar(8) not null default 'many',
          target_cardinality varchar(8) not null default 'many',
          field_definitions jsonb not null default '[]'::jsonb,
          active boolean not null default true,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          constraint ck_entity_relation_source_cardinality check (source_cardinality in ('one', 'many')),
          constraint ck_entity_relation_target_cardinality check (target_cardinality in ('one', 'many'))
        )
        """
    )
    op.execute("create index ix_entity_relations_source on entity_relations(source_entity_schema_id, active)")
    op.execute("create index ix_entity_relations_target on entity_relations(target_entity_schema_id, active)")
    op.execute(
        """
        create table object_relations (
          id uuid primary key default gen_random_uuid(),
          relation_id uuid not null references entity_relations(id) on delete restrict,
          source_object_id uuid not null references entity_objects(id) on delete cascade,
          target_object_id uuid not null references entity_objects(id) on delete cascade,
          values jsonb not null default '{}'::jsonb,
          attachment_paths jsonb not null default '[]'::jsonb,
          revision integer not null default 1,
          created_by uuid,
          updated_by uuid,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          constraint uq_object_relation unique(relation_id, source_object_id, target_object_id),
          constraint ck_object_relation_distinct_objects check(source_object_id <> target_object_id)
        )
        """
    )
    op.execute("create index ix_object_relations_source on object_relations(source_object_id, relation_id)")
    op.execute("create index ix_object_relations_target on object_relations(target_object_id, relation_id)")

    op.execute(
        """
        create table import_profiles (
          id uuid primary key default gen_random_uuid(),
          entity_schema_id uuid not null references entity_schemas(id) on delete cascade,
          owner_id uuid not null,
          name varchar(255) not null,
          sheet_name varchar(255),
          header_row integer not null default 1,
          mapping jsonb not null default '{}'::jsonb,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          constraint uq_import_profile_owner_name unique(entity_schema_id, owner_id, name),
          constraint ck_import_profile_header_row check(header_row > 0)
        )
        """
    )
    op.execute("create index ix_import_profiles_entity_owner on import_profiles(entity_schema_id, owner_id)")

    op.execute(
        """
        create table change_sets (
          id uuid primary key default gen_random_uuid(),
          entity_schema_id uuid not null references entity_schemas(id) on delete restrict,
          schema_version_id uuid not null references entity_schema_versions(id) on delete restrict,
          source varchar(32) not null,
          status varchar(24) not null default 'draft',
          idempotency_key varchar(255),
          created_by uuid,
          decided_by uuid,
          decided_at timestamptz,
          metadata jsonb not null default '{}'::jsonb,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          constraint ck_change_set_source check(source in ('ui', 'excel', 'form', 'api', 'actualization')),
          constraint ck_change_set_status check(status in ('draft', 'validated', 'applied', 'rejected', 'conflict', 'cancelled')),
          constraint uq_change_set_idempotency unique(entity_schema_id, idempotency_key)
        )
        """
    )
    op.execute("create index ix_change_sets_entity_status on change_sets(entity_schema_id, status, created_at desc)")
    op.execute(
        """
        create table change_set_items (
          id uuid primary key default gen_random_uuid(),
          change_set_id uuid not null references change_sets(id) on delete cascade,
          object_id uuid references entity_objects(id) on delete restrict,
          operation varchar(16) not null,
          base_revision integer,
          proposed_values jsonb not null default '{}'::jsonb,
          proposed_geometry geometry(Geometry, 4326),
          validation_errors jsonb not null default '[]'::jsonb,
          status varchar(24) not null default 'pending',
          result_object_id uuid references entity_objects(id) on delete restrict,
          constraint ck_change_set_item_operation check(operation in ('create', 'update', 'archive', 'confirm')),
          constraint ck_change_set_item_status check(status in ('pending', 'valid', 'invalid', 'applied', 'conflict', 'excluded'))
        )
        """
    )
    op.execute("create index ix_change_set_items_set_status on change_set_items(change_set_id, status)")
    op.execute("create index ix_change_set_items_object on change_set_items(object_id) where object_id is not null")

    op.execute("alter table attachments add column current_version integer not null default 1")
    op.execute("alter table attachments add column scan_status varchar(24) not null default 'pending'")
    op.execute(
        "alter table attachments add constraint ck_attachment_scan_status "
        "check(scan_status in ('pending', 'clean', 'infected', 'failed'))"
    )
    op.execute(
        """
        create table attachment_versions (
          id uuid primary key default gen_random_uuid(),
          attachment_id uuid not null references attachments(id) on delete cascade,
          version integer not null,
          object_key varchar(1024) not null unique,
          original_name varchar(1024) not null,
          mime_type varchar(255) not null,
          size_bytes bigint not null,
          checksum_sha256 varchar(64) not null,
          uploaded_by uuid,
          created_at timestamptz not null default now(),
          constraint uq_attachment_version unique(attachment_id, version)
        )
        """
    )
    op.execute("create index ix_attachment_versions_attachment on attachment_versions(attachment_id, version desc)")
    op.execute(
        """
        insert into attachment_versions (
          attachment_id, version, object_key, original_name, mime_type,
          size_bytes, checksum_sha256, uploaded_by, created_at
        )
        select id, 1, object_key, original_name, mime_type,
               size_bytes, checksum_sha256, uploaded_by, created_at
        from attachments
        """
    )


def downgrade() -> None:
    """Удалить таблицы второго этапа предметной модели."""

    op.execute("drop table if exists attachment_versions")
    op.execute("alter table attachments drop constraint if exists ck_attachment_scan_status")
    op.execute("alter table attachments drop column if exists scan_status")
    op.execute("alter table attachments drop column if exists current_version")
    op.execute("drop table if exists change_set_items")
    op.execute("drop table if exists change_sets")
    op.execute("drop table if exists import_profiles")
    op.execute("drop table if exists object_relations")
    op.execute("drop table if exists entity_relations")
