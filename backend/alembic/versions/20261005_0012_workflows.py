"""Добавляет формы, сборы, актуализацию, поручения и обмен.

Revision ID: 20261005_0012
Revises: 20261005_0011
Create Date: 2026-10-05 14:00:00.000000
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261005_0012"
down_revision: str | None = "20261005_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Создать таблицы предметных процессов."""

    op.execute(
        """
        create table form_definitions (
          id uuid primary key default gen_random_uuid(),
          entity_schema_id uuid not null references entity_schemas(id) on delete restrict,
          owner_organization_id uuid references organizations(id) on delete restrict,
          name varchar(500) not null,
          description text,
          status varchar(20) not null default 'draft',
          current_version integer not null default 1,
          definition jsonb not null default '{}'::jsonb,
          created_by uuid,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          constraint ck_form_definition_status check(status in ('draft', 'published', 'archived')),
          constraint ck_form_definition_version check(current_version > 0)
        )
        """
    )
    op.execute("create index ix_form_definitions_entity_status on form_definitions(entity_schema_id, status)")
    op.execute(
        """
        create table form_versions (
          id uuid primary key default gen_random_uuid(),
          form_id uuid not null references form_definitions(id) on delete cascade,
          schema_version_id uuid not null references entity_schema_versions(id) on delete restrict,
          version integer not null,
          snapshot jsonb not null,
          created_by uuid,
          created_at timestamptz not null default now(),
          constraint uq_form_version unique(form_id, version)
        )
        """
    )

    op.execute(
        """
        create table information_requests (
          id uuid primary key default gen_random_uuid(),
          request_type varchar(24) not null,
          entity_schema_id uuid not null references entity_schemas(id) on delete restrict,
          form_id uuid not null references form_definitions(id) on delete restrict,
          form_version_id uuid references form_versions(id) on delete restrict,
          owner_organization_id uuid references organizations(id) on delete restrict,
          name varchar(500) not null,
          description text,
          period_start date,
          period_end date,
          due_at timestamptz,
          reviewer_id uuid,
          selected_fields jsonb not null default '[]'::jsonb,
          correction_rules jsonb not null default '{}'::jsonb,
          status varchar(20) not null default 'draft',
          created_by uuid,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          constraint ck_information_request_type check(request_type in ('collection', 'actualization')),
          constraint ck_information_request_status check(status in ('draft', 'open', 'closed', 'cancelled')),
          constraint ck_information_request_period check(period_start is null or period_end is null or period_start <= period_end)
        )
        """
    )
    op.execute("create index ix_information_requests_type_status on information_requests(request_type, status, due_at)")
    op.execute(
        """
        create table request_recipients (
          id uuid primary key default gen_random_uuid(),
          request_id uuid not null references information_requests(id) on delete cascade,
          organization_id uuid not null references organizations(id) on delete restrict,
          object_ids jsonb not null default '[]'::jsonb,
          field_codes jsonb not null default '[]'::jsonb,
          contact_email varchar(320),
          status varchar(24) not null default 'not_started',
          reopened_until timestamptz,
          reopen_reason text,
          last_submission_id uuid,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          constraint uq_request_recipient unique(request_id, organization_id),
          constraint ck_request_recipient_status check(status in ('not_started', 'draft', 'review', 'rework', 'accepted'))
        )
        """
    )
    op.execute("create index ix_request_recipients_request_status on request_recipients(request_id, status)")
    op.execute(
        """
        create table form_access_links (
          id uuid primary key default gen_random_uuid(),
          recipient_id uuid not null references request_recipients(id) on delete cascade,
          token_hash varchar(64) not null unique,
          requires_otp boolean not null default true,
          allowed_actions jsonb not null default '[\"read\", \"save_draft\", \"submit\"]'::jsonb,
          expires_at timestamptz not null,
          revoked_at timestamptz,
          created_by uuid,
          created_at timestamptz not null default now()
        )
        """
    )
    op.execute("create index ix_form_access_links_recipient_active on form_access_links(recipient_id, expires_at) where revoked_at is null")
    op.execute(
        """
        create table form_submissions (
          id uuid primary key default gen_random_uuid(),
          recipient_id uuid not null references request_recipients(id) on delete cascade,
          form_version_id uuid not null references form_versions(id) on delete restrict,
          version integer not null,
          status varchar(24) not null default 'draft',
          payload jsonb not null default '{}'::jsonb,
          source_snapshot jsonb not null default '{}'::jsonb,
          change_set_id uuid references change_sets(id) on delete restrict,
          submitted_by uuid,
          submitted_at timestamptz,
          reviewed_by uuid,
          reviewed_at timestamptz,
          review_comment text,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          constraint uq_form_submission_version unique(recipient_id, version),
          constraint ck_form_submission_status check(status in ('draft', 'submitted', 'replaced', 'accepted', 'rework'))
        )
        """
    )
    op.execute("alter table request_recipients add constraint fk_request_recipient_last_submission foreign key(last_submission_id) references form_submissions(id) on delete set null")
    op.execute("create index ix_form_submissions_recipient_status on form_submissions(recipient_id, status, version desc)")
    op.execute(
        """
        create table field_confirmations (
          id uuid primary key default gen_random_uuid(),
          object_id uuid not null references entity_objects(id) on delete cascade,
          entity_field_id uuid not null references entity_fields(id) on delete cascade,
          value_hash varchar(64) not null,
          confirmed_by uuid,
          confirmed_by_label varchar(500),
          method varchar(32) not null,
          confirmed_at timestamptz not null default now(),
          valid_until timestamptz,
          submission_id uuid references form_submissions(id) on delete set null,
          constraint uq_field_confirmation_value unique(object_id, entity_field_id, value_hash)
        )
        """
    )
    op.execute("create index ix_field_confirmations_object_field on field_confirmations(object_id, entity_field_id, confirmed_at desc)")

    op.execute(
        """
        create table assignments (
          id uuid primary key default gen_random_uuid(),
          title varchar(500) not null,
          description text,
          owner_organization_id uuid references organizations(id) on delete restrict,
          created_by uuid,
          reviewer_id uuid,
          due_at timestamptz,
          priority varchar(16) not null default 'normal',
          expected_result jsonb not null default '{}'::jsonb,
          related_request_id uuid references information_requests(id) on delete set null,
          status varchar(24) not null default 'draft',
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          constraint ck_assignment_priority check(priority in ('low', 'normal', 'high', 'critical')),
          constraint ck_assignment_status check(status in ('draft', 'active', 'completed', 'cancelled'))
        )
        """
    )
    op.execute(
        """
        create table assignment_executions (
          id uuid primary key default gen_random_uuid(),
          assignment_id uuid not null references assignments(id) on delete cascade,
          assignee_id uuid,
          assignee_organization_id uuid references organizations(id) on delete restrict,
          object_ids jsonb not null default '[]'::jsonb,
          status varchar(24) not null default 'not_started',
          result jsonb not null default '{}'::jsonb,
          change_set_id uuid references change_sets(id) on delete restrict,
          submitted_at timestamptz,
          completed_at timestamptz,
          completion_late boolean not null default false,
          review_comment text,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          constraint ck_assignment_execution_status check(status in ('not_started', 'in_progress', 'review', 'rework', 'completed', 'cancelled'))
        )
        """
    )
    op.execute("create index ix_assignment_executions_assignee_status on assignment_executions(assignee_id, status)")

    op.execute(
        """
        create table interagency_requests (
          id uuid primary key default gen_random_uuid(),
          sender_organization_id uuid not null references organizations(id) on delete restrict,
          recipient_organization_id uuid not null references organizations(id) on delete restrict,
          purpose text not null,
          object_ids jsonb not null default '[]'::jsonb,
          field_codes jsonb not null default '[]'::jsonb,
          form_id uuid references form_definitions(id) on delete restrict,
          due_at timestamptz,
          expected_format varchar(32),
          status varchar(24) not null default 'draft',
          created_by uuid,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          constraint ck_interagency_request_status check(status in ('draft', 'sent', 'in_progress', 'response_received', 'clarification', 'completed', 'cancelled'))
        )
        """
    )
    op.execute("create index ix_interagency_requests_recipient_status on interagency_requests(recipient_organization_id, status, due_at)")
    op.execute(
        """
        create table interagency_response_versions (
          id uuid primary key default gen_random_uuid(),
          request_id uuid not null references interagency_requests(id) on delete cascade,
          version integer not null,
          payload jsonb not null default '{}'::jsonb,
          change_set_id uuid references change_sets(id) on delete restrict,
          submitted_by uuid,
          submitted_at timestamptz not null default now(),
          accepted_by uuid,
          accepted_at timestamptz,
          constraint uq_interagency_response_version unique(request_id, version)
        )
        """
    )


def downgrade() -> None:
    """Удалить таблицы предметных процессов."""

    op.execute("drop table if exists interagency_response_versions")
    op.execute("drop table if exists interagency_requests")
    op.execute("drop table if exists assignment_executions")
    op.execute("drop table if exists assignments")
    op.execute("drop table if exists field_confirmations")
    op.execute("alter table request_recipients drop constraint if exists fk_request_recipient_last_submission")
    op.execute("drop table if exists form_submissions")
    op.execute("drop table if exists form_access_links")
    op.execute("drop table if exists request_recipients")
    op.execute("drop table if exists information_requests")
    op.execute("drop table if exists form_versions")
    op.execute("drop table if exists form_definitions")
