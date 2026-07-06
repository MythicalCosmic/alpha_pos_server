"""Regression: Payme is its OWN tender in the cashier shift row.

Bug: _cashier_shift_row computed card = uzcard + humo + payme, folding Payme into
the card figure while the drawer/dashboard treat Payme as a standalone tender.
Fix: card = uzcard + humo only; Payme surfaced separately as money.payme.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from admins.services.shift_analytics_service import _cashier_shift_row

pytestmark = pytest.mark.django_db


def _cashier():
    from base.models import User
    return User.objects.create(email='ruxsora@x.local', first_name='Rux', last_name='Sora',
                               role='CASHIER', status='ACTIVE', password='!')


def _shift(user, start, end):
    from base.models import Shift
    return Shift.objects.create(user=user, status='ENDED', start_time=start, end_time=end)


def _paid_order(cashier, method, total, when):
    """A paid, non-canceled order pinned inside the shift window (created_at and
    paid_at are auto/So overwrite them after insert)."""
    from base.models import Order
    o = Order.objects.create(
        user=cashier, cashier=cashier, order_type='HALL', status='COMPLETED',
        is_paid=True, payment_method=method,
        total_amount=Decimal(total), subtotal=Decimal(total),
        display_id=Order.objects.count() + 1)
    Order.objects.filter(pk=o.pk).update(created_at=when, paid_at=when)
    o.refresh_from_db()
    return o


def test_payme_is_own_tender_not_folded_into_card():
    c = _cashier()
    end = timezone.now()
    start = end - timedelta(hours=4)
    mid = end - timedelta(hours=2)
    s = _shift(c, start, end)

    _paid_order(c, 'UZCARD', 100000, mid)
    _paid_order(c, 'HUMO', 50000, mid)
    _paid_order(c, 'PAYME', 30000, mid)
    _paid_order(c, 'CASH', 20000, mid)

    row = _cashier_shift_row(s, {})
    money = row['money']

    # card = Uzcard + Humo ONLY (was 180000 when Payme was folded in)
    assert money['card'] == '150000.00', money
    # Payme surfaced as its own tender
    assert money['payme'] == '30000.00', money
    assert money['cash'] == '20000.00'
    assert money['revenue'] == '200000.00'
    # payment_mix keeps every tender broken out and reconciles to revenue
    mix = money['payment_mix']
    assert mix == {'CASH': '20000.00', 'UZCARD': '100000.00', 'HUMO': '50000.00',
                   'PAYME': '30000.00', 'MIXED': '0.00'}
    mix_sum = sum(Decimal(v) for v in mix.values())
    assert mix_sum == Decimal(money['revenue'])
    # card + cash + payme (+mixed) == revenue — no tender double-counted or dropped
    assert (Decimal(money['cash']) + Decimal(money['card'])
            + Decimal(money['payme']) + Decimal(mix['MIXED'])) == Decimal(money['revenue'])
