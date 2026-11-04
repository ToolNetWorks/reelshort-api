"""book aliases (alias book_id -> canonical edition)

ReelShort search returns several ids for the same edition; ``getBookInfo``
always answers with the canonical ``book_id``. Storing the mapping lets the API
answer alias lookups from cache instead of re-crawling the same story.

Revision ID: 3f9a6c1d2b77
Revises: 87cfbf8868ab
Create Date: 2026-10-18 19:40:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "3f9a6c1d2b77"
down_revision: Union[str, None] = "87cfbf8868ab"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "book_aliases",
        sa.Column("alias_book_id", sa.String(length=64), nullable=False),
        sa.Column("canonical_book_id", sa.String(length=64), nullable=False),
        sa.Column("lang", sa.String(length=16), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["canonical_book_id"], ["movie_editions.book_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("alias_book_id"),
    )
    op.create_index("ix_book_aliases_canonical", "book_aliases", ["canonical_book_id"])


def downgrade() -> None:
    op.drop_index("ix_book_aliases_canonical", table_name="book_aliases")
    op.drop_table("book_aliases")
