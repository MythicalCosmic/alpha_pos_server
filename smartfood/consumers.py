"""Customer-facing WebSocket — the Telegram Mini App subscribes to ONE of its own
orders and receives lifecycle pushes (dispatched / rejected / canceled / kitchen
status).

  CustomerOrderConsumer  /ws/smartfood/orders/<order_id>/  -> group botorder_<id>

Cloud edition (OPEN_LAN off), so a valid customer bearer token is ALWAYS required
and the socket is scoped to the authenticated customer's OWN order — a per-order
group plus an ownership check prevents leaking other customers' orders. The
socket is read-only (no inbound messages)."""
import logging

from asgiref.sync import async_to_sync
from channels.generic.websocket import JsonWebsocketConsumer
from django.conf import settings

from base.helpers.request import SessionCredentialConflict
from base.helpers.websocket import resolve_websocket_session_credential
from smartfood.realtime import botorder_group

logger = logging.getLogger('smartfood.ws')

_CLOSE_AUTH = 4401
_CLOSE_FORBIDDEN = 4403


def _handshake_token(scope):
    try:
        token, _source = resolve_websocket_session_credential(
            scope, header_schemes=('token', 'bearer'),
        )
    except SessionCredentialConflict:
        return None
    return token


def _license_blocked():
    if getattr(settings, 'LICENSE_DEV_BYPASS', False):
        return False
    try:
        from licensing.services.state import get_state
        return get_state().is_blocked()
    except Exception:
        return False


def _customer_from_token(token):
    """Resolve the authenticated Customer from a bearer token, or None. Mirrors
    smartfood.security.customer_required (token matched by SHA-256 digest)."""
    if not token:
        return None
    try:
        from smartfood.repositories import CustomerSessionRepository
        session = CustomerSessionRepository.get_by_token(token)
    except Exception:
        return None
    if not session or session.is_expired():
        return None
    customer = session.customer
    if customer is None or customer.is_blocked:
        return None
    return customer


class CustomerOrderConsumer(JsonWebsocketConsumer):
    """A customer watches their own bot order. Joins ``botorder_<id>``; the server
    pushes ``order.event`` frames. Inbound messages are ignored."""

    def connect(self):
        if _license_blocked():
            self.close(code=_CLOSE_FORBIDDEN)
            return
        order_id = self.scope['url_route']['kwargs'].get('order_id')
        customer = _customer_from_token(_handshake_token(self.scope))
        if customer is None:
            self.close(code=_CLOSE_AUTH)
            return
        # Own-order scope: a customer may only subscribe to an order they own.
        # Without this they could watch any order id (cross-customer leak).
        from smartfood.models import BotOrder
        if not BotOrder.objects.filter(id=order_id, customer=customer).exists():
            self.close(code=_CLOSE_FORBIDDEN)
            return
        self.group = botorder_group(order_id)
        async_to_sync(self.channel_layer.group_add)(self.group, self.channel_name)
        self.accept()
        self.send_json({'event': 'connected', 'data': {'order_id': int(order_id)}})

    def disconnect(self, code):
        if getattr(self, 'group', None):
            async_to_sync(self.channel_layer.group_discard)(self.group, self.channel_name)

    def receive_json(self, content, **kwargs):
        # Read-only subscription — the customer cannot drive anything from here.
        return

    # --- group_send handler (message type 'order.event') ------------------- #
    def order_event(self, message):
        self.send_json({'event': message['event'], 'data': message['data']})
