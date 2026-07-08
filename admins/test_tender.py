"""Canonical tender attribution: the fallback ladder + the exact-sum invariant.

MIXED is never a bucket. cash is DERIVED (total - noncash), so the customer's
change never inflates it. Orders whose tender cannot be determined go to
`unknown` -- never silently to cash.
"""
import secrets
from decimal import Decimal

import pytest

from base.services.tender import (
    BUCKETS, breakdown_for_orders, empty_split, split_from_rows, unattributed_orders,
)

pytestmark = pytest.mark.django_db
D = Decimal


def _sums_to(split, total):
    return sum((split[b] for b in BUCKETS), D('0')) == D(str(total))


# ---------- pure ladder (no DB) ----------

def test_till_split_derives_cash_ignoring_change():
    # bill 53,000; customer hands 100,000 cash + 18,000 uzcard. OP stores the
    # TENDERED cash (100,000). Derived cash must be the bill portion: 35,000.
    s, d = split_from_rows(53000, 'MIXED', [('CASH', 100000), ('UZCARD', 18000)])
    assert s['cash'] == D('35000') and s['card'] == D('18000')
    assert s['payme'] == 0 and s['unknown'] == 0
    assert d['UZCARD'] == D('18000')
    assert _sums_to(s, 53000)


def test_pure_cash_over_tender():
    # order #2774 on prod: 5,000 bill, 100,000 cash row (a 100k note).
    s, _ = split_from_rows(5000, 'CASH', [('CASH', 100000)])
    assert s['cash'] == D('5000') and _sums_to(s, 5000)


def test_all_noncash_leaves_zero_cash():
    s, _ = split_from_rows(60000, 'HUMO', [('HUMO', 60000)])
    assert s['cash'] == 0 and s['card'] == D('60000') and _sums_to(s, 60000)


def test_uzcard_and_humo_both_fold_into_card_with_detail():
    s, d = split_from_rows(50000, 'MIXED', [('UZCARD', 20000), ('HUMO', 30000)])
    assert s['card'] == D('50000') and s['cash'] == 0
    assert d['UZCARD'] == D('20000') and d['HUMO'] == D('30000')


def test_payme_stays_its_own_tender():
    s, _ = split_from_rows(10000, 'PAYME', [('PAYME', 10000)])
    assert s['payme'] == D('10000') and s['card'] == 0 and s['cash'] == 0


# ---------- fallback ladder: no OrderPayment rows ----------

@pytest.mark.parametrize('method,bucket', [
    ('CASH', 'cash'), (None, 'cash'), ('', 'cash'),
    ('UZCARD', 'card'), ('HUMO', 'card'), ('CARD', 'card'),
    ('PAYME', 'payme'),
])
def test_no_lines_falls_back_to_rolled_up_method(method, bucket):
    s, _ = split_from_rows(70000, method, [])
    assert s[bucket] == D('70000') and _sums_to(s, 70000)


def test_mixed_without_lines_is_unknown_never_cash():
    # An admin-minted MIXED order with no payment lines is UNRESOLVABLE.
    s, _ = split_from_rows(100000, 'MIXED', [])
    assert s['unknown'] == D('100000')
    assert s['cash'] == 0 and s['card'] == 0
    assert _sums_to(s, 100000)


def test_orderpayment_row_with_illegal_method_is_unknown():
    # OrderPayment.method inherits Order.PaymentMethod.choices -> 'MIXED' is
    # model-legal. It must NOT be treated as non-cash by an exclude().
    s, _ = split_from_rows(53000, 'MIXED', [('MIXED', 53000)])
    assert s['unknown'] == D('53000') and s['cash'] == 0
    assert _sums_to(s, 53000)


def test_noncash_exceeding_total_goes_to_unknown_not_clamped():
    # stale OP rows after unpay->repay: noncash 71,000 on a 53,000 order.
    s, _ = split_from_rows(53000, 'MIXED', [('UZCARD', 18000), ('UZCARD', 53000)])
    assert s['unknown'] == D('53000')
    assert s['cash'] == 0 and s['card'] == 0
    assert _sums_to(s, 53000)          # invariant survives the defect


def test_courier_card_at_door_is_card_not_cash():
    # THE regression the audit caught: no OrderPayment rows, courier CARD collection.
    s, _ = split_from_rows(60000, 'UZCARD', [], courier_rows=[('UZCARD', 60000)])
    assert s['card'] == D('60000') and s['cash'] == 0


def test_courier_cash_plus_card_split():
    s, _ = split_from_rows(50000, 'MIXED', [], courier_rows=[('CASH', 20000), ('UZCARD', 30000)])
    assert s['cash'] == D('20000') and s['card'] == D('30000') and _sums_to(s, 50000)


def test_zero_total_attributes_nothing():
    s, _ = split_from_rows(0, 'CASH', [('CASH', 1000)])   # 100% discount
    assert s == empty_split()


# ---------- aggregate over a queryset ----------

def _u():
    from base.models import User
    return User.objects.create(email=f'{secrets.token_hex(4)}@x.local', first_name='U',
                               last_name='X', role='CASHIER', status='ACTIVE', password='!')


def _order(total, method, lines=()):
    from base.models import Order, OrderPayment
    u = _u()
    o = Order.objects.create(user=u, cashier=u, order_type='HALL', status='COMPLETED',
                             is_paid=True, payment_method=method,
                             total_amount=D(total), subtotal=D(total),
                             display_id=Order.objects.count() + 1)
    for m, a in lines:
        OrderPayment.objects.create(order=o, method=m, amount=D(a))
    return o


def test_breakdown_for_orders_sums_exactly_to_revenue():
    from base.models import Order
    from django.db.models import Sum
    _order(53000, 'MIXED', [('CASH', 100000), ('UZCARD', 18000)])   # change + split
    _order(60000, 'HUMO', [('HUMO', 60000)])
    _order(10000, 'PAYME', [('PAYME', 10000)])
    _order(20000, 'CASH', [('CASH', 20000)])
    _order(30000, 'MIXED', [])                                      # unresolvable -> unknown

    qs = Order.objects.filter(is_deleted=False, is_paid=True).exclude(status='CANCELED')
    split, detail = breakdown_for_orders(qs)
    revenue = qs.aggregate(s=Sum('total_amount'))['s']

    assert split['cash'] == D('55000')     # 35,000 (derived) + 20,000
    assert split['card'] == D('78000')     # 18,000 uzcard + 60,000 humo
    assert split['payme'] == D('10000')
    assert split['unknown'] == D('30000')  # the MIXED-without-lines order
    assert sum((split[b] for b in BUCKETS), D('0')) == revenue   # EXACT
    assert detail['UZCARD'] == D('18000') and detail['HUMO'] == D('60000')


def test_canary_flags_noncash_order_without_payment_lines():
    from base.models import Order
    _order(20000, 'CASH', [('CASH', 20000)])      # fine
    _order(50000, 'UZCARD', [])                   # card sale, no lines -> canary
    qs = Order.objects.filter(is_deleted=False, is_paid=True).exclude(status='CANCELED')
    flagged = unattributed_orders(qs)
    assert flagged.count() == 1
    assert flagged.first().payment_method == 'UZCARD'
