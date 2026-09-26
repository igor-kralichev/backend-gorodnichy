"""Расширяет статусы фонового импорта объектов.

Revision ID: 20260829_0009
Revises: 20260828_0008
Create Date: 2026-08-29 00:09:00.000000
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260829_0009"
down_revision: str | None = "20260828_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавить статусы paused и cancelled для управления импортом."""

    op.execute("alter table import_jobs drop constraint if exists import_jobs_status_check")
    op.execute("alter table import_jobs drop constraint if exists status")
    op.execute(
        """
        alter table import_jobs
        add constraint import_jobs_status_check
        check (status in ('queued', 'running', 'paused', 'completed', 'failed', 'cancelled'))
        """
    )


def downgrade() -> None:
    """Вернуть исходный набор статусов импорта."""

    op.execute("update import_jobs set status = 'failed' where status in ('paused', 'cancelled')")
    op.execute("alter table import_jobs drop constraint if exists import_jobs_status_check")
    op.execute(
        """
        alter table import_jobs
        add constraint import_jobs_status_check
        check (status in ('queued', 'running', 'completed', 'failed'))
        """
    )
