"""Добавляет универсальную иерархию сущностей и объектов.

Revision ID: 20260827_0007
Revises: 20260826_0006
Create Date: 2026-08-27 00:07:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260827_0007"
down_revision: str | None = "20260826_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Создать связи родитель-потомок для схем сущностей и объектов."""

    op.add_column(
        "entity_schemas",
        sa.Column(
            "parent_entity_schema_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_entity_schemas_parent_entity_schema_id",
        "entity_schemas",
        "entity_schemas",
        ["parent_entity_schema_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_entity_schemas_parent",
        "entity_schemas",
        ["parent_entity_schema_id"],
        postgresql_where=sa.text("parent_entity_schema_id is not null"),
    )

    op.add_column(
        "entity_objects",
        sa.Column(
            "parent_object_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_entity_objects_parent_object_id",
        "entity_objects",
        "entity_objects",
        ["parent_object_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_entity_objects_parent_status_updated",
        "entity_objects",
        ["parent_object_id", "status", "updated_at"],
        postgresql_where=sa.text("parent_object_id is not null"),
    )


def downgrade() -> None:
    """Откатить связи родитель-потомок."""

    op.drop_index("ix_entity_objects_parent_status_updated", table_name="entity_objects")
    op.drop_constraint(
        "fk_entity_objects_parent_object_id",
        "entity_objects",
        type_="foreignkey",
    )
    op.drop_column("entity_objects", "parent_object_id")

    op.drop_index("ix_entity_schemas_parent", table_name="entity_schemas")
    op.drop_constraint(
        "fk_entity_schemas_parent_entity_schema_id",
        "entity_schemas",
        type_="foreignkey",
    )
    op.drop_column("entity_schemas", "parent_entity_schema_id")
