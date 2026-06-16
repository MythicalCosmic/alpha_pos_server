"""Courier WebSocket routes — merged into config/asgi.py alongside the
core.realtime routes."""
from django.urls import re_path

from couriers import consumers

websocket_urlpatterns = [
    re_path(r'^ws/courier/$', consumers.CourierConsumer.as_asgi()),
    re_path(r'^ws/cashier/$', consumers.CashierConsumer.as_asgi()),
]
