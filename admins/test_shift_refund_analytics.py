from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone


pytestmark = pytest.mark.django_db


def test_sale_and_refund_are_reported_in_their_own_shifts(
    regular_user, cashier_user, product,
):
    from admins.services.shift_analytics_service import (
        _cashier_shift_row,
        _hourly_daily,
        shift_handover_report,
    )
    from base.models import Order, OrderItem, OrderPayment, OrderRefund, Shift

    now = timezone.now()
    sale_shift = Shift.objects.create(
        user=cashier_user,
        status=Shift.Status.ENDED,
        start_time=now - timedelta(hours=4),
        end_time=now - timedelta(hours=3),
        branch_id='branch-a',
    )
    refund_shift = Shift.objects.create(
        user=cashier_user,
        status=Shift.Status.ENDED,
        start_time=now - timedelta(hours=2),
        end_time=now - timedelta(hours=1),
        branch_id='branch-a',
    )
    sale_time = sale_shift.start_time + timedelta(minutes=10)
    refund_time = refund_shift.start_time + timedelta(minutes=10)
    order = Order.objects.create(
        user=regular_user,
        cashier=cashier_user,
        branch_id='branch-a',
        status='CANCELED',
        is_paid=True,
        payment_method='CASH',
        paid_at=sale_time,
        subtotal='80.00',
        total_amount='80.00',
    )
    Order.objects.filter(pk=order.pk).update(created_at=sale_time)
    OrderItem.objects.create(
        order=order,
        product=product,
        quantity=1,
        price='80.00',
        original_price='80.00',
    )
    OrderPayment.objects.create(
        order=order,
        method='CASH',
        amount='80.00',
        branch_id='branch-a',
    )
    refund = OrderRefund.objects.create(
        order=order,
        shift=refund_shift,
        cashier=cashier_user,
        amount='80.00',
        cash_amount='80.00',
        drawer_cash_amount='80.00',
        refunded_at=refund_time,
        source=OrderRefund.Source.ORDER_CANCEL,
        source_id=str(order.uuid),
        reason='customer return',
        branch_id='branch-a',
    )

    sale_row = _cashier_shift_row(sale_shift, {})
    refund_row = _cashier_shift_row(refund_shift, {})
    assert Decimal(sale_row['money']['revenue']) == Decimal('80.00')
    assert Decimal(sale_row['money']['cash']) == Decimal('80.00')
    assert sale_row['items']['units_sold'] == 1
    assert sale_row['refunds']['count'] == 0
    assert Decimal(refund_row['money']['revenue']) == Decimal('-80.00')
    assert Decimal(refund_row['money']['cash']) == Decimal('-80.00')
    assert refund_row['items']['units_sold'] == -1
    assert refund_row['refunds']['count'] == 1

    distribution = _hourly_daily([sale_shift, refund_shift])
    assert sum(
        (Decimal(row['revenue']) for row in distribution['by_date']),
        Decimal('0'),
    ) == Decimal('0.00')

    sale_handover = shift_handover_report(sale_shift)
    refund_handover = shift_handover_report(refund_shift)
    assert sale_handover['products'][0]['units_sold'] == 1
    assert Decimal(sale_handover['products'][0]['revenue']) == Decimal('80.00')
    assert refund_handover['refunds'][0]['refund_id'] == refund.id
    assert refund_handover['products'][0]['units_sold'] == -1
    assert refund_handover['products'][0]['times_refunded'] == 1
    assert Decimal(refund_handover['products'][0]['revenue']) == Decimal('-80.00')
