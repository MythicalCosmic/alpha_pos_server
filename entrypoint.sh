#!/bin/sh
set -eu

DB_HOST="${DB_HOST:-db}"
DB_PORT="${DB_PORT:-5432}"

# Wait up to ~60s for the DB to accept connections. Bail out instead of spinning
# forever so a misconfiguration surfaces in container logs.
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

# License heartbeat runs as a sibling process (a single loop, observable in
# `docker logs`, killed cleanly by SIGTERM) — NOT one-per-worker inside the
# server. Skipped when LICENSE_HEARTBEAT_DISABLED is set.
if [ -z "${LICENSE_HEARTBEAT_DISABLED:-}" ]; then
    python manage.py heartbeat_daemon &
fi

# ASGI server: serves HTTP *and* websockets (channels) on one port. Multiple
# workers share websocket groups via the Redis channel layer (channels-redis).
# --lifespan off: channels' ProtocolTypeRouter has no lifespan handler.
echo "Starting uvicorn (ASGI: HTTP + websockets)..."
exec uvicorn config.asgi:application \
    --host 0.0.0.0 --port 8000 \
    --workers "${WEB_CONCURRENCY:-3}" \
    --lifespan off \
    --log-level info
