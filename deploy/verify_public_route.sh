#!/usr/bin/env bash
# Verify that the Django container is reachable through the public Caddy route.
#
# A container can be healthy on its private Compose network while Caddy returns
# 502 because the web service was recreated without docker-compose.edge.yml.
# Keep this check separate from the internal /healthz probe so deployments fail
# loudly when the edge attachment or the stable `alpha-web` alias is missing.
set -euo pipefail

APP_DIR="${1:?Usage: verify_public_route.sh <app-dir> <public-host> [expected-sha]}"
PUBLIC_HOST="${2:?Usage: verify_public_route.sh <app-dir> <public-host> [expected-sha]}"
EXPECTED_SHA="${3:-$(git -C "$APP_DIR" rev-parse --short=12 HEAD)}"
TIMEOUT_SECONDS="${PUBLIC_HEALTH_TIMEOUT_SECONDS:-120}"
CORS_ORIGIN="${CORS_SMOKE_ORIGIN:-https://smart-pos-admin-panel-phi.vercel.app}"
COMPOSE=(docker compose -f docker-compose.yaml -f docker-compose.edge.yml)

WEB_ID="$(cd "$APP_DIR" && "${COMPOSE[@]}" ps -q web)"
if [ -z "$WEB_ID" ]; then
    echo "!! public-route check: web container is not running" >&2
    exit 1
fi

EDGE_ALIASES="$(
    docker inspect \
        --format '{{json (index .NetworkSettings.Networks "edge").Aliases}}' \
        "$WEB_ID" 2>/dev/null || true
)"
if [[ "$EDGE_ALIASES" != *'"alpha-web"'* ]]; then
    echo "!! public-route check: web lacks the alpha-web alias on edge" >&2
    echo "   aliases=${EDGE_ALIASES:-<not attached>}" >&2
    echo "   recreate it with docker-compose.edge.yml" >&2
    exit 1
fi

PUBLIC_URL="https://${PUBLIC_HOST}/healthz"
EXPECTED_BODY="ok ${EXPECTED_SHA}"
BODY_FILE="$(mktemp)"
HEADER_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE" "$HEADER_FILE"' EXIT

DEADLINE=$((SECONDS + TIMEOUT_SECONDS))
PUBLIC_READY=false
STATUS=""
BODY=""
while (( SECONDS < DEADLINE )); do
    STATUS="$(
        curl -sS --connect-timeout 2 --max-time 4 \
            -D "$HEADER_FILE" -o "$BODY_FILE" -w '%{http_code}' \
            "$PUBLIC_URL" 2>/dev/null || true
    )"
    BODY="$(tr -d '\r\n' < "$BODY_FILE")"
    if [ "$STATUS" = "200" ] && [ "$BODY" = "$EXPECTED_BODY" ]; then
        PUBLIC_READY=true
        break
    fi
    sleep 2
done

if [ "$PUBLIC_READY" != true ]; then
    echo "!! public-route check failed: ${PUBLIC_URL}" >&2
    echo "   expected HTTP 200 body '${EXPECTED_BODY}', got '${STATUS:-none}' '${BODY:-}'" >&2
    echo "   inspect Caddy logs and confirm the alpha-web alias exists on edge" >&2
    exit 1
fi

# Exercise the same browser preflight used by the admin panel. A plain health
# GET cannot detect a missing Origin/header policy and would let a real CORS
# regression pass the deploy gate.
STATUS="$(
    curl -sS --connect-timeout 2 --max-time 8 \
        -X OPTIONS -D "$HEADER_FILE" -o "$BODY_FILE" -w '%{http_code}' \
        -H "Origin: ${CORS_ORIGIN}" \
        -H 'Access-Control-Request-Method: GET' \
        -H 'Access-Control-Request-Headers: authorization,content-type,idempotency-key' \
        "https://${PUBLIC_HOST}/api/admins/sidebar-counts" 2>/dev/null || true
)"
ALLOW_ORIGIN="$(
    awk -F ': *' 'tolower($1) == "access-control-allow-origin" {gsub("\r", "", $2); print $2}' \
        "$HEADER_FILE" | tail -n1
)"
ALLOW_HEADERS="$(
    awk -F ': *' 'tolower($1) == "access-control-allow-headers" {gsub("\r", "", $2); print tolower($2)}' \
        "$HEADER_FILE" | tail -n1
)"
ALLOW_METHODS="$(
    awk -F ': *' 'tolower($1) == "access-control-allow-methods" {gsub("\r", "", $2); print toupper($2)}' \
        "$HEADER_FILE" | tail -n1
)"

if [ "$STATUS" != "200" ] \
    || { [ "$ALLOW_ORIGIN" != "*" ] && [ "$ALLOW_ORIGIN" != "$CORS_ORIGIN" ]; }; then
    echo "!! CORS preflight failed: status=${STATUS:-none} allow-origin=${ALLOW_ORIGIN:-<missing>}" >&2
    exit 1
fi
for REQUIRED_HEADER in authorization content-type idempotency-key; do
    if ! grep -Eq "(^|[, ]+)${REQUIRED_HEADER}([, ]+|$)" <<< "$ALLOW_HEADERS"; then
        echo "!! CORS preflight omitted required header: ${REQUIRED_HEADER}" >&2
        exit 1
    fi
done
if ! grep -Eq '(^|[, ]+)GET([, ]+|$)' <<< "$ALLOW_METHODS"; then
    echo "!! CORS preflight omitted GET" >&2
    exit 1
fi

echo ">> public route + CORS ready: ${PUBLIC_URL} (${EXPECTED_SHA})"
