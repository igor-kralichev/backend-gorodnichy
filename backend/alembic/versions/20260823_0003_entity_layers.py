"""Добавить настройки и материализованные слои сущностей.

Идентификатор ревизии: 20260823_0003
Предыдущая ревизия: 20260822_0002
Дата создания: 2026-08-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260823_0003"
down_revision: str | None = "20260822_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "alter table entity_schemas "
        "add column layer_selectable boolean not null default true"
    )
    op.execute(
        "alter table entity_schemas "
        "add column layer_visible_by_default boolean not null default true"
    )
    op.execute(
        """
        create table entity_layers (
          id uuid primary key default gen_random_uuid(),
          entity_schema_id uuid not null
            references entity_schemas(id) on delete cascade,
          code varchar(120) not null,
          name varchar(255) not null,
          style jsonb not null default '{}'::jsonb,
          opacity numeric(4,3) not null,
          selectable boolean not null default true,
          visible_by_default boolean not null default true,
          active boolean not null default true,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now()
          ,constraint uq_entity_layers_entity_schema_id unique (entity_schema_id)
          ,constraint uq_entity_layers_code unique (code)
          ,constraint ck_entity_layers_valid_opacity
            check (opacity >= 0 and opacity <= 1)
        )
        """
    )
    op.execute(
        "create index ix_entity_layers_active_visible "
        "on entity_layers (active, visible_by_default)"
    )
    op.execute(
        "create trigger trg_entity_layers_updated_at "
        "before update on entity_layers "
        "for each row execute function set_updated_at()"
    )


def downgrade() -> None:
    op.execute("drop trigger if exists trg_entity_layers_updated_at on entity_layers")
    op.execute("drop table if exists entity_layers")
    op.execute(
        "alter table entity_schemas drop column if exists layer_visible_by_default"
    )
    op.execute("alter table entity_schemas drop column if exists layer_selectable")
