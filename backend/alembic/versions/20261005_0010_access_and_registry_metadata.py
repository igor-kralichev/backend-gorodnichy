"""Добавляет организации, права и расширенные метаданные реестров.

Revision ID: 20261005_0010
Revises: 20260829_0009
Create Date: 2026-10-05 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261005_0010"
down_revision: str | None = "20260829_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Создать организационный контур и расширить схему динамических полей."""

    op.execute(
        """
        create table organizations (
          id uuid primary key default gen_random_uuid(),
          parent_id uuid references organizations(id) on delete restrict,
          code varchar(120) not null unique,
          name varchar(500) not null,
          active boolean not null default true,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now()
        )
        """
    )
    op.execute("create index ix_organizations_parent on organizations(parent_id) where parent_id is not null")
    op.execute("create index ix_organizations_active_name on organizations(active, name)")

    op.execute(
        """
        create table memberships (
          id uuid primary key default gen_random_uuid(),
          user_id uuid not null,
          organization_id uuid not null references organizations(id) on delete cascade,
          role_code varchar(120) not null,
          active boolean not null default true,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          constraint uq_membership_user_organization_role unique(user_id, organization_id, role_code)
        )
        """
    )
    op.execute("create index ix_memberships_user_active on memberships(user_id, active)")
    op.execute("create index ix_memberships_organization_active on memberships(organization_id, active)")

    op.execute("alter table entity_schemas add column owner_organization_id uuid references organizations(id) on delete restrict")
    op.execute(
        "create index ix_entity_schemas_owner_organization on entity_schemas(owner_organization_id) "
        "where owner_organization_id is not null"
    )

    op.execute("alter table entity_fields drop constraint if exists entity_fields_field_type_check")
    op.execute("alter table entity_fields drop constraint if exists field_type")
    op.execute(
        """
        alter table entity_fields add constraint entity_fields_field_type_check check (
          field_type in (
            'string', 'text', 'integer', 'decimal', 'boolean', 'date', 'datetime',
            'address', 'enum', 'reference', 'file', 'phone', 'email', 'url', 'calculated'
          )
        )
        """
    )
    op.execute("alter table entity_fields add column hint text")
    op.execute("alter table entity_fields add column default_value jsonb")
    op.execute("alter table entity_fields add column group_name varchar(255)")
    op.execute("alter table entity_fields add column min_length integer")
    op.execute("alter table entity_fields add column max_length integer")
    op.execute("alter table entity_fields add column min_value numeric")
    op.execute("alter table entity_fields add column max_value numeric")
    op.execute("alter table entity_fields add column unique_value boolean not null default false")
    op.execute("alter table entity_fields add column multiple boolean not null default false")
    op.execute("alter table entity_fields add column read_only boolean not null default false")
    op.execute("alter table entity_fields add column archived boolean not null default false")
    op.execute("alter table entity_fields add column access_rules jsonb not null default '{}'::jsonb")
    op.execute("alter table entity_fields add column formula jsonb")
    op.execute(
        """
        alter table entity_fields add constraint ck_entity_field_lengths check (
          (min_length is null or min_length >= 0)
          and (max_length is null or max_length >= 0)
          and (min_length is null or max_length is null or min_length <= max_length)
        )
        """
    )
    op.execute(
        """
        alter table entity_fields add constraint ck_entity_field_range check (
          min_value is null or max_value is null or min_value <= max_value
        )
        """
    )
    op.execute(
        """
        alter table entity_fields add constraint ck_entity_field_multiple check (
          not multiple or field_type in ('enum', 'reference', 'file')
        )
        """
    )
    op.execute(
        """
        alter table entity_fields add constraint ck_entity_field_formula check (
          (field_type = 'calculated' and formula is not null and read_only)
          or (field_type <> 'calculated' and formula is null)
        )
        """
    )

    op.execute("alter table entity_objects add column schema_version_id uuid references entity_schema_versions(id) on delete restrict")
    op.execute(
        """
        insert into entity_schema_versions (entity_schema_id, version, snapshot)
        select entity.id, entity.current_version, jsonb_build_object(
          'id', entity.id,
          'code', entity.code,
          'name', entity.name,
          'description', entity.description,
          'geometryType', entity.geometry_type,
          'fields', '[]'::jsonb
        )
        from entity_schemas entity
        where not exists (
          select 1 from entity_schema_versions version
          where version.entity_schema_id = entity.id
        )
        """
    )
    op.execute(
        """
        update entity_objects object
        set schema_version_id = (
          select schema_version.id
          from entity_schema_versions schema_version
          where schema_version.entity_schema_id = object.entity_schema_id
          order by schema_version.version desc
          limit 1
        )
        """
    )
    op.execute("alter table entity_objects alter column schema_version_id set not null")
    op.execute("alter table entity_objects add column owner_organization_id uuid references organizations(id) on delete restrict")
    op.execute("alter table entity_objects add column responsible_id uuid")
    op.execute("alter table entity_objects add column archived_at timestamptz")
    op.execute("create index ix_entity_objects_schema_version on entity_objects(schema_version_id)")
    op.execute(
        "create index ix_entity_objects_owner_status on entity_objects(owner_organization_id, status) "
        "where owner_organization_id is not null"
    )
    op.execute(
        "create index ix_entity_objects_responsible_status on entity_objects(responsible_id, status) "
        "where responsible_id is not null"
    )

    op.execute(
        """
        create table permission_grants (
          id uuid primary key default gen_random_uuid(),
          user_id uuid,
          role_code varchar(120),
          organization_id uuid references organizations(id) on delete cascade,
          entity_schema_id uuid references entity_schemas(id) on delete cascade,
          entity_field_id uuid references entity_fields(id) on delete cascade,
          entity_object_id uuid references entity_objects(id) on delete cascade,
          actions jsonb not null default '[]'::jsonb,
          conditions jsonb not null default '{}'::jsonb,
          created_by uuid,
          created_at timestamptz not null default now(),
          constraint ck_permission_grant_subject check (user_id is not null or role_code is not null),
          constraint ck_permission_grant_scope check (
            entity_field_id is null or entity_schema_id is not null
          )
        )
        """
    )
    op.execute("create index ix_permission_grants_user on permission_grants(user_id) where user_id is not null")
    op.execute("create index ix_permission_grants_role on permission_grants(role_code) where role_code is not null")
    op.execute("create index ix_permission_grants_entity on permission_grants(entity_schema_id) where entity_schema_id is not null")
    op.execute("create index ix_permission_grants_object on permission_grants(entity_object_id) where entity_object_id is not null")

    op.execute(
        """
        create table saved_views (
          id uuid primary key default gen_random_uuid(),
          entity_schema_id uuid not null references entity_schemas(id) on delete cascade,
          owner_id uuid not null,
          name varchar(255) not null,
          configuration jsonb not null default '{}'::jsonb,
          shared boolean not null default false,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          constraint uq_saved_view_owner_name unique(entity_schema_id, owner_id, name)
        )
        """
    )
    op.execute("create index ix_saved_views_entity_owner on saved_views(entity_schema_id, owner_id)")

    op.execute(
        """
        create table entity_unique_values (
          id uuid primary key default gen_random_uuid(),
          entity_schema_id uuid not null references entity_schemas(id) on delete cascade,
          entity_field_id uuid not null references entity_fields(id) on delete cascade,
          entity_object_id uuid not null references entity_objects(id) on delete cascade,
          normalized_value text not null,
          constraint uq_entity_unique_field_value unique(entity_field_id, normalized_value),
          constraint uq_entity_unique_object_field unique(entity_object_id, entity_field_id)
        )
        """
    )
    op.execute("create index ix_entity_unique_values_entity on entity_unique_values(entity_schema_id)")


def downgrade() -> None:
    """Удалить организационный контур и расширенные метаданные."""

    op.execute("drop table if exists entity_unique_values")
    op.execute("drop table if exists saved_views")
    op.execute("drop table if exists permission_grants")
    op.execute("alter table entity_objects drop column if exists archived_at")
    op.execute("alter table entity_objects drop column if exists responsible_id")
    op.execute("alter table entity_objects drop column if exists owner_organization_id")
    op.execute("alter table entity_objects drop column if exists schema_version_id")
    for column in (
        "formula",
        "access_rules",
        "archived",
        "read_only",
        "multiple",
        "unique_value",
        "max_value",
        "min_value",
        "max_length",
        "min_length",
        "group_name",
        "default_value",
        "hint",
    ):
        op.execute(f"alter table entity_fields drop column if exists {column}")
    op.execute("alter table entity_schemas drop column if exists owner_organization_id")
    op.execute("drop table if exists memberships")
    op.execute("drop table if exists organizations")
