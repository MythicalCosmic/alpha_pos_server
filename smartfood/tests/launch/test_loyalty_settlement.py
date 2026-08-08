from decimal import Decimal
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.utils import timezone


pytestmark = pytest.mark.django_db(transaction=True)


def _linked_order(customer, cashier, *, status='PREPARING'):
    from base.models import Order
    from smartfood.models import BotOrder

    pos_order = Order.objects.create(
        user=cashier,
        cashier=cashier,
        status=status,
        order_type=Order.OrderType.DELIVERY,
        order_origin=Order.Origin.TELEGRAM,
        total_amount=Decimal('30000.00'),
        branch_id='branch-a',
    )
    bot_order = BotOrder.objects.create(
        customer=customer,
        client_order_id=uuid4(),
        request_fingerprint=uuid4().hex,
        status=BotOrder.Status.DISPATCHED,
        order_type=BotOrder.OrderType.DELIVERY,
        subtotal=Decimal('30000.00'),
        total=Decimal('30000.00'),
        loyalty_points_used=50,
        loyalty_points_earned=30,
        pos_order=pos_order,
    )
    return bot_order, pos_order


def test_points_settle_only_from_completed_paid_concrete_tender_and_reverse_once(
    customer,
    cashier,
):
    from base.models import Order, OrderPayment
    from smartfood.models import LoyaltyTransaction
    from smartfood.services.loyalty_service import LoyaltyService
    from smartfood.services.loyalty_settlement_service import (
        reconcile_bot_order_loyalty,
        reconcile_due_bot_order_loyalty,
    )

    customer.loyalty_points = 100
    customer.save(update_fields=['loyalty_points'])
    bot_order, pos_order = _linked_order(customer, cashier)
    LoyaltyService.record(
        customer.id,
        LoyaltyTransaction.Kind.SPEND_ORDER,
        -bot_order.loyalty_points_used,
        reason=f'Redeemed on order {bot_order.code}',
        bot_order=bot_order,
    )

    assert reconcile_bot_order_loyalty(bot_order.id) is False
    customer.refresh_from_db()
    assert customer.loyalty_points == 50

    pos_order.status = Order.Status.COMPLETED
    pos_order.save(update_fields=['status'])
    customer.refresh_from_db()
    assert customer.loyalty_points == 50

    action_id = uuid4()
    pos_order.is_paid = True
    pos_order.paid_at = timezone.now()
    pos_order.payment_method = Order.PaymentMethod.CASH
    pos_order.payment_action_id = action_id
    pos_order.save(update_fields=[
        'is_paid', 'paid_at', 'payment_method', 'payment_action_id',
    ])
    customer.refresh_from_db()
    assert customer.loyalty_points == 50

    OrderPayment.objects.create(
        order=pos_order,
        method=Order.PaymentMethod.CASH,
        amount=Decimal('30000.00'),
        payment_action_id=action_id,
        line_index=0,
        branch_id='branch-a',
    )
    assert reconcile_due_bot_order_loyalty(limit=10) == {
        'checked': 1,
        'reconciled': 1,
    }
    customer.refresh_from_db()
    bot_order.refresh_from_db()
    assert customer.loyalty_points == 80
    assert bot_order.loyalty_earned_settled_at is not None
    assert LoyaltyTransaction.objects.filter(
        bot_order=bot_order,
        kind=LoyaltyTransaction.Kind.EARN_ORDER,
    ).count() == 1

    assert reconcile_bot_order_loyalty(bot_order.id) is False
    customer.refresh_from_db()
    assert customer.loyalty_points == 80

    pos_order.status = Order.Status.CANCELED
    pos_order.save(update_fields=['status'])
    reconcile_bot_order_loyalty(bot_order.id)
    customer.refresh_from_db()
    bot_order.refresh_from_db()
    assert customer.loyalty_points == 100
    assert bot_order.loyalty_spend_restored_at is not None
    assert bot_order.loyalty_earn_reversed_at is not None
    assert LoyaltyTransaction.objects.filter(
        bot_order=bot_order,
        kind=LoyaltyTransaction.Kind.REFUND,
        points=50,
    ).count() == 1
    assert LoyaltyTransaction.objects.filter(
        bot_order=bot_order,
        kind=LoyaltyTransaction.Kind.ADJUST,
        points=-30,
    ).count() == 1

    assert reconcile_bot_order_loyalty(bot_order.id) is False
    customer.refresh_from_db()
    assert customer.loyalty_points == 100


def test_worker_sweep_repairs_preupgrade_earn_on_canceled_order_when_dispatch_off(
    settings,
    customer,
    cashier,
):
    from smartfood.models import LoyaltyTransaction
    from smartfood.services.loyalty_service import LoyaltyService

    settings.SMARTFOOD_AUTO_DISPATCH = False
    customer.loyalty_points = 0
    customer.save(update_fields=['loyalty_points'])
    bot_order, _pos_order = _linked_order(
        customer,
        cashier,
        status='CANCELED',
    )
    bot_order.loyalty_points_used = 0
    bot_order.save(update_fields=['loyalty_points_used'])
    LoyaltyService.record(
        customer.id,
        LoyaltyTransaction.Kind.EARN_ORDER,
        bot_order.loyalty_points_earned,
        reason=f'Earned on order {bot_order.code}',
        bot_order=bot_order,
    )
    customer.refresh_from_db()
    assert customer.loyalty_points == 30

    call_command(
        'process_smartfood_dispatch_jobs',
        once=True,
        interval=0.25,
        batch_size=10,
        verbosity=0,
    )

    customer.refresh_from_db()
    bot_order.refresh_from_db()
    assert customer.loyalty_points == 0
    assert bot_order.loyalty_earned_settled_at is not None
    assert bot_order.loyalty_earn_reversed_at is not None
    assert LoyaltyTransaction.objects.filter(
        bot_order=bot_order,
        kind=LoyaltyTransaction.Kind.EARN_ORDER,
        points=30,
    ).count() == 1
    assert LoyaltyTransaction.objects.filter(
        bot_order=bot_order,
        kind=LoyaltyTransaction.Kind.ADJUST,
        points=-30,
    ).count() == 1
