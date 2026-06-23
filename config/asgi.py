"""ASGI entrypoint — server edition. Serves HTTP (Django) + websockets (channels)
through a single ProtocolTypeRouter. Run with uvicorn workers in production."""
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

from django.core.asgi import get_asgi_application

# Initialise Django (populate the app registry) BEFORE importing consumers.
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter

from core.realtime.routing import websocket_urlpatterns as core_ws_urlpatterns
from couriers.routing import websocket_urlpatterns as courier_ws_urlpatterns
from smartfood.routing import websocket_urlpatterns as smartfood_ws_urlpatterns

# In-store realtime (orders/kds/cashiers) + courier layer (/ws/courier/, /ws/cashier/)
# + customer Mini App order tracking (/ws/smartfood/orders/<id>/).
websocket_urlpatterns = (
    core_ws_urlpatterns + courier_ws_urlpatterns + smartfood_ws_urlpatterns
)

application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': AuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
})
