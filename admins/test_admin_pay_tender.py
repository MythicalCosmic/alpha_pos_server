"""Admin mark_as_paid now writes the OrderPayment tender line(s).

Previously it set only is_paid/payment_method/paid_at, so a cloud-paid sale had no
tender lines. MIXED stays OUTPUT-only: a split is recorded by sending `payments`.
"""
import secrets
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from admins.services.order_service import AdminOrderService

pytestmark = pytest.mark.django_db
D = Decimal


def _cashier():
    from base.models import User
    return User.objects.create(email=f'{secrets.token_hex(4)}@x.local', first_name='C',
                               last_name='X', role='CASHIER', status='ACTIVE', password='!')


def _unpaid_order(cashier, total):
    from base.models import Order, Shift
    Shift.objects.get_or_create(
        user=cashier,
        status='ACTIVE',
        defaults={'start_time': timezone.now() - timedelta(hours=1)},
    )
    return Order.objects.create(user=cashier, cashier=cashier, order_type='HALL',
                                status='COMPLETED', is_paid=False,
                                total_amount=D(total), subtotal=D(total),
                                display_id=Order.objects.count() + 1)


def _lines(order):
    from base.models import OrderPayment
    return sorted((p.method, p.amount) for p in
                  OrderPayment.objects.filter(order=order, is_deleted=False))


def test_single_tender_writes_one_line():
    c = _cashier()
    o = _unpaid_order(c, 60000)
    _, st = AdminOrderService.mark_as_paid(o.id, payment_method='UZCARD')
    assert st == 200
    o.refresh_from_db()
    assert o.is_paid and o.payment_method == 'UZCARD'
    assert _lines(o) == [('UZCARD', D('60000.00'))]


def test_split_payments_write_lines_and_roll_up_to_mixed():
    c = _cashier()
    o = _unpaid_order(c, 53000)
    _, st = AdminOrderService.mark_as_paid(o.id, payments=[
        {'method': 'HUMO', 'amount': '35000'}, {'method': 'UZCARD', 'amount': '18000'}])
    assert st == 200
    o.refresh_from_db()
    assert o.payment_method == 'MIXED'
    assert _lines(o) == [('HUMO', D('35000')), ('UZCARD', D('18000'))]


def test_bare_mixed_is_rejected():
    c = _cashier()
    o = _unpaid_order(c, 10000)
    body, st = AdminOrderService.mark_as_paid(o.id, payment_method='MIXED')
    assert st == 422, body


def test_noncash_overpayment_rejected():
    c = _cashier()
    o = _unpaid_order(c, 10000)
    _, st = AdminOrderService.mark_as_paid(o.id, payments=[{'method': 'HUMO', 'amount': '20000'}])
    assert st == 422


def test_short_payment_rejected():
    c = _cashier()
    o = _unpaid_order(c, 10000)
    _, st = AdminOrderService.mark_as_paid(o.id, payments=[{'method': 'UZCARD', 'amount': '5000'}])
    assert st == 422


def test_unpay_preserves_tender_evidence_and_appends_refund():
    from base.models import OrderRefund

    c = _cashier()
    o = _unpaid_order(c, 40000)
    AdminOrderService.mark_as_paid(o.id, payment_method='UZCARD')
    assert _lines(o) == [('UZCARD', D('40000.00'))]
    _, st = AdminOrderService.mark_as_paid(o.id, payment_method='UZCARD')  # already paid
    assert st == 400
    _, st = AdminOrderService.mark_as_unpaid(o.id)
    assert st == 200
    o.refresh_from_db()
    assert o.is_paid is True
    assert o.status == 'CANCELED'
    assert _lines(o) == [('UZCARD', D('40000.00'))]
    assert OrderRefund.objects.filter(order=o, amount=D('40000.00')).exists()


def test_settlement_equals_revenue_for_admin_paid_orders():
    """The FE's actual criterion: an admin-paid sale must be visible to per-tender
    shift settlement, so sum(expected tenders) == revenue (before cashbox expenses)."""
    from base.models import Order, Shift
    from cashbox.services.drawer import expected_payment_totals
    from django.db.models import Sum

    c = _cashier()
    now = timezone.now()
    s = Shift.objects.create(user=c, status='ACTIVE', start_time=now - timedelta(hours=3))

    o1 = _unpaid_order(c, 100000)
    o2 = _unpaid_order(c, 60000)
    o3 = _unpaid_order(c, 53000)
    AdminOrderService.mark_as_paid(o1.id, payment_method='PAYME')
    AdminOrderService.mark_as_paid(o2.id, payment_method='HUMO')
    AdminOrderService.mark_as_paid(o3.id, payments=[
        {'method': 'HUMO', 'amount': '35000'}, {'method': 'UZCARD', 'amount': '18000'}])

    revenue = Order.objects.filter(cashier=c, is_paid=True).exclude(
        status='CANCELED').aggregate(s=Sum('total_amount'))['s']
    t = expected_payment_totals(s)
    assert sum(t.values()) == revenue == D('213000')
    assert t['CASH'] == D('0')
    assert t['PAYME'] == D('100000')
    assert t['HUMO'] == D('95000')
    assert t['UZCARD'] == D('18000')
