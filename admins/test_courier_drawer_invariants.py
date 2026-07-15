from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _paid_delivery(user, *, total='100.00'):
    from base.models import Order

    return Order.objects.create(
        user=user,
        cashier=user,
        order_type=Order.OrderType.DELIVERY,
        status='COMPLETED',
        is_paid=True,
        payment_method='CASH',
        paid_at=timezone.now(),
        subtotal=total,
        total_amount=total,
        branch_id='branch-a',
    )


@override_settings(DEPLOYMENT_MODE='local', BRANCH_ID='branch-a')
def test_courier_cash_never_enters_or_leaves_pos_drawer(cashier_user):
    from base.models import CashRegister, Shift
    from base.services.order_refund import record_external_provider_refund
    from cashbox.services.drawer import expected_payment_totals
    from couriers.models import CourierPayment

    cashier_user.branch_id = 'cloud'  # production shared staff identity
    cashier_user.save(update_fields=['branch_id'])
    shift = Shift.objects.create(
        user=cashier_user,
        status=Shift.Status.ACTIVE,
        start_time=timezone.now() - timedelta(hours=1),
        branch_id='branch-a',
    )
    order = _paid_delivery(cashier_user)
    CourierPayment.objects.create(
        order=order,
        courier=None,
        provider=CourierPayment.Provider.CASH,
        amount=100,
        status=CourierPayment.Status.PAID,
        external_id='courier-cash-only',
        branch_id='branch-a',
        paid_at=order.paid_at,
    )
    # Paying the order creates the per-branch register as the accounting lock.
    # Reuse that exact drawer instead of attempting a second live register.
    register = CashRegister.objects.get(branch_id='branch-a')
    assert register.current_balance == Decimal('0.00')

    assert expected_payment_totals(shift)['CASH'] == Decimal('0.00')
    refund, created = record_external_provider_refund(
        order,
        method='CASH',
        amount='100.00',
        source_id='courier-cash-only',
        reason='gateway reversal',
    )
    assert created is True
    assert refund.cash_amount == Decimal('100.00')
    assert refund.drawer_cash_amount == Decimal('0.00')
    register.refresh_from_db()
    assert register.current_balance == Decimal('0.00')


@override_settings(DEPLOYMENT_MODE='local', BRANCH_ID='branch-a')
def test_mixed_till_and_courier_cash_reconciles_only_till_residual(
    cashier_user,
):
    from base.models import CashRegister, OrderPayment, Shift
    from base.services.order_refund import record_paid_order_refund
    from cashbox.services.drawer import expected_payment_totals
    from core.shifts.service import ShiftService
    from couriers.models import CourierPayment

    cashier_user.branch_id = 'cloud'
    cashier_user.save(update_fields=['branch_id'])
    now = timezone.now()
    sale_shift = Shift.objects.create(
        user=cashier_user,
        status=Shift.Status.ENDED,
        start_time=now - timedelta(hours=3),
        end_time=now - timedelta(hours=2),
        branch_id='branch-a',
    )
    refund_shift = Shift.objects.create(
        user=cashier_user,
        status=Shift.Status.ACTIVE,
        start_time=now - timedelta(hours=1),
        branch_id='branch-a',
    )
    order = _paid_delivery(cashier_user)
    sale_time = sale_shift.start_time + timedelta(minutes=10)
    type(order).objects.filter(pk=order.pk).update(paid_at=sale_time)
    order.paid_at = sale_time
    OrderPayment.objects.create(
        order=order,
        method='CASH',
        amount='60.00',
        branch_id='branch-a',
    )
    CourierPayment.objects.create(
        order=order,
        courier=None,
        provider=CourierPayment.Provider.CASH,
        amount=40,
        status=CourierPayment.Status.PAID,
        external_id='courier-mixed-cash',
        branch_id='branch-a',
        paid_at=sale_time,
    )
    # Paying the order creates the per-branch register as the accounting lock.
    # Seed the till portion on that same drawer; the courier's 40 never enters it.
    register = CashRegister.objects.get(branch_id='branch-a')
    register.current_balance = Decimal('60.00')
    register.save(update_fields=['current_balance'])

    assert expected_payment_totals(sale_shift)['CASH'] == Decimal('60.00')
    sale_stats = ShiftService._shift_stats(sale_shift, sale_shift.end_time)
    assert Decimal(sale_stats['payment_mix']['cash']) == Decimal('100.00')

    refund, created = record_paid_order_refund(
        order, cashier_user.id, reason='full cancellation',
    )
    assert created is True
    assert refund.cash_amount == Decimal('100.00')
    assert refund.drawer_cash_amount == Decimal('60.00')
    register.refresh_from_db()
    assert register.current_balance == Decimal('0.00')
    assert expected_payment_totals(refund_shift)['CASH'] == Decimal('-60.00')
    refund_stats = ShiftService._shift_stats(refund_shift, timezone.now())
    assert Decimal(refund_stats['payment_mix']['cash']) == Decimal('-100.00')
