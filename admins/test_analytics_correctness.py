"""Regression: revenue analytics must count only PAID, non-cancelled orders, and
the cashier leaderboard AOV must divide by PAID orders, not total orders.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db


def _cashier():
    from base.models import User
    return User.objects.create(email='c@x.local', first_name='C', last_name='X',
                               role='CASHIER', status='ACTIVE', password='!')


def _order(cashier, total, when, is_paid=True, status='COMPLETED'):
    from base.models import Order
    o = Order.objects.create(
        user=cashier, cashier=cashier, order_type='HALL', status=status,
        is_paid=is_paid, payment_method='CASH' if is_paid else None,
        total_amount=Decimal(total), subtotal=Decimal(total),
        display_id=Order.objects.count() + 1)
    Order.objects.filter(pk=o.pk).update(created_at=when, paid_at=when if is_paid else None)
    o.refresh_from_db()
    return o


def _item(order, product, qty, price):
    from base.models import OrderItem
    OrderItem.objects.create(order=order, product=product, quantity=qty,
                             price=Decimal(price), original_price=Decimal(price))


def test_leaderboard_aov_uses_paid_denominator():
    from base.models import Shift
    from admins.services.shift_analytics_service import _cashier_shift_row, _cashier_leaderboard
    c = _cashier()
    end = timezone.now()
    start = end - timedelta(hours=4)
    mid = end - timedelta(hours=2)
    s = Shift.objects.create(user=c, status='ENDED', start_time=start, end_time=end)

    _order(c, 100000, mid, is_paid=True, status='COMPLETED')   # paid
    _order(c, 100000, mid, is_paid=True, status='COMPLETED')   # paid  -> revenue 200000, paid=2
    _order(c, 100000, mid, is_paid=False, status='OPEN')       # unpaid
    _order(c, 100000, mid, is_paid=False, status='OPEN')       # unpaid -> total orders = 5
    _order(c, 100000, mid, is_paid=True, status='CANCELED')    # cancelled (excluded from revenue)

    row = _cashier_shift_row(s, {})
    board = _cashier_leaderboard([row])
    entry = board[0]
    assert entry['revenue'] == '200000.00', entry
    # AOV = 200000 / 2 paid  (was 200000 / 5 total = 40000 with the bug)
    assert entry['avg_order_value'] == '100000.00', entry


def test_top_products_excludes_unpaid_and_cancelled():
    from base.models import Category, Product
    from base.repositories.order_item import OrderItemRepository
    cat = Category.objects.create(name='Drinks', slug='drinks')
    p = Product.objects.create(name='Cola', price=Decimal('10000'), category=cat)
    c = _cashier()
    now = timezone.now()

    paid = _order(c, 20000, now, is_paid=True, status='COMPLETED')
    _item(paid, p, 2, 10000)          # 20000 paid revenue
    unpaid = _order(c, 30000, now, is_paid=False, status='OPEN')
    _item(unpaid, p, 3, 10000)        # must NOT count
    cancelled = _order(c, 50000, now, is_paid=True, status='CANCELED')
    _item(cancelled, p, 5, 10000)     # must NOT count

    rows = OrderItemRepository.get_top_products(
        date_from=now - timedelta(hours=1), date_to=now + timedelta(hours=1))
    cola = next(r for r in rows if r['product_id'] == p.id)
    assert cola['total_qty'] == 2, cola          # only the 2 paid units
    assert cola['total_revenue'] == Decimal('20000.00'), cola
