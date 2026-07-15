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
    cancelled = _order(c, 100000, mid, is_paid=True, status='CANCELED')
    from base.models import OrderRefund
    OrderRefund.objects.create(
        order=cancelled, shift=s, cashier=c, branch_id=cancelled.branch_id,
        amount=Decimal('100000'), cash_amount=Decimal('100000'),
        drawer_cash_amount=Decimal('100000'),
        source=OrderRefund.Source.ORDER_CANCEL,
        source_id='leaderboard-cancel', refunded_at=mid,
    )

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
    from base.models import OrderRefund
    OrderRefund.objects.create(
        order=cancelled, branch_id=cancelled.branch_id,
        amount=Decimal('50000'), cash_amount=Decimal('50000'),
        drawer_cash_amount=Decimal('50000'),
        source=OrderRefund.Source.ORDER_CANCEL,
        source_id='top-products-cancel', refunded_at=now,
    )

    rows = OrderItemRepository.get_top_products(
        date_from=now - timedelta(hours=1), date_to=now + timedelta(hours=1))
    cola = next(r for r in rows if r['product_id'] == p.id)
    assert cola['total_qty'] == 2, cola          # only the 2 paid units
    assert cola['total_revenue'] == Decimal('20000.00'), cola


def test_products_overview_excludes_unpaid_and_soft_deleted_lines():
    from base.models import Category, Product
    from admins.services.product_analytics_service import products_overview

    cat = Category.objects.create(name='Food', slug='food')
    product = Product.objects.create(name='Burger', price=Decimal('10000'), category=cat)
    cashier = _cashier()
    now = timezone.now()

    paid = _order(cashier, 20000, now, is_paid=True)
    _item(paid, product, 2, 10000)
    unpaid = _order(cashier, 50000, now, is_paid=False, status='OPEN')
    _item(unpaid, product, 5, 10000)
    removed = _order(cashier, 30000, now, is_paid=True)
    _item(removed, product, 3, 10000)
    removed.items.first().delete()

    day = timezone.localdate()
    data = products_overview(day, day)
    assert data['total_units'] == 2, data
    assert data['total_revenue'] == '20000', data


def test_products_overview_allocates_order_discount():
    from base.models import Category, Product
    from admins.services.product_analytics_service import products_overview

    cat = Category.objects.create(name='Discounted', slug='discounted')
    product = Product.objects.create(name='Combo', price=Decimal('10000'), category=cat)
    cashier = _cashier()
    now = timezone.now()
    order = _order(cashier, 8000, now, is_paid=True)
    order.subtotal = Decimal('10000')
    order.discount_amount = Decimal('2000')
    order.save(update_fields=['subtotal', 'discount_amount'])
    _item(order, product, 1, 10000)

    day = timezone.localdate()
    data = products_overview(day, day)
    assert data['total_revenue'] == '8000', data
    assert data['top_products'][0]['revenue'] == '8000', data
