#!/bin/sh
set -eu

DB_HOST="${DB_HOST:-db}"
DB_PORT="${DB_PORT:-5432}"

# Wait up to ~60s for the DB to accept connections. Bail out instead of
# spinning forever so a misconfiguration surfaces in container logs.
echo "Waiting for ${DB_HOST}:${DB_PORT}..."
i=0
until python -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('${DB_HOST}', ${DB_PORT})) == 0 else 1)" 2>/dev/null; do
    i=$((i + 1))
    if [ "${i}" -ge 60 ]; then
        echo "DB not reachable after ${i}s, exiting." >&2
        exit 1
    fi
    sleep 1
done
echo "DB reachable."

echo "Running migrations..."
python manage.py migrate --noinput

# License heartbeat runs as a sibling process — NOT inside gunicorn.
# Spawning it from AppConfig.ready() would create one heartbeat thread
# per worker (3 by default), tripling control-center load and skewing
# last_heartbeat_at attribution. Running it separately keeps a single
# loop, makes it observable in `docker logs`, and lets `docker stop`
# kill it cleanly via SIGTERM. Skipped when LICENSE_HEARTBEAT_DISABLED
# is set, for local development against a stub control center.
if [ -z "${LICENSE_HEARTBEAT_DISABLED:-}" ]; then
    python manage.py heartbeat_daemon &
fi

echo "Starting gunicorn..."
exec gunicorn alpha_pos.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers "${GUNICORN_WORKERS:-3}" \
    --timeout "${GUNICORN_TIMEOUT:-120}" \
    --access-logfile - \
    --error-logfile -
