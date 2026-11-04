#!/usr/bin/env bash
# Online-safe SQLite backup using the official `.backup` command (WAL aware).
#
#   scripts/backup.sh [destination-dir]
#
# Default destination: $BACKUP_DIR (or <data>/backups). Keeps the newest
# $BACKUP_KEEP archives (default 7) and verifies integrity afterwards.
set -euo pipefail

DATA_DIR="${DATA_DIR:-/srv/data}"
DB_PATH="${DB_PATH:-${DATA_DIR}/reelshort.sqlite3}"
DEST="${1:-${BACKUP_DIR:-${DATA_DIR}/backups}}"
KEEP="${BACKUP_KEEP:-7}"

if [[ ! -f "$DB_PATH" ]]; then
  echo "backup: database not found at $DB_PATH" >&2
  exit 1
fi

mkdir -p "$DEST"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="${DEST}/reelshort-${stamp}.sqlite3"

echo "backup: $DB_PATH -> $target"
# `.backup` is transactionally consistent even while the API is writing.
sqlite3 "$DB_PATH" ".backup '${target}'" 2>/dev/null || {
  # Fallback: python's sqlite3 backup API (same guarantees, no CLI needed).
  python3 - "$DB_PATH" "$target" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close()
src.close()
PY
}

integrity="$(python3 - "$target" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
try:
    print(con.execute("PRAGMA integrity_check").fetchone()[0])
finally:
    con.close()
PY
)"
if [[ "$integrity" != "ok" ]]; then
  echo "backup: integrity check failed: $integrity" >&2
  rm -f "$target"
  exit 1
fi

gzip -f "$target"
echo "backup: ok -> ${target}.gz ($(du -h "${target}.gz" | cut -f1))"

# Retention: keep the newest $KEEP archives.
mapfile -t archives < <(ls -1t "${DEST}"/reelshort-*.sqlite3.gz 2>/dev/null || true)
if ((${#archives[@]} > KEEP)); then
  for old in "${archives[@]:KEEP}"; do
    echo "backup: pruning $old"
    rm -f "$old"
  done
fi
