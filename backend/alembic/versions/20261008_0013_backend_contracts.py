"""Добавляет недостающие API-контракты реестров и справочников.

Revision ID: 20261008_0013
Revises: 20261005_0012
Create Date: 2026-10-08 13:00:00.000000
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261008_0013"
down_revision: str | None = "20261005_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Расширить метаданные и создать таблицы черновиков/уведомлений."""

    op.execute("alter table dictionaries alter column entity_schema_id drop not null")
    op.execute("alter table dictionaries add column scope varchar(24) not null default 'entity'")
    op.execute("alter table dictionaries add column owner_organization_id uuid references organizations(id) on delete restrict")
    op.execute("alter table dictionaries add column revision integer not null default 1")
    op.execute("alter table dictionaries add constraint ck_dictionaries_scope check(scope in ('global', 'organization', 'entity'))")
    op.execute("alter table dictionaries add constraint ck_dictionaries_revision check(revision > 0)")
    op.execute("create unique index uq_dictionaries_global_code on dictionaries(code) where scope = 'global'")
    op.execute("create unique index uq_dictionaries_organization_code on dictionaries(owner_organization_id, code) where scope = 'organization'")
    op.execute("create unique index uq_dictionaries_global_name on dictionaries(lower(btrim(name))) where scope = 'global'")
    op.execute("create unique index uq_dictionaries_organization_name on dictionaries(owner_organization_id, lower(btrim(name))) where scope = 'organization'")
    op.execute("create unique index uq_dictionaries_entity_name on dictionaries(entity_schema_id, lower(btrim(name))) where scope = 'entity'")
    op.execute("create index ix_dictionaries_scope_active on dictionaries(scope, active)")

    op.execute("alter table entity_fields add column unit_code varchar(64)")
    op.execute("alter table entity_fields add column decimal_scale integer")
    op.execute("alter table entity_fields add constraint ck_entity_fields_decimal_scale check(decimal_scale is null or decimal_scale between 0 and 12)")
    op.execute("alter table entity_map_styles add column marker_icon varchar(120)")
    op.execute("alter table attachments add column entity_field_id uuid references entity_fields(id) on delete set null")
    op.execute("create index ix_attachments_field_created on attachments(entity_field_id, created_at desc) where entity_field_id is not null")

    op.execute(
        """
        create table entity_schema_drafts (
          id uuid primary key default gen_random_uuid(),
          entity_schema_id uuid not null unique references entity_schemas(id) on delete cascade,
          base_version integer not null,
          snapshot jsonb not null,
          updated_by uuid,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          constraint ck_entity_schema_draft_base_version check(base_version > 0)
        )
        """
    )
    op.execute("create index ix_entity_schema_drafts_updated on entity_schema_drafts(updated_at desc)")

    op.execute(
        """
        create table excel_import_plans (
          id uuid primary key default gen_random_uuid(),
          entity_schema_id uuid not null references entity_schemas(id) on delete cascade,
          schema_version_id uuid not null references entity_schema_versions(id) on delete restrict,
          created_by uuid not null,
          status varchar(24) not null default 'draft',
          source_name varchar(1024) not null,
          source_object_key varchar(1024),
          mapping jsonb not null default '{}'::jsonb,
          summary jsonb not null default '{}'::jsonb,
          rows jsonb not null default '[]'::jsonb,
          decisions jsonb not null default '{}'::jsonb,
          expires_at timestamptz not null,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          constraint ck_excel_import_plan_status check(status in ('draft', 'ready', 'applied', 'rejected', 'expired'))
        )
        """
    )
    op.execute("create index ix_excel_import_plans_owner_status on excel_import_plans(created_by, status, created_at desc)")

    op.execute(
        """
        create table notifications (
          id uuid primary key default gen_random_uuid(),
          user_id uuid not null,
          type varchar(120) not null,
          title varchar(500) not null,
          message text not null,
          resource_type varchar(80),
          resource_id uuid,
          metadata jsonb not null default '{}'::jsonb,
          read_at timestamptz,
          created_at timestamptz not null default now()
        )
        """
    )
    op.execute("create index ix_notifications_user_read_created on notifications(user_id, read_at, created_at desc)")
    op.execute("alter table change_set_items add column parent_object_id uuid references entity_objects(id) on delete restrict")


def downgrade() -> None:
    """Удалить добавленные контракты."""

    op.execute("alter table change_set_items drop column if exists parent_object_id")
    op.execute("drop table if exists notifications")
    op.execute("drop table if exists excel_import_plans")
    op.execute("drop table if exists entity_schema_drafts")
    op.execute("drop index if exists ix_attachments_field_created")
    op.execute("alter table attachments drop column if exists entity_field_id")
    op.execute("alter table entity_map_styles drop column if exists marker_icon")
    op.execute("alter table entity_fields drop constraint if exists ck_entity_fields_decimal_scale")
    op.execute("alter table entity_fields drop column if exists decimal_scale")
    op.execute("alter table entity_fields drop column if exists unit_code")
    op.execute("drop index if exists ix_dictionaries_scope_active")
    op.execute("drop index if exists uq_dictionaries_organization_code")
    op.execute("drop index if exists uq_dictionaries_global_code")
    op.execute("drop index if exists uq_dictionaries_entity_name")
    op.execute("drop index if exists uq_dictionaries_organization_name")
    op.execute("drop index if exists uq_dictionaries_global_name")
    op.execute("alter table dictionaries drop constraint if exists ck_dictionaries_revision")
    op.execute("alter table dictionaries drop constraint if exists ck_dictionaries_scope")
    op.execute("alter table dictionaries drop column if exists revision")
    op.execute("alter table dictionaries drop column if exists owner_organization_id")
    op.execute("alter table dictionaries drop column if exists scope")
    op.execute("alter table dictionaries alter column entity_schema_id set not null")
