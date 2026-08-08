"""Idempotent loyalty settlement driven by the linked POS order outcome."""

import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from smartfood.models import BotOrder, LoyaltyTransaction
from smartfood.services.loyalty_service import LoyaltyService


logger = logging.getLogger(__name__)


def _first_event(bot_order_id, kind, *, positive=None, reason_prefix=''):
    events = LoyaltyTransaction.objects.filter(
        bot_order_id=bot_order_id,
        kind=kind,
    )
    if positive is True:
        events = events.filter(points__gt=0)
    elif positive is False:
        events = events.filter(points__lt=0)
    if reason_prefix:
        events = events.filter(reason__startswith=reason_prefix)
    return events.order_by('created_at', 'id').first()


def _has_authoritative_settlement(pos_order):
    if (
        pos_order is None
        or pos_order.is_deleted
        or pos_order.status != pos_order.Status.COMPLETED
        or not pos_order.is_paid
        or pos_order.paid_at is None
    ):
        return False

    from base.models import Order
    from base.services.tender import tender_integrity_issues

    return not tender_integrity_issues(
        Order.objects.filter(pk=pos_order.pk),
        require_concrete=True,
    )


@transaction.atomic
def reconcile_bot_order_loyalty(bot_order_id):
    """Apply or reverse points exactly once from durable POS evidence.

    Earned points are released only after the linked POS order is both paid and
    completed with valid tender evidence. A linked POS cancellation restores
    points reserved at checkout and reverses any released earn. The timestamp
    fields on the locked BotOrder are the idempotency ledger for these effects.
    """
    bot_order = (
        BotOrder.objects.select_for_update()
        .filter(pk=bot_order_id)
        .first()
    )
    if bot_order is None:
        return False

    changed = []

    # Adopt transactions written by the pre-settlement implementation so an
    # upgrade cannot award or restore the same points for a second time.
    if bot_order.loyalty_earned_settled_at is None:
        legacy_earn = _first_event(
            bot_order.id,
            LoyaltyTransaction.Kind.EARN_ORDER,
            positive=True,
        )
        if legacy_earn is not None:
            bot_order.loyalty_earned_settled_at = legacy_earn.created_at
            changed.append('loyalty_earned_settled_at')
    if bot_order.loyalty_spend_restored_at is None:
        legacy_restore = _first_event(
            bot_order.id,
            LoyaltyTransaction.Kind.REFUND,
            positive=True,
        )
        if legacy_restore is not None:
            bot_order.loyalty_spend_restored_at = legacy_restore.created_at
            changed.append('loyalty_spend_restored_at')
    if bot_order.loyalty_earn_reversed_at is None:
        legacy_reversal = _first_event(
            bot_order.id,
            LoyaltyTransaction.Kind.ADJUST,
            positive=False,
            reason_prefix='Reversed earned points for canceled order ',
        )
        if legacy_reversal is not None:
            bot_order.loyalty_earn_reversed_at = legacy_reversal.created_at
            changed.append('loyalty_earn_reversed_at')

    pos_order = bot_order.pos_order
    canceled = (
        bot_order.status in {
            BotOrder.Status.CANCELED,
            BotOrder.Status.REJECTED,
        }
        or (
            pos_order is not None
            and pos_order.status == pos_order.Status.CANCELED
        )
    )

    if canceled:
        if (
            bot_order.loyalty_points_used > 0
            and bot_order.loyalty_spend_restored_at is None
        ):
            txn, _balance = LoyaltyService.record(
                bot_order.customer_id,
                LoyaltyTransaction.Kind.REFUND,
                bot_order.loyalty_points_used,
                reason=f'Restored points for canceled order {bot_order.code}',
                bot_order=bot_order,
            )
            bot_order.loyalty_spend_restored_at = txn.created_at
            changed.append('loyalty_spend_restored_at')
        if (
            bot_order.loyalty_points_earned > 0
            and bot_order.loyalty_earn_reversed_at is None
        ):
            if bot_order.loyalty_earned_settled_at is not None:
                txn, _balance = LoyaltyService.record(
                    bot_order.customer_id,
                    LoyaltyTransaction.Kind.ADJUST,
                    -bot_order.loyalty_points_earned,
                    reason=(
                        'Reversed earned points for canceled order '
                        f'{bot_order.code}'
                    ),
                    bot_order=bot_order,
                )
                resolved_at = txn.created_at
            else:
                resolved_at = timezone.now()
            bot_order.loyalty_earn_reversed_at = resolved_at
            changed.append('loyalty_earn_reversed_at')
    elif (
        bot_order.loyalty_points_earned > 0
        and bot_order.loyalty_earned_settled_at is None
        and _has_authoritative_settlement(pos_order)
    ):
        txn, _balance = LoyaltyService.record(
            bot_order.customer_id,
            LoyaltyTransaction.Kind.EARN_ORDER,
            bot_order.loyalty_points_earned,
            reason=f'Earned on completed order {bot_order.code}',
            bot_order=bot_order,
        )
        bot_order.loyalty_earned_settled_at = txn.created_at
        changed.append('loyalty_earned_settled_at')

    if changed:
        bot_order.save(update_fields=[*dict.fromkeys(changed), 'updated_at'])
    return bool(changed)


def reconcile_bot_order_loyalty_safe(bot_order_id):
    try:
        return reconcile_bot_order_loyalty(bot_order_id)
    except Exception:
        logger.exception(
            'Smart Food loyalty settlement failed (bot_order=%s)',
            bot_order_id,
        )
        return False


def reconcile_due_bot_order_loyalty(limit=100):
    """Bounded repair sweep for missed callbacks and pre-upgrade orders."""
    limit = max(1, min(int(limit), 500))
    candidates = BotOrder.objects.filter(
        Q(
            loyalty_points_earned__gt=0,
            loyalty_earned_settled_at__isnull=True,
            pos_order__status='COMPLETED',
            pos_order__is_paid=True,
            pos_order__paid_at__isnull=False,
        )
        | Q(
            loyalty_points_used__gt=0,
            loyalty_spend_restored_at__isnull=True,
        ) & (
            Q(status__in=[BotOrder.Status.CANCELED, BotOrder.Status.REJECTED])
            | Q(pos_order__status='CANCELED')
        )
        | Q(
            loyalty_points_earned__gt=0,
            loyalty_earn_reversed_at__isnull=True,
            pos_order__status='CANCELED',
        )
    ).order_by('id').values_list('id', flat=True)[:limit]
    order_ids = list(candidates)
    reconciled = sum(
        bool(reconcile_bot_order_loyalty_safe(order_id))
        for order_id in order_ids
    )
    return {'checked': len(order_ids), 'reconciled': reconciled}
