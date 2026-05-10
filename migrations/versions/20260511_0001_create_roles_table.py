"""Create roles table

Revision ID: 20260511_0001
Revises:
Create Date: 2026-05-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260511_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("allowed_doc_types", postgresql.JSONB, nullable=False),
        sa.Column("allowed_confidentiality", postgresql.JSONB, nullable=False),
        sa.Column("max_results", sa.Integer, nullable=False, server_default="10"),
        sa.Column("requires_hitl_above", sa.Integer, nullable=True),
        sa.Column("is_system", sa.Boolean, nullable=False, server_default=sa.text("FALSE")),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )


def downgrade() -> None:
    op.drop_table("roles")
