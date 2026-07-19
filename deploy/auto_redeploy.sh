#!/usr/bin/env bash
#
# Self-redeploy: poll the git remote and, when the deploy branch moves, pull and
# rebuild+restart the stack. No GitHub Actions, no registry, no billing — you
# `git push`, the server redeploys itself within the timer interval.
#
# Pairs with deploy/systemd/alpha-pos-autodeploy.{service,timer}. Run manually to
# test:  DEPLOY_BRANCH=main ~/alpha_pos/deploy/auto_redeploy.sh
#
# Safe by design:
#   * fast-forward only (never force-resets / discards work);
#   * a flock guard so two timer firings can't deploy at once;
#   * rebuilds with the same compose files deploy.sh uses (base + edge override).
#   * the gitignored .env and docker-compose.edge.yml are left untouched.
set -euo pipefail

ALPHA_DIR="${ALPHA_DIR:-$HOME/alpha_pos}"
BRANCH="${DEPLOY_BRANCH:-main}"
LOCK="/tmp/alpha-pos-autodeploy.lock"

# Serialize: if a previous run is still building, skip this tick.
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "$(date -Is) another redeploy is in progress; skipping"
    exit 0
fi

cd "$ALPHA_DIR"

git fetch --quiet origin "$BRANCH"
LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/$BRANCH")"
COMPOSE="docker compose -f docker-compose.yaml -f docker-compose.edge.yml"
PUBLIC_HOST="${POS_HOST:-$(sed -n 's/^ALLOWED_HOSTS=\([^,]*\).*/\1/p' .env | head -n1)}"

if [ -z "$PUBLIC_HOST" ]; then
    echo "$(date -Is) ERROR: cannot derive the public POS host" >&2
    exit 1
fi

if [ "$LOCAL" = "$REMOTE" ]; then
    CURRENT_SHA="$(git rev-parse --short=12 HEAD)"
    if bash "$ALPHA_DIR/deploy/verify_public_route.sh" \
        "$ALPHA_DIR" "$PUBLIC_HOST" "$CURRENT_SHA"; then
        echo "$(date -Is) up to date and publicly healthy ($LOCAL)"
        exit 0
    fi
    # Git can already be at the target revision after a failed build, and a
    # manual base-only Compose recreate can drop the Caddy edge attachment.
    # Rebuild the current revision instead of treating equal SHAs as healthy.
    echo "$(date -Is) current revision is publicly unhealthy; redeploying $LOCAL"
fi

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "$(date -Is) change detected: $LOCAL -> $REMOTE ; deploying"
fi

# Make sure we're on the deploy branch, then fast-forward only. If the branches
# have diverged (someone hand-committed on the server), bail loudly rather than
# clobber it.
git checkout --quiet "$BRANCH"
if ! git merge --ff-only "origin/$BRANCH"; then
    echo "$(date -Is) ERROR: cannot fast-forward $BRANCH (diverged) — fix manually" >&2
    exit 1
fi

git submodule update --init --recursive

# Stamp the build with the commit we just checked out so /healthz reports it.
export GIT_SHA="$(git rev-parse --short=12 HEAD)"
# --build picks up the new code; migrations run from the container entrypoint.
$COMPOSE up -d --build

# Internal container health is not enough: Caddy reaches Django over the
# external `edge` network and stable `alpha-web` alias. Fail this deployment
# if the overlay was omitted or the public route still returns 502.
bash "$ALPHA_DIR/deploy/verify_public_route.sh" \
    "$ALPHA_DIR" "$PUBLIC_HOST" "$GIT_SHA"

# Drop dangling images from old builds so the disk doesn't fill over time.
docker image prune -f >/dev/null 2>&1 || true

echo "$(date -Is) deploy done -> $REMOTE"
