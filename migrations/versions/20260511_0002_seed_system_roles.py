"""Seed 5 built-in system roles

Revision ID: 20260511_0002
Revises: 20260511_0001
Create Date: 2026-05-11

Seeds the five canonical roles (analyst, finance, hr, c_level, admin) from
the in-code default in `src/config/rbac_config.py`. Marked `is_system=true`
so the admin-panel role CRUD blocks deletion of these — operators can
adjust permissions on them but the slugs themselves cannot disappear.
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260511_0002"
down_revision: Union[str, None] = "20260511_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SYSTEM_ROLES = [
    {
        "name": "analyst",
        "allowed_doc_types": ["10k"],
        "allowed_confidentiality": ["public"],
        "max_results": 5,
        "requires_hitl_above": None,
    },
    {
        "name": "finance",
        "allowed_doc_types": ["10k", "invoice", "expense_policy"],
        "allowed_confidentiality": ["public", "internal"],
        "max_results": 10,
        "requires_hitl_above": 100_000,
    },
    {
        "name": "hr",
        "allowed_doc_types": ["expense_policy"],
        "allowed_confidentiality": ["public", "internal"],
        "max_results": 5,
        "requires_hitl_above": None,
    },
    {
        "name": "c_level",
        "allowed_doc_types": ["10k", "invoice", "expense_policy", "board_report"],
        "allowed_confidentiality": ["public", "internal", "confidential"],
        "max_results": 15,
        "requires_hitl_above": 1_000_000,
    },
    {
        "name": "admin",
        "allowed_doc_types": ["*"],
        "allowed_confidentiality": ["*"],
        "max_results": 20,
        "requires_hitl_above": None,
    },
]


def upgrade() -> None:
    conn = op.get_bind()
    for r in _SYSTEM_ROLES:
        # ON CONFLICT prevents the migration from failing if a same-named
        # row was hand-inserted previously (or if `alembic downgrade` left
        # a partial state).
        conn.execute(
            sa.text(
                """
                INSERT INTO roles (
                    name, allowed_doc_types, allowed_confidentiality,
                    max_results, requires_hitl_above, is_system
                ) VALUES (
                    :name, CAST(:allowed_doc_types AS JSONB),
                    CAST(:allowed_confidentiality AS JSONB),
                    :max_results, :requires_hitl_above, TRUE
                )
                ON CONFLICT (name) DO NOTHING
                """
            ),
            {
                "name": r["name"],
                "allowed_doc_types": json.dumps(r["allowed_doc_types"]),
                "allowed_confidentiality": json.dumps(r["allowed_confidentiality"]),
                "max_results": r["max_results"],
                "requires_hitl_above": r["requires_hitl_above"],
            },
        )


def downgrade() -> None:
    conn = op.get_bind()
    for r in _SYSTEM_ROLES:
        conn.execute(sa.text("DELETE FROM roles WHERE name = :n"), {"n": r["name"]})
