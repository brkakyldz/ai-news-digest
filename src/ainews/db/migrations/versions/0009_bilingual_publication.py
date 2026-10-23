"""Additive schema for one selection with two stored language views.

Revision ID: 0009_bilingual_publication
Revises: 0008_published_checks
Created: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from ainews.db.models import UTCDateTime

revision: str = "0009_bilingual_publication"
down_revision: str | None = "0008_published_checks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("bulletins") as batch_op:
        batch_op.add_column(sa.Column("editor_note_tr", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("bilingual", sa.Boolean(), nullable=False, server_default=sa.false())
        )

    with op.batch_alter_table("bulletin_items") as batch_op:
        batch_op.add_column(sa.Column("reason_tr", sa.Text(), nullable=True))

    op.create_table(
        "summary_translations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("summary_id", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(length=2), nullable=False),
        sa.Column("title_local", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("why_it_matters", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=60), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("est_cost_usd", sa.Float(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("language = 'tr'", name="ck_summary_translations_language"),
        sa.CheckConstraint("length(canonical_hash) = 64", name="ck_summary_translations_hash"),
        sa.ForeignKeyConstraint(["summary_id"], ["summaries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("summary_id", "language", name="uq_translation_summary_language"),
    )


def downgrade() -> None:
    op.drop_table("summary_translations")
    with op.batch_alter_table("bulletin_items") as batch_op:
        batch_op.drop_column("reason_tr")
    with op.batch_alter_table("bulletins") as batch_op:
        batch_op.drop_column("bilingual")
        batch_op.drop_column("editor_note_tr")
