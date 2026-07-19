"""Regression: manager reconciliation expected_cash must be NET of cashbox expenses.

Bug: reconcile() set expected_cash = shift.cash_collected (GROSS cash — Sum of CASH
order totals, no cashbox-expense subtraction). A cashier who collects 200,000 cash
and pays 50,000 of it out of the drawer as expenses physically has 150,000 left, but
the manager screen showed expected 200,000 → a FALSE 50,000 shortage. The per-tender
ShiftPaymentTotal already froze the correct net figure (150,000); reconcile now uses it.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db


def _paid_cash_order(cashier, total, when):
    """Paid CASH order + its OrderPayment row (the till pay path writes these),
    pinned inside the shift window."""
    from base.models import Order, OrderPayment
    o = Order.objects.create(
        user=cashier, cashier=cashier, order_type='HALL', status='COMPLETED',
        is_paid=True, payment_method='CASH',
        total_amount=Decimal(total), subtotal=Decimal(total),
        display_id=Order.objects.count() + 1)
    Order.objects.filter(pk=o.pk).update(created_at=when, paid_at=when)
    OrderPayment.objects.create(order=o, method='CASH', amount=Decimal(total))
    return o


def test_reconcile_expected_cash_is_net_of_expenses(cashier_user, admin_user):
    from base.models import Shift
    from cashbox.models import CashboxExpense, ShiftPaymentTotal
    from admins.services.shift_service import ShiftService

    # Reconciliation is deliberately branch-scoped. This scenario exercises a
    # control-centre administrator, so model that global identity explicitly
    # instead of inheriting the server node's ``BRANCH_ID`` from the shared
    # fixture (which would make it a manager for a different branch).
    admin_user.branch_id = 'cloud'
    admin_user.save(update_fields=['branch_id'])

    start = timezone.now() - timedelta(hours=1)
    mid = timezone.now() - timedelta(minutes=30)
    s = Shift.objects.create(user=cashier_user, start_time=start, status='ACTIVE')

    _paid_cash_order(cashier_user, 100000, mid)
    _paid_cash_order(cashier_user, 100000, mid)          # 200,000 gross cash collected
    CashboxExpense.objects.create(shift=s, amount=Decimal('50000'), comment='Suv')  # paid OUT

    res, st = ShiftService.end_shift(s.id, cashier_user.id, 'done')
    assert st == 200

    # The frozen settlement row is already net: 200,000 − 50,000 = 150,000.
    spt = ShiftPaymentTotal.objects.get(shift=s, method='CASH')
    assert spt.expected_amount == Decimal('150000.00')
    # ...and the shift's GROSS cash_collected is the un-netted 200,000 (the old bug source).
    s.refresh_from_db()
    assert s.cash_collected == Decimal('200000.00')

    # Manager counts the physical drawer (net) = 150,000 → should reconcile to ZERO.
    res2, st2 = ShiftService.reconcile(
        s.id, actual_cash='150000', notes='', reconciled_by_id=admin_user.id)
    assert st2 == 201, res2
    assert res2['data']['expected_cash'] == '150000.00', res2['data']   # was 200000.00 (bug)
    assert res2['data']['difference'] == '0.00', res2['data']           # was -50000.00 (false short)
