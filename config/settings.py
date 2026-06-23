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
# couriers = the delivery-rider backend (assignment, courier lifecycle, GPS relay).
INSTALLED_APPS = build_installed_apps(['admins', 'smartfood', 'couriers'])  # noqa: F405

# Expo push (courier app background notifications). Optional in dev; set for prod.
EXPO_ACCESS_TOKEN = os.environ.get('EXPO_ACCESS_TOKEN', '')

# Shared secret for the courier payment webhook (online-gateway confirmation).
# Empty = the webhook endpoint is disabled (the launch is record-only / cash).
COURIER_PAYMENT_WEBHOOK_SECRET = os.environ.get('COURIER_PAYMENT_WEBHOOK_SECRET', '')

# Auto-dispatch a bot order to the active cashier on a CONNECTED POS the moment it
# lands (WS Phase 3). ON by default; set false to restore the manual operator queue.
SMARTFOOD_AUTO_DISPATCH = os.environ.get(
    'SMARTFOOD_AUTO_DISPATCH', 'True').strip().lower() in ('1', 'true', 'yes', 'on')

# Auto-assign an available courier to a DELIVERY order right after dispatch. OFF by
# default — couriers are chosen by hand via POST /api/admins/couriers/assign.
COURIER_AUTO_ASSIGN = os.environ.get(
    'COURIER_AUTO_ASSIGN', 'False').strip().lower() in ('1', 'true', 'yes', 'on')

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
