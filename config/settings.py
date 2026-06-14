"""Server edition settings — cloud back-office. Extends the shared core spine.

Run with DJANGO_SETTINGS_MODULE=config.settings. Production supplies SECRET_KEY,
DB_* (Postgres) and REDIS_URL via the environment (see docker-compose.yaml).
"""
import os

os.environ.setdefault('DEPLOYMENT_MODE', 'cloud')

from alpha_pos_core.settings_base import *  # noqa: F401,F403

EDITION = 'server'

# Back-office app on top of the shared spine. customers / waiters are NOT installed
# here (no POS order-taking on the server). hr IS installed (shared) and its UI is
# mounted in config.urls; admins' order-WRITE routes are intentionally not mounted.
# smartfood = the customer Telegram Mini App delivery backend (server-only).
INSTALLED_APPS = build_installed_apps(['admins', 'smartfood'])  # noqa: F405

ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

# Multi-worker cloud: a shared Redis channel layer so websocket groups fan out
# across uvicorn workers. (Activates once 'channels' is added in the websocket
# phase; inert until then.)
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        # capacity per channel: the default 100 silently DROPS messages under a
        # burst (load test: 100 -> 50% delivered; 5000 -> 100% at ~68k msg/s fanout).
        'CONFIG': {'hosts': [REDIS_URL], 'capacity': 5000},  # noqa: F405
    },
}
