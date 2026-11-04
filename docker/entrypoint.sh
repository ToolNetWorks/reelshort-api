#!/bin/sh
# Entrypoint: apply migrations, then exec the server (PID 1 aware).
# Works on Render (PORT env), local (8077), and with Turso or SQLite.
set -eu

# --- Render provides PORT, default 8077 for local ---
export PORT="${PORT:-8077}"

# --- Database URL for alembic (Turso preferred, SQLite fallback) ---
DB_PATH="${DB_PATH:-/tmp/data/reelshort.sqlite3}"
export DB_PATH

if [ -n "${TURSO_DATABASE_URL:-}" ] && [ -n "${TURSO_AUTH_TOKEN:-}" ]; then
    # Turso (libSQL) - persistent, works on Render free tier
    # Format: libsql://db-name-xyz.turso.io → sqlite+libsql://db-name-xyz.turso.io?secure=true
    DB_URL="sqlite+${TURSO_DATABASE_URL}?secure=true"
    echo "[entrypoint] Using Turso database: ${TURSO_DATABASE_URL}"
else
    # Local SQLite (ephemeral on Render free tier - data lost on restart!)
    mkdir -p "$(dirname "$DB_PATH")" "${BACKUP_DIR:-/tmp/data/backups}"
    DB_URL="sqlite+pysqlite:///${DB_PATH}"
    echo "[entrypoint] Using SQLite: ${DB_PATH} (ephemeral on Render free tier)"
fi

export DB_URL

# --- Alembic config: override sqlalchemy.url at runtime ---
# alembic.ini has placeholder, we inject via env var substitution
# Create a temporary alembic.ini with resolved URL
cat > /tmp/alembic.runtime.ini <<EOF
[alembic]
script_location = migrations
prepend_sys_path = .
version_path_separator = os
path_separator = os
sqlalchemy.url = ${DB_URL}

[post_write_hooks]

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARNING
handlers = console
qualname =

[logger_sqlalchemy]
level = WARNING
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
EOF

echo "[entrypoint] Applying database migrations -> ${DB_URL}"
alembic -c /tmp/alembic.runtime.ini upgrade head

# --- Start server ---
# Use CMD args if provided, otherwise default uvicorn command
if [ $# -gt 0 ]; then
    echo "[entrypoint] Starting: $*"
    exec "$@"
else
    echo "[entrypoint] Starting uvicorn on 0.0.0.0:${PORT}"
    exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --workers 1 --proxy-headers --no-server-header
fi