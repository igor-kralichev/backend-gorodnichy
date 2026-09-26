"""Нормализованное название элемента справочника.

Идентификатор ревизии: 20260825_0005
Предыдущая ревизия: 20260824_0004
Дата создания: 2026-08-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260825_0005"
down_revision: str | None = "20260824_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("alter table dictionary_items add column normalized_name varchar(500)")
    op.execute(
        """
        with normalized as (
          select
            id,
            lower(btrim(name)) as base_name,
            row_number() over (
              partition by dictionary_id, lower(btrim(name))
              order by created_at, id
            ) as duplicate_number
          from dictionary_items
        )
        update dictionary_items item
        set normalized_name = case
          when normalized.duplicate_number = 1 then normalized.base_name
          else normalized.base_name || '#' || item.id::text
        end
        from normalized
        where item.id = normalized.id
        """
    )
    op.execute("alter table dictionary_items alter column normalized_name set not null")
    op.execute(
        "alter table dictionary_items "
        "add constraint dictionary_item_normalized_name unique (dictionary_id, normalized_name)"
    )


def downgrade() -> None:
    op.execute("alter table dictionary_items drop constraint if exists dictionary_item_normalized_name")
    op.execute("alter table dictionary_items drop column if exists normalized_name")
