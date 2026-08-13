#!/usr/bin/env bash
# Reconcile the public Telegram Mini App to one canonical delivery hostname.
# Safe to run repeatedly; used by both full deploys and the self-redeploy timer.
set -euo pipefail

ALPHA_DIR="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
PUBLIC_IP="${2:-}"
ENV_FILE="$ALPHA_DIR/.env"
EDGE_NETWORK="${EDGE_NETWORK:-edge}"
WEBAPP_CONTAINER="${DELIVERY_WEBAPP_CONTAINER:-smartfood-webapp}"

[ -f "$ENV_FILE" ] || { echo "delivery reconcile: missing $ENV_FILE" >&2; exit 1; }

if [ -z "$PUBLIC_IP" ]; then
    POS_HOST="$(sed -n 's/^ALLOWED_HOSTS=\([^,]*\).*/\1/p' "$ENV_FILE" | head -n1)"
    case "$POS_HOST" in
        pos.*.nip.io)
            PUBLIC_IP="${POS_HOST#pos.}"
            PUBLIC_IP="${PUBLIC_IP%.nip.io}"
            ;;
        *)
            echo "delivery reconcile: cannot derive public IP from ALLOWED_HOSTS" >&2
            exit 1
            ;;
    esac
fi

DELIVERY_HOST="delivery.${PUBLIC_IP}.nip.io"
DELIVERY_URL="https://${DELIVERY_HOST}/webapp/"
ENV_CHANGED=false

CURRENT_URL="$(sed -n 's/^CUSTOMER_WEBAPP_URL=//p' "$ENV_FILE" | head -n1)"
if [ "$CURRENT_URL" != "$DELIVERY_URL" ]; then
    ENV_TMP="$(mktemp "${ENV_FILE}.delivery.XXXXXX")"
    awk -v delivery_url="$DELIVERY_URL" '
        BEGIN { replaced = 0 }
        /^CUSTOMER_WEBAPP_URL=/ {
            if (!replaced) print "CUSTOMER_WEBAPP_URL=" delivery_url
            replaced = 1
            next
        }
        { print }
        END {
            if (!replaced) print "CUSTOMER_WEBAPP_URL=" delivery_url
        }
    ' "$ENV_FILE" > "$ENV_TMP"
    chmod 600 "$ENV_TMP"
    chown --reference="$ENV_FILE" "$ENV_TMP"
    mv "$ENV_TMP" "$ENV_FILE"
    ENV_CHANGED=true
    echo "delivery reconcile: CUSTOMER_WEBAPP_URL -> $DELIVERY_URL"
fi

docker network inspect "$EDGE_NETWORK" >/dev/null 2>&1 \
    || docker network create "$EDGE_NETWORK" >/dev/null
if docker container inspect "$WEBAPP_CONTAINER" >/dev/null 2>&1; then
    WEBAPP_EDGE="$(docker inspect --format \
        "{{json (index .NetworkSettings.Networks \"$EDGE_NETWORK\")}}" \
        "$WEBAPP_CONTAINER" 2>/dev/null || true)"
    if [ -z "$WEBAPP_EDGE" ] || [ "$WEBAPP_EDGE" = "null" ]; then
        docker network connect --alias delivery-webapp \
            "$EDGE_NETWORK" "$WEBAPP_CONTAINER"
        echo "delivery reconcile: connected $WEBAPP_CONTAINER to $EDGE_NETWORK"
    fi
else
    echo "delivery reconcile: webapp container $WEBAPP_CONTAINER is not running" >&2
    exit 1
fi

CADDY_FOUND=false
for CADDY_DIR in "$ALPHA_DIR/deploy/caddy" "$ALPHA_DIR/caddy"; do
    CADDYFILE="$CADDY_DIR/Caddyfile"
    CADDY_COMPOSE="$CADDY_DIR/docker-compose.yml"
    [ -f "$CADDYFILE" ] && [ -f "$CADDY_COMPOSE" ] || continue
    CADDY_FOUND=true

    if ! grep -Fqx "$DELIVERY_HOST {" "$CADDYFILE"; then
        CADDY_BACKUP="$(mktemp "${CADDYFILE}.delivery.XXXXXX")"
        cp "$CADDYFILE" "$CADDY_BACKUP"
        {
            echo ""
            echo "$DELIVERY_HOST {"
            printf '\treverse_proxy %s:80\n' "$WEBAPP_CONTAINER"
            echo "}"
        } >> "$CADDYFILE"

        if ! docker compose -f "$CADDY_COMPOSE" exec -T caddy \
            caddy validate --config /etc/caddy/Caddyfile >/dev/null; then
            mv "$CADDY_BACKUP" "$CADDYFILE"
            echo "delivery reconcile: invalid Caddy configuration; restored backup" >&2
            exit 1
        fi
        rm "$CADDY_BACKUP"
        echo "delivery reconcile: added Caddy route for $DELIVERY_HOST"
    fi

    CADDY_ID="$(docker compose -f "$CADDY_COMPOSE" ps -q caddy 2>/dev/null || true)"
    if [ -n "$CADDY_ID" ]; then
        docker compose -f "$CADDY_COMPOSE" exec -T caddy \
            caddy reload --config /etc/caddy/Caddyfile >/dev/null
    fi
done

$CADDY_FOUND || { echo "delivery reconcile: active Caddy configuration not found" >&2; exit 1; }

if $ENV_CHANGED; then
    COMPOSE_ARGS=(-f "$ALPHA_DIR/docker-compose.yaml")
    [ -f "$ALPHA_DIR/docker-compose.edge.yml" ] \
        && COMPOSE_ARGS+=(-f "$ALPHA_DIR/docker-compose.edge.yml")
    docker compose "${COMPOSE_ARGS[@]}" up -d --no-deps --force-recreate bot
fi

BOT_TOKEN="$(sed -n 's/^CUSTOMER_BOT_TOKEN=//p' "$ENV_FILE" | head -n1)"
if [ -n "$BOT_TOKEN" ]; then
    MENU_BUTTON="$(printf \
        '{"type":"web_app","text":"Order food","web_app":{"url":"%s"}}' \
        "$DELIVERY_URL")"
    if curl -fsS --max-time 15 -X POST \
        "https://api.telegram.org/bot${BOT_TOKEN}/setChatMenuButton" \
        --data-urlencode "menu_button=${MENU_BUTTON}" >/dev/null; then
        echo "delivery reconcile: Telegram menu -> $DELIVERY_URL"
    else
        echo "delivery reconcile: Telegram menu update failed; next deploy will retry" >&2
    fi
fi

for _attempt in $(seq 1 6); do
    if curl -fsS --max-time 5 "https://${DELIVERY_HOST}/healthz" >/dev/null; then
        echo "delivery reconcile: healthy at $DELIVERY_URL"
        exit 0
    fi
    sleep 3
done

echo "delivery reconcile: $DELIVERY_URL did not become healthy" >&2
exit 1
