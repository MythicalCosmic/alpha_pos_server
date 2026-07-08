"""Drawer expected-cash must be DERIVED (bill portion), not the raw tendered cash.

Two bugs this locks down:
 1. OrderPayment stores the cash TENDERED, so a customer's change inflated the
    drawer's expected CASH and flagged the cashier short by exactly the change.
 2. An order with no payment lines (admin/courier pay) must be bucketed by its
    rolled-up method, not fall into cash because cash is the residual.
"""
import secrets
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db
D = Decimal


def _cashier():
    from base.models import User
    return User.objects.create(email=f'{secrets.token_hex(4)}@x.local', first_name='C',
                               last_name='X', role='CASHIER', status='ACTIVE', password='!')


def _shift(user):
    from base.models import Shift
    now = timezone.now()
    return Shift.objects.create(user=user, status='ACTIVE', start_time=now - timedelta(hours=3))


def _paid_order(cashier, total, method, lines=(), when=None):
    from base.models import Order, OrderPayment
    when = when or (timezone.now() - timedelta(hours=1))
    o = Order.objects.create(user=cashier, cashier=cashier, order_type='HALL',
                             status='COMPLETED', is_paid=True, payment_method=method,
                             total_amount=D(total), subtotal=D(total),
                             display_id=Order.objects.count() + 1)
    Order.objects.filter(pk=o.pk).update(created_at=when, paid_at=when)
    for m, a in lines:
        OrderPayment.objects.create(order=o, method=m, amount=D(a))
    return o


def test_drawer_cash_excludes_the_customers_change():
    from cashbox.services.drawer import expected_payment_totals
    c = _cashier()
    s = _shift(c)
    # bill 50,000; cashier keyed CASH 35,000 (tendered) + UZCARD 20,000.
    # Only 30,000 cash actually belongs to the bill; 5,000 was change.
    _paid_order(c, 50000, 'MIXED', [('CASH', 35000), ('UZCARD', 20000)])

    t = expected_payment_totals(s)
    assert t['CASH'] == D('30000'), t     # was 35,000 (raw tendered) -> false 5k shortage
    assert t['UZCARD'] == D('20000'), t
    # expected tenders reconcile exactly to revenue
    assert t['CASH'] + t['UZCARD'] + t['HUMO'] + t['CARD'] + t['PAYME'] == D('50000')


def test_drawer_pure_cash_over_tender():
    from cashbox.services.drawer import drawer_cash
    c = _cashier()
    s = _shift(c)
    _paid_order(c, 5000, 'CASH', [('CASH', 100000)])   # 100k note for a 5k bill
    assert drawer_cash(s) == D('5000')                  # not 100,000


def test_drawer_card_order_without_payment_lines_is_not_cash():
    from cashbox.services.drawer import expected_payment_totals
    c = _cashier()
    s = _shift(c)
    # Admin-style pay: is_paid + payment_method, NO OrderPayment rows.
    _paid_order(c, 80000, 'UZCARD', lines=())
    t = expected_payment_totals(s)
    assert t['CASH'] == D('0'), t          # the drawer never received this money
    assert t['UZCARD'] == D('80000'), t


def test_drawer_cash_is_net_of_cashbox_expenses():
    from cashbox.models import CashboxExpense
    from cashbox.services.drawer import expected_payment_totals
    c = _cashier()
    s = _shift(c)
    _paid_order(c, 100000, 'CASH', [('CASH', 100000)])
    CashboxExpense.objects.create(shift=s, amount=D('30000'), comment='Suv')
    t = expected_payment_totals(s)
    assert t['CASH'] == D('70000'), t      # 100,000 collected - 30,000 paid out


def test_drawer_card_value_is_a_first_class_tender():
    from cashbox.services.drawer import expected_payment_totals
    c = _cashier()
    s = _shift(c)
    _paid_order(c, 40000, 'CARD', [('CARD', 40000)])   # smartfood-style 'CARD'
    t = expected_payment_totals(s)
    assert t['CARD'] == D('40000'), t      # previously vanished from every bucket
    assert t['CASH'] == D('0')
