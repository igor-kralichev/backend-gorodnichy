"""Начальная схема low-code платформы с PostGIS и transactional outbox.

Идентификатор ревизии: 20260822_0001
Предыдущая ревизия: отсутствует
Дата создания: 2026-08-22
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260822_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("create extension if not exists pgcrypto")
    op.execute("create extension if not exists postgis")
    op.execute(
        """
        create function set_updated_at() returns trigger as $$
        begin
          new.updated_at = now();
          return new;
        end;
        $$ language plpgsql
        """
    )

    op.execute(
        """
        create table municipalities (
          id uuid primary key default gen_random_uuid(),
          code varchar(120) not null unique,
          name varchar(255) not null,
          map_center geometry(Point, 4326),
          map_zoom integer not null default 12 check (map_zoom between 1 and 22),
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now()
        )
        """
    )
    op.execute("create index ix_municipalities_map_center_gist on municipalities using gist (map_center)")

    op.execute(
        """
        create table entity_schemas (
          id uuid primary key default gen_random_uuid(),
          scope_municipality_id uuid references municipalities(id) on delete restrict,
          code varchar(120) not null unique,
          name varchar(255) not null,
          description text,
          geometry_type varchar(20) not null default 'none'
            check (geometry_type in ('none', 'point', 'lineString', 'polygon')),
          clustering_enabled boolean not null default false,
          status varchar(20) not null default 'draft'
            check (status in ('draft', 'active', 'archived')),
          current_version integer not null default 1 check (current_version > 0),
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now()
        )
        """
    )
    op.execute("create index ix_entity_schemas_status_updated_at on entity_schemas (status, updated_at desc)")
    op.execute(
        "create index ix_entity_schemas_scope on entity_schemas "
        "(scope_municipality_id) where scope_municipality_id is not null"
    )

    op.execute(
        """
        create table entity_schema_versions (
          id uuid primary key default gen_random_uuid(),
          entity_schema_id uuid not null references entity_schemas(id) on delete cascade,
          version integer not null check (version > 0),
          snapshot jsonb not null,
          created_by uuid,
          created_at timestamptz not null default now(),
          constraint uq_entity_schema_versions_entity_schema_version unique (entity_schema_id, version)
        )
        """
    )
    op.execute(
        "create index ix_entity_schema_versions_schema_created "
        "on entity_schema_versions (entity_schema_id, created_at desc)"
    )

    op.execute(
        """
        create table entity_allowed_geometry_types (
          id uuid primary key default gen_random_uuid(),
          entity_schema_id uuid not null references entity_schemas(id) on delete cascade,
          geometry_type varchar(20) not null check (geometry_type in ('point', 'lineString', 'polygon')),
          sort_order integer not null check (sort_order > 0),
          constraint uq_entity_allowed_geometry_type unique (entity_schema_id, geometry_type),
          constraint uq_entity_allowed_geometry_order unique (entity_schema_id, sort_order)
        )
        """
    )

    op.execute(
        """
        create table entity_map_styles (
          id uuid primary key default gen_random_uuid(),
          entity_schema_id uuid not null references entity_schemas(id) on delete cascade,
          geometry_type varchar(20) not null check (geometry_type in ('point', 'lineString', 'polygon')),
          fill varchar(20) not null,
          stroke varchar(20) not null,
          stroke_width numeric(5,2) not null check (stroke_width > 0),
          point_size numeric(6,2) not null check (point_size > 0),
          opacity numeric(4,3) not null check (opacity between 0 and 1),
          constraint uq_entity_map_style_geometry unique (entity_schema_id, geometry_type)
        )
        """
    )

    op.execute(
        """
        create table dictionaries (
          id uuid primary key default gen_random_uuid(),
          entity_schema_id uuid not null references entity_schemas(id) on delete cascade,
          code varchar(120) not null,
          name varchar(255) not null,
          active boolean not null default true,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          constraint uq_dictionary_entity_code unique (entity_schema_id, code)
        )
        """
    )
    op.execute("create index ix_dictionaries_entity_active on dictionaries (entity_schema_id, active)")

    op.execute(
        """
        create table entity_fields (
          id uuid primary key default gen_random_uuid(),
          entity_schema_id uuid not null references entity_schemas(id) on delete cascade,
          code varchar(120) not null,
          name varchar(255) not null,
          field_type varchar(24) not null check (
            field_type in ('string', 'text', 'integer', 'decimal', 'boolean', 'date', 'datetime',
                           'address', 'enum', 'reference', 'file')
          ),
          required boolean not null default false,
          list_visible boolean not null default true,
          card_visible boolean not null default true,
          searchable boolean not null default true,
          filterable boolean not null default true,
          sort_order integer not null check (sort_order > 0),
          dictionary_id uuid references dictionaries(id) on delete restrict,
          reference_entity_schema_id uuid references entity_schemas(id) on delete restrict,
          constraint uq_entity_field_code unique (entity_schema_id, code),
          constraint uq_entity_field_order unique (entity_schema_id, sort_order),
          constraint ck_entity_field_reference check (
            (field_type = 'enum' and dictionary_id is not null and reference_entity_schema_id is null)
            or (field_type = 'reference' and reference_entity_schema_id is not null and dictionary_id is null)
            or (
              field_type not in ('enum', 'reference')
              and dictionary_id is null
              and reference_entity_schema_id is null
            )
          )
        )
        """
    )
    op.execute("create index ix_entity_fields_schema_searchable on entity_fields (entity_schema_id) where searchable")
    op.execute("create index ix_entity_fields_schema_filterable on entity_fields (entity_schema_id) where filterable")

    op.execute(
        """
        create table dictionary_items (
          id uuid primary key default gen_random_uuid(),
          dictionary_id uuid not null references dictionaries(id) on delete cascade,
          code varchar(120) not null,
          name varchar(500) not null,
          active boolean not null default true,
          sort_order integer not null default 1 check (sort_order > 0),
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          constraint uq_dictionary_item_code unique (dictionary_id, code)
        )
        """
    )
    op.execute(
        "create index ix_dictionary_items_dictionary_active_name "
        "on dictionary_items (dictionary_id, active, name)"
    )

    op.execute(
        """
        create table entity_map_color_rules (
          id uuid primary key default gen_random_uuid(),
          entity_schema_id uuid not null references entity_schemas(id) on delete cascade,
          entity_field_id uuid not null references entity_fields(id) on delete cascade,
          name varchar(255) not null,
          operator varchar(24) not null
            check (operator in ('equals', 'notEquals', 'contains', 'filled', 'empty', 'before', 'after')),
          value text not null default '',
          color varchar(20) not null,
          sort_order integer not null check (sort_order > 0)
        )
        """
    )
    op.execute(
        "create index ix_entity_map_color_rules_schema_order "
        "on entity_map_color_rules (entity_schema_id, sort_order)"
    )

    op.execute(
        """
        create table entity_objects (
          id uuid primary key default gen_random_uuid(),
          entity_schema_id uuid not null references entity_schemas(id) on delete restrict,
          municipality_id uuid references municipalities(id) on delete restrict,
          values jsonb not null default '{}'::jsonb,
          geometry geometry(Geometry, 4326),
          status varchar(20) not null default 'draft'
            check (status in ('draft', 'published', 'archived')),
          data_quality varchar(20) not null default 'incomplete'
            check (data_quality in ('complete', 'incomplete')),
          validation_errors jsonb not null default '[]'::jsonb,
          revision integer not null default 1 check (revision > 0),
          created_by uuid,
          updated_by uuid,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now()
        )
        """
    )
    op.execute(
        "create index ix_entity_objects_entity_status_updated "
        "on entity_objects (entity_schema_id, status, updated_at desc)"
    )
    op.execute("create index ix_entity_objects_created_by on entity_objects (created_by)")
    op.execute("create index ix_entity_objects_values_gin on entity_objects using gin (values jsonb_path_ops)")
    op.execute("create index ix_entity_objects_geometry_gist on entity_objects using gist (geometry)")

    op.execute(
        """
        create table object_events (
          id uuid primary key default gen_random_uuid(),
          entity_schema_id uuid not null references entity_schemas(id) on delete restrict,
          object_id uuid not null references entity_objects(id) on delete cascade,
          revision integer not null check (revision > 0),
          event_type varchar(80) not null,
          actor_id uuid,
          occurred_at timestamptz not null default now(),
          before_values jsonb,
          after_values jsonb,
          changes jsonb not null default '[]'::jsonb,
          metadata jsonb not null default '{}'::jsonb,
          constraint uq_object_event_revision unique (object_id, revision)
        )
        """
    )
    op.execute("create index ix_object_events_object_occurred on object_events (object_id, occurred_at desc)")
    op.execute("create index ix_object_events_entity_occurred on object_events (entity_schema_id, occurred_at desc)")

    op.execute(
        """
        create table attachments (
          id uuid primary key default gen_random_uuid(),
          entity_schema_id uuid not null references entity_schemas(id) on delete restrict,
          object_id uuid not null references entity_objects(id) on delete cascade,
          kind varchar(20) not null check (kind in ('photo', 'document')),
          original_name varchar(1024) not null,
          object_key varchar(1024) not null unique,
          mime_type varchar(255) not null,
          size_bytes bigint not null check (size_bytes >= 0),
          checksum_sha256 varchar(64) not null,
          uploaded_by uuid,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now()
        )
        """
    )
    op.execute("create index ix_attachments_object_kind_created on attachments (object_id, kind, created_at desc)")

    op.execute(
        """
        create table import_jobs (
          id uuid primary key default gen_random_uuid(),
          entity_schema_id uuid not null references entity_schemas(id) on delete restrict,
          status varchar(20) not null default 'queued'
            check (status in ('queued', 'running', 'completed', 'failed')),
          source_name varchar(1024) not null,
          source_object_key varchar(1024),
          mapping jsonb not null default '{}'::jsonb,
          total_rows integer not null default 0 check (total_rows >= 0),
          processed_rows integer not null default 0 check (processed_rows >= 0),
          error_rows integer not null default 0 check (error_rows >= 0),
          created_by uuid,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now()
        )
        """
    )
    op.execute(
        "create index ix_import_jobs_entity_status_created "
        "on import_jobs (entity_schema_id, status, created_at desc)"
    )

    op.execute(
        """
        create table import_rows (
          id uuid primary key default gen_random_uuid(),
          import_job_id uuid not null references import_jobs(id) on delete cascade,
          object_id uuid references entity_objects(id) on delete set null,
          row_number integer not null check (row_number > 0),
          status varchar(20) not null default 'pending'
            check (status in ('pending', 'imported', 'incomplete', 'failed')),
          raw_values jsonb not null default '{}'::jsonb,
          normalized_values jsonb not null default '{}'::jsonb,
          errors jsonb not null default '[]'::jsonb,
          constraint uq_import_row_number unique (import_job_id, row_number)
        )
        """
    )
    op.execute("create index ix_import_rows_job_status on import_rows (import_job_id, status)")

    op.execute(
        """
        create table outbox_events (
          id uuid primary key default gen_random_uuid(),
          aggregate_type varchar(120) not null,
          aggregate_id uuid not null,
          event_type varchar(160) not null,
          payload jsonb not null,
          occurred_at timestamptz not null default now(),
          published_at timestamptz,
          attempts integer not null default 0 check (attempts >= 0),
          last_error text
        )
        """
    )
    op.execute(
        "create index ix_outbox_events_unpublished "
        "on outbox_events (published_at, occurred_at) "
        "where published_at is null"
    )
    op.execute("create index ix_outbox_events_aggregate on outbox_events (aggregate_type, aggregate_id)")

    for table in (
        "municipalities",
        "entity_schemas",
        "dictionaries",
        "dictionary_items",
        "entity_objects",
        "attachments",
        "import_jobs",
    ):
        op.execute(
            f"create trigger trg_{table}_updated_at before update on {table} "
            "for each row execute function set_updated_at()"
        )


def downgrade() -> None:
    for table in (
        "import_jobs",
        "attachments",
        "entity_objects",
        "dictionary_items",
        "dictionaries",
        "entity_schemas",
        "municipalities",
    ):
        op.execute(f"drop trigger if exists trg_{table}_updated_at on {table}")

    op.execute("drop table if exists outbox_events")
    op.execute("drop table if exists import_rows")
    op.execute("drop table if exists import_jobs")
    op.execute("drop table if exists attachments")
    op.execute("drop table if exists object_events")
    op.execute("drop table if exists entity_objects")
    op.execute("drop table if exists entity_map_color_rules")
    op.execute("drop table if exists dictionary_items")
    op.execute("drop table if exists entity_fields")
    op.execute("drop table if exists dictionaries")
    op.execute("drop table if exists entity_map_styles")
    op.execute("drop table if exists entity_allowed_geometry_types")
    op.execute("drop table if exists entity_schema_versions")
    op.execute("drop table if exists entity_schemas")
    op.execute("drop table if exists municipalities")
    op.execute("drop function if exists set_updated_at()")
