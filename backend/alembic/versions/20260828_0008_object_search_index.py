"""Добавляет read-model для быстрых подсказок по объектам.

Revision ID: 20260828_0008
Revises: 20260827_0007
Create Date: 2026-08-28 00:08:00.000000
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260828_0008"
down_revision: str | None = "20260827_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Создать таблицу поискового индекса объектов и trigram-индекс."""

    op.execute("create extension if not exists pg_trgm")
    op.execute(
        """
        create table object_search_index (
          id uuid primary key default gen_random_uuid(),
          entity_schema_id uuid not null references entity_schemas(id) on delete cascade,
          object_id uuid not null references entity_objects(id) on delete cascade,
          field_code varchar(120) not null,
          value text not null,
          normalized_value text not null,
          constraint object_search_index_object_field_value
            unique (object_id, field_code, value)
        )
        """
    )
    op.execute(
        "create index ix_object_search_index_entity_field "
        "on object_search_index (entity_schema_id, field_code)"
    )
    op.execute(
        "create index ix_object_search_index_object "
        "on object_search_index (object_id)"
    )
    op.execute(
        "create index ix_object_search_index_normalized_trgm "
        "on object_search_index using gin (normalized_value gin_trgm_ops)"
    )


def downgrade() -> None:
    """Удалить read-model подсказок по объектам."""

    op.execute("drop index if exists ix_object_search_index_normalized_trgm")
    op.execute("drop index if exists ix_object_search_index_object")
    op.execute("drop index if exists ix_object_search_index_entity_field")
    op.execute("drop table if exists object_search_index")
