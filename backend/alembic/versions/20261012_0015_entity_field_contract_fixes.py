"""Исправляет хранение формул и контракт чтения полей сущности.

Revision ID: 20261012_0015
Revises: 20261009_0014
Create Date: 2026-10-12 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261012_0015"
down_revision: str | None = "20261009_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Заменить JSON null в формулах обычных полей на SQL NULL."""

    op.execute(
        """
        update entity_fields
        set formula = null
        where formula = 'null'::jsonb
        """
    )


def downgrade() -> None:
    """Не возвращать некорректное представление JSON null."""

    # SQL NULL корректен для прежней и новой версии приложения, поэтому
    # обратная миграция данных не требуется.
    pass
