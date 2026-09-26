"""Массив путей файлов в объекте.

Идентификатор ревизии: 20260826_0006
Предыдущая ревизия: 20260825_0005
Дата создания: 2026-08-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260826_0006"
down_revision: str | None = "20260825_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("alter table entity_objects add column attachment_paths jsonb not null default '[]'::jsonb")
    op.execute(
        """
        update entity_objects object
        set attachment_paths = coalesce(paths.items, '[]'::jsonb)
        from (
          select object_id, jsonb_agg(object_key order by created_at desc) as items
          from attachments
          group by object_id
        ) paths
        where object.id = paths.object_id
        """
    )


def downgrade() -> None:
    op.execute("alter table entity_objects drop column if exists attachment_paths")
