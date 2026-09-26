"""Единая история изменений платформы.

Идентификатор ревизии: 20260824_0004
Предыдущая ревизия: 20260823_0003
Дата создания: 2026-08-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260824_0004"
down_revision: str | None = "20260823_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        create table audit_events (
          id uuid primary key default gen_random_uuid(),
          resource_type varchar(80) not null,
          resource_id uuid,
          resource_code varchar(160),
          resource_name varchar(500),
          action varchar(120) not null,
          actor_id uuid,
          actor_full_name varchar(500),
          actor_email varchar(320),
          occurred_at timestamptz not null default now(),
          old_value jsonb,
          new_value jsonb,
          changes jsonb not null default '[]'::jsonb,
          metadata jsonb not null default '{}'::jsonb
        )
        """
    )
    op.execute(
        "create index ix_audit_events_resource_occurred "
        "on audit_events (resource_type, resource_id, occurred_at desc)"
    )
    op.execute(
        "create index ix_audit_events_actor_occurred "
        "on audit_events (actor_id, occurred_at desc)"
    )
    op.execute(
        "create index ix_audit_events_action_occurred "
        "on audit_events (action, occurred_at desc)"
    )


def downgrade() -> None:
    op.execute("drop table if exists audit_events")
