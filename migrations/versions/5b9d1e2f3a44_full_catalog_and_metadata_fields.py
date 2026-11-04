"""full catalog state and metadata fields

Revision ID: 5b9d1e2f3a44
Revises: 4a8c9e1f2b33
Create Date: 2026-10-19 00:15:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "5b9d1e2f3a44"
down_revision: Union[str, None] = "4a8c9e1f2b33"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("movie_editions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("provider", sa.String(length=32), server_default="reelshort", nullable=False)
        )
        batch_op.add_column(sa.Column("read_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("collect_count", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("metadata_status", sa.String(length=32), server_default="COMPLETE", nullable=False)
        )
        batch_op.add_column(sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True))

    op.execute(
        """
        UPDATE movie_editions
        SET
            discovered_at = COALESCE(discovered_at, created_at, fetched_at),
            last_synced_at = COALESCE(last_synced_at, fetched_at, updated_at),
            read_count = COALESCE(read_count, CAST(json_extract(raw_meta, '$.read_count') AS INTEGER)),
            collect_count = COALESCE(collect_count, CAST(json_extract(raw_meta, '$.collect_count') AS INTEGER)),
            metadata_status = CASE
                WHEN (chapter_count IS NULL OR chapter_count <= 0)
                     AND (stored_chapter_count IS NULL OR stored_chapter_count <= 0)
                THEN 'METADATA_INCOMPLETE'
                ELSE 'COMPLETE'
            END
        """
    )

    with op.batch_alter_table("movie_editions", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_movie_editions_provider_lang_book", ["provider", "lang", "book_id"]
        )
        batch_op.create_index("ix_movie_editions_lang_read_count", ["lang", "read_count"], unique=False)
        batch_op.create_index("ix_movie_editions_metadata_status", ["metadata_status"], unique=False)

    op.create_table(
        "catalog_states",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.String(length=32), server_default="reelshort", nullable=False),
        sa.Column("locale", sa.String(length=16), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_total", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_pages", sa.Integer(), server_default="0", nullable=False),
        sa.Column("page_size", sa.Integer(), server_default="12", nullable=False),
        sa.Column("pages_completed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("pages_failed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("discovered_total", sa.Integer(), server_default="0", nullable=False),
        sa.Column("unique_total", sa.Integer(), server_default="0", nullable=False),
        sa.Column("indexed_total", sa.Integer(), server_default="0", nullable=False),
        sa.Column("enriched_total", sa.Integer(), server_default="0", nullable=False),
        sa.Column("incomplete_total", sa.Integer(), server_default="0", nullable=False),
        sa.Column("new_movies", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_movies", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped_movies", sa.Integer(), server_default="0", nullable=False),
        sa.Column("http_requests", sa.Integer(), server_default="0", nullable=False),
        sa.Column("crawl_duration_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("crawl_rate_pages_per_min", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("last_completed_page", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="idle", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "locale", name="uq_catalog_states_provider_locale"),
    )
    with op.batch_alter_table("catalog_states", schema=None) as batch_op:
        batch_op.create_index("ix_catalog_states_locale", ["locale"], unique=False)

    op.create_table(
        "catalog_page_states",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.String(length=32), server_default="reelshort", nullable=False),
        sa.Column("locale", sa.String(length=16), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("items_found", sa.Integer(), server_default="0", nullable=False),
        sa.Column("items_new", sa.Integer(), server_default="0", nullable=False),
        sa.Column("items_updated", sa.Integer(), server_default="0", nullable=False),
        sa.Column("items_skipped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("incomplete_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("crawled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "locale", "page_number", name="uq_catalog_page_states_provider_locale_page"),
    )
    with op.batch_alter_table("catalog_page_states", schema=None) as batch_op:
        batch_op.create_index("ix_catalog_page_states_locale_status", ["locale", "status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("catalog_page_states", schema=None) as batch_op:
        batch_op.drop_index("ix_catalog_page_states_locale_status")
    op.drop_table("catalog_page_states")

    with op.batch_alter_table("catalog_states", schema=None) as batch_op:
        batch_op.drop_index("ix_catalog_states_locale")
    op.drop_table("catalog_states")

    with op.batch_alter_table("movie_editions", schema=None) as batch_op:
        batch_op.drop_index("ix_movie_editions_metadata_status")
        batch_op.drop_index("ix_movie_editions_lang_read_count")
        batch_op.drop_constraint("uq_movie_editions_provider_lang_book", type_="unique")
        batch_op.drop_column("last_synced_at")
        batch_op.drop_column("discovered_at")
        batch_op.drop_column("metadata_status")
        batch_op.drop_column("collect_count")
        batch_op.drop_column("read_count")
        batch_op.drop_column("provider")
