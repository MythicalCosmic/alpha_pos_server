"""Customer Mini App WebSocket routes — merged into config/asgi.py alongside the
core.realtime + courier routes."""
from django.urls import re_path

from smartfood import consumers

websocket_urlpatterns = [
    re_path(r'^ws/smartfood/orders/(?P<order_id>\d+)/$',
            consumers.CustomerOrderConsumer.as_asgi()),
]
