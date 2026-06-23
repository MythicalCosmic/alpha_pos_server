"""Customer realtime bridge (Phase 1).

When a dispatched bot order's linked POS order changes status on the cloud
(PREPARING -> READY -> COMPLETED -> CANCELED — whether driven by the till's sync
or the courier service), push the updated order to the customer's Mini App so
they see kitchen/delivery progress live. The customer payload (bot_order_dict)
already carries ``pos_order.status``, so re-publishing the order surfaces the new
stage with no schema change.

Mirrors couriers.signals: we OBSERVE base.Order, we never own its status.
Server edition only (smartfood is not installed on the till)."""
import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from base.models import Order

logger = logging.getLogger('smartfood.signals')


@receiver(post_save, sender=Order, dispatch_uid='smartfood_customer_order_status')
def _customer_order_status_bridge(sender, instance, created, **kwargs):
    # A bare create carries no status delta the customer cares about, and at
    # dispatch the bot_order<->pos_order link isn't set yet (the explicit
    # 'dispatched' event handles that moment). Only react to later updates.
    if created:
        return
    try:
        bot_order = instance.bot_order      # reverse OneToOne; raises if unlinked
    except Exception:
        return
    bo_id = bot_order.id
    from smartfood.realtime import publish_bot_order_event
    transaction.on_commit(lambda: publish_bot_order_event(bo_id, 'status'))
