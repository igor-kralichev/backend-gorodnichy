"""Добавляет долговечные задачи фонового XLSX-экспорта.

Revision ID: 20261009_0014
Revises: 20261008_0013
Create Date: 2026-10-09 15:10:00.000000
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261009_0014"
down_revision: str | None = "20261008_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Создать очередь задач и хранить ссылку на готовый XLSX в MinIO."""

    op.execute(
        """
        create table excel_export_jobs (
          id uuid primary key default gen_random_uuid(),
          entity_schema_id uuid not null references entity_schemas(id) on delete restrict,
          status varchar(20) not null default 'queued',
          request_payload jsonb not null default '{}'::jsonb,
          result_object_key varchar(1024),
          result_filename varchar(1024),
          total_rows integer not null default 0,
          error_message text,
          created_by uuid not null,
          expires_at timestamptz not null,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          constraint ck_excel_export_jobs_status
            check(status in ('queued', 'running', 'completed', 'failed', 'cancelled')),
          constraint ck_excel_export_jobs_total_rows check(total_rows >= 0)
        )
        """
    )
    op.execute(
        "create index ix_excel_export_jobs_owner_status_created "
        "on excel_export_jobs(created_by, status, created_at desc)"
    )
    op.execute(
        "create index ix_excel_export_jobs_entity_created "
        "on excel_export_jobs(entity_schema_id, created_at desc)"
    )


def downgrade() -> None:
    """Удалить задачи фонового экспорта."""

    op.execute("drop table if exists excel_export_jobs")
