FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Build deps in a separate layer so the wheel cache is reused across rebuilds,
# then drop them so the final image stays small.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && pip install --no-cache-dir gunicorn \
    && true

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# Stamp the build with the git commit so /healthz can report exactly which code
# is live. Passed by the deploy scripts (--build-arg GIT_SHA=...); defaults to
# "unknown" for ad-hoc builds. Placed after COPY so it only affects late layers.
ARG GIT_SHA=unknown
ENV APP_GIT_SHA=${GIT_SHA}

# A throwaway SECRET_KEY lets settings import at build time — with DEBUG unset
# (False) settings is fail-loud without one, which is why this previously ran
# under `2>/dev/null || true` and silently collected NOTHING. collectstatic
# needs no real secret and no DB. Fail the build if it errors rather than
# shipping an admin with no CSS.
RUN SECRET_KEY=build-time-only-not-used-at-runtime \
    python manage.py collectstatic --noinput

# Run as a non-root user; createUser ahead of chown so the container can
# write to the working directory but not poke at root-owned files.
RUN groupadd --system app && useradd --system --gid app --home /app --shell /usr/sbin/nologin app \
    && chown -R app:app /app

COPY --chown=app:app entrypoint.sh /entrypoint.sh
# Strip any CR so a Windows (CRLF) checkout doesn't yield a `#!/bin/sh\r`
# shebang that crashes the container with "no such file or directory".
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8000/healthz || exit 1

ENTRYPOINT ["/entrypoint.sh"]
