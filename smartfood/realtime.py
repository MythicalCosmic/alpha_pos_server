"""Realtime spine for the customer Telegram Mini App.

A customer subscribes to ONE of their own orders over
``/ws/smartfood/orders/<id>/`` (joins group ``botorder_<id>``) and the server
pushes that order's lifecycle events (dispatched / rejected / canceled / kitchen
status). Every frame is ``{"event": "<name>", "data": <bot_order_dict>}`` — the
same envelope the courier layer uses, so the client parser stays uniform.

Cloud-only (smartfood is the server edition). Best-effort: a realtime failure
never breaks the order flow that triggered it. The webapp keeps its 8s
``/track`` poll as a fallback — this is the low-latency layer, not the only one.
"""
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger('smartfood.realtime')


def botorder_group(order_id):
    """One group per order so a customer only ever receives THEIR order — never
    a global feed (which would leak every customer's orders)."""
    return f'botorder_{order_id}'


def _group_send(group, message):
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(group, message)
    except Exception:  # noqa: BLE001 — realtime is best-effort, never fatal
        logger.debug('smartfood realtime send failed (group=%s)', group, exc_info=True)


def publish_bot_order_event(bot_order_id, event):
    """Server -> the customer watching bot order <id>. Re-reads the committed
    order so the payload reflects final state — call via ``transaction.on_commit``
    so the client never sees a status that was rolled back."""
    from smartfood.models import BotOrder
    from smartfood.serializers import bot_order_dict
    order = (BotOrder.objects.prefetch_related('items')
             .select_related('pos_order', 'customer').filter(id=bot_order_id).first())
    if order is None:
        return
    _group_send(
        botorder_group(bot_order_id),
        {'type': 'order.event', 'event': event, 'data': bot_order_dict(order)},
    )
