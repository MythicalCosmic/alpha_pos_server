"""The realtime spine for the courier layer.

Every frame on the wire is ``{"event": "<name>", "data": {...snake_case...}}``
— the same envelope the payment events use, so the client parser stays uniform.

`push_courier_event` is the single funnel: REST handlers, the kitchen-ready
signal and (later) payment webhooks all call it, and it fans out to:
  * ``courier_<id>``  — the courier app   (CourierConsumer, /ws/courier/)
  * ``branch_<id>``   — the cashier desktop (CashierConsumer, /ws/cashier/)
  * the existing ``orders`` group           (OrderQueueConsumer, /ws/orders/)
    so the current desktop order feed also receives courier events without a
    second socket.

Channel-layer groups (string ids):
  courier_<courier.pk>     branch_<branch_id>
"""
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger('couriers.realtime')

# Reuse the existing in-store order feed group the desktop already listens on.
try:
    from core.realtime.consumers import ORDERS_GROUP
except Exception:  # pragma: no cover - core always present on the server edition
    ORDERS_GROUP = 'orders'


def courier_group(courier_id):
    return f'courier_{courier_id}'


def branch_group(branch_id):
    return f'branch_{branch_id or "cloud"}'


def _group_send(group, message):
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(group, message)
    except Exception:  # noqa: BLE001 — realtime is best-effort, never fatal
        logger.debug('courier realtime send failed (group=%s)', group, exc_info=True)


def send_to_courier(courier_id, event, data):
    """Server -> a single courier (order.assigned / order.ready / order.status …)."""
    _group_send(courier_group(courier_id),
                {'type': 'courier.event', 'event': event, 'data': data})


def send_to_cashiers(branch_id, event, data):
    """Server -> the cashier desktop(s) of a branch (courier.location, order.status…).
    Emitted to the per-branch group AND the legacy `orders` feed group."""
    _group_send(branch_group(branch_id),
                {'type': 'cashier.event', 'event': event, 'data': data})
    # Legacy desktop order feed: OrderQueueConsumer.broadcast forwards payload as-is.
    _group_send(ORDERS_GROUP,
                {'type': 'broadcast', 'payload': {'event': event, 'data': data}})


def push_courier_event(event, *, courier_id=None, branch_id=None, data=None):
    """The one funnel. Routes an event to the courier and/or the cashiers."""
    data = data or {}
    if courier_id is not None:
        send_to_courier(courier_id, event, data)
    if branch_id is not None:
        send_to_cashiers(branch_id, event, data)
