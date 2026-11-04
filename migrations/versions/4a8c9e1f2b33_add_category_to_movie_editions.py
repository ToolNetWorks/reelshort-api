"""add category to movie_editions

Revision ID: 4a8c9e1f2b33
Revises: 3f9a6c1d2b77
Create Date: 2026-10-18 21:55:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "4a8c9e1f2b33"
down_revision: Union[str, None] = "3f9a6c1d2b77"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("movie_editions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("category", sa.String(length=128), nullable=True))
        batch_op.create_index("ix_movie_editions_category", ["category"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("movie_editions", schema=None) as batch_op:
        batch_op.drop_index("ix_movie_editions_category")
        batch_op.drop_column("category")
