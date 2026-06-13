"""ASGI entrypoint — server edition.

Phase 0: a plain Django ASGI app (served by uvicorn workers in place of
gunicorn-WSGI). The websocket phase wraps this in a channels ProtocolTypeRouter:

    application = ProtocolTypeRouter({
        'http': django_asgi_app,
        'websocket': AuthMiddlewareStack(URLRouter(core.realtime.routing.websocket_urlpatterns)),
    })
"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

application = get_asgi_application()
