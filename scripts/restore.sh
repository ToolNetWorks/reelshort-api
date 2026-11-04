#!/usr/bin/env bash
# Restore a database archive produced by scripts/backup.sh.
#
#   scripts/restore.sh <archive.sqlite3.gz|archive.sqlite3> [--yes]
#
# The service MUST be stopped first (docker compose stop reelshort-api);
# the current database is preserved as `<db>.pre-restore-<stamp>`.
set -euo pipefail

ARCHIVE="${1:-}"
CONFIRM="${2:-}"
DATA_DIR="${DATA_DIR:-/srv/data}"
DB_PATH="${DB_PATH:-${DATA_DIR}/reelshort.sqlite3}"

if [[ -z "$ARCHIVE" || ! -f "$ARCHIVE" ]]; then
  echo "usage: scripts/restore.sh <archive.sqlite3.gz> [--yes]" >&2
  exit 1
fi

if [[ "$CONFIRM" != "--yes" ]]; then
  read -r -p "Restore ${ARCHIVE} over ${DB_PATH}? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || { echo "aborted"; exit 1; }
fi

if pgrep -f "uvicorn app.main:app" >/dev/null 2>&1; then
  echo "restore: the API still appears to be running — stop it first" >&2
  exit 1
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

case "$ARCHIVE" in
  *.gz) gzip -dc "$ARCHIVE" > "$work/restore.sqlite3" ;;
  *)    cp "$ARCHIVE" "$work/restore.sqlite3" ;;
esac

integrity="$(python3 - "$work/restore.sqlite3" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
try:
    print(con.execute("PRAGMA integrity_check").fetchone()[0])
finally:
    con.close()
PY
)"
if [[ "$integrity" != "ok" ]]; then
  echo "restore: archive failed integrity check: $integrity" >&2
  exit 1
fi

mkdir -p "$(dirname "$DB_PATH")"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
if [[ -f "$DB_PATH" ]]; then
  mv "$DB_PATH" "${DB_PATH}.pre-restore-${stamp}"
  echo "restore: previous database kept at ${DB_PATH}.pre-restore-${stamp}"
fi

# Stale WAL/SHM sidecars would corrupt the restored file.
rm -f "${DB_PATH}-wal" "${DB_PATH}-shm"
cp "$work/restore.sqlite3" "$DB_PATH"

echo "restore: ok -> $DB_PATH"
echo "restore: start the service (docker compose up -d) — migrations run on boot."
