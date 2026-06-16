"""Kitchen READY -> courier ``order.ready`` bridge.

The kitchen/order status lives on base.Order and is owned by the existing POS
logic. We don't rewrite it — we observe it: when an Order flips to READY and it
has a courier assignment still sitting at ASSIGNED, fire the courier's
``order.ready`` (WS + push), exactly once.
"""
import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from base.models import Order

logger = logging.getLogger('couriers.signals')


@receiver(post_save, sender=Order, dispatch_uid='couriers_order_ready_bridge')
def _order_ready_bridge(sender, instance, created, **kwargs):
    if created or getattr(instance, 'status', None) != 'READY':
        return
    try:
        from couriers.services import mark_ready
        # mark_ready is a no-op unless there's an ASSIGNED courier assignment.
        mark_ready(instance)
    except Exception:  # noqa: BLE001 — never let the courier bridge break a save
        logger.debug('order.ready courier bridge failed', exc_info=True)
