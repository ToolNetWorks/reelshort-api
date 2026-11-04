"""Repair helper: give every edition a group (singleton until it is matched).

Older rows created before discovery ensured groups (or by hand) can be left with
``movie_group_id IS NULL``. This is idempotent and safe to re-run:

    docker compose exec reelshort-api python /srv/scripts/repair_groups.py
    DB_PATH=./data/reelshort.sqlite3 .venv/bin/python scripts/repair_groups.py

It never merges groups and never touches already-grouped editions.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from app.crawler import store  # noqa: E402
from app.db.base import session_scope  # noqa: E402
from app.db.models import MovieEdition, MovieGroup  # noqa: E402


def main() -> int:
    repaired = 0
    with session_scope() as session:
        orphans = list(session.scalars(select(MovieEdition).where(MovieEdition.movie_group_id.is_(None))))
        for edition in orphans:
            group = store.ensure_group(session, edition)
            store.refresh_group_stats(session, group.id)
            repaired += 1
            print(f"  {edition.book_id} ({edition.lang}) -> {group.id}")
        groups = int(session.scalar(select(func.count()).select_from(MovieGroup)) or 0)
        editions = int(session.scalar(select(func.count()).select_from(MovieEdition)) or 0)
    print(f"repaired {repaired} edition(s); {editions} editions in {groups} group(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
