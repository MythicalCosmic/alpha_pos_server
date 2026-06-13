FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Only curl is needed at runtime (healthcheck). psycopg v3 ships a manylinux binary
# wheel with libpq bundled, so there is NO compilation step — no build-essential,
# no libpq-dev (this is why the base moved to 3.13: full binary-wheel coverage).
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 1) Shared spine from the vendored wheel (pulls Django / channels / psycopg v3 /
#    redis / ... via its own pinned deps), then the server-edition extras. Kept
#    before `COPY . .` so this dependency layer caches across app-only rebuilds.
#    (When alpha_pos_core is published to PyPI this becomes a line in requirements.txt.)
COPY vendor/ /wheels/
COPY requirements.txt .
RUN pip install /wheels/alpha_pos_core-*.whl -r requirements.txt

# 2) Server edition app code (config/, admins/, deploy/, manage.py, ...).
COPY . .

# Stamp the build with the git commit so /healthz reports exactly which code is live.
ARG GIT_SHA=unknown
ENV APP_GIT_SHA=${GIT_SHA}

# Collect static (Django admin assets). A throwaway key lets settings import with
# DEBUG=False; collectstatic itself needs no real secret and no DB.
RUN SECRET_KEY=build-time-only-not-used-at-runtime \
    python manage.py collectstatic --noinput

# Non-root runtime user.
RUN groupadd --system app && useradd --system --gid app --home /app --shell /usr/sbin/nologin app \
    && chown -R app:app /app

COPY --chown=app:app entrypoint.sh /entrypoint.sh
# Strip CR so a Windows (CRLF) checkout doesn't yield a `#!/bin/sh\r` shebang.
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8000/healthz || exit 1

ENTRYPOINT ["/entrypoint.sh"]
