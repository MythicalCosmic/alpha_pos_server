"""The 5 admin-panel data-gap fixes from the FE QA pass:
1) user created_at  2) category product_count  3) orders/stats status_counts +
payment_counts  4) operations prepByCategory mins/target  5) dashboard range
category_stats."""
import secrets
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db


def _u():
    from base.models import User
    return User.objects.create(email=f'g{secrets.token_hex(4)}@x.local', first_name='a',
                               last_name='b', role='CASHIER', status='ACTIVE', password='!')


def _category(name):
    from base.models import Category
    return Category.objects.create(name=name)


def _product(category, name, price='10000'):
    from base.models import Product
    return Product.objects.create(name=name, price=price, category=category)


def _order(status='COMPLETED', paid=True, total='100000', method='CASH'):
    from base.models import Order
    return Order.objects.create(
        user=_u(), cashier=_u(), status=status, is_paid=paid, display_id=1,
        subtotal=total, total_amount=total,
        payment_method=(method if paid else None),
        paid_at=(timezone.now() if paid else None))


# 1) GET /users — created_at populated
def test_user_created_at_present():
    from admins.services.user_service import _serialize_user
    u = _u()
    assert u.created_at is not None
    assert _serialize_user(u)['created_at'] is not None


# 2) GET /categories — product_count (excludes soft-deleted)
def test_category_product_count_excludes_soft_deleted():
    from admins.services.category_service import AdminCategoryService
    c = _category('Pizza')
    _product(c, 'Margherita')
    _product(c, 'Pepperoni')
    dead = _product(c, 'Discontinued')
    dead.is_deleted = True
    dead.save()
    body, st = AdminCategoryService.get_all_categories()
    assert st == 200
    row = next(r for r in body['data']['categories'] if r['id'] == c.id)
    assert row['product_count'] == 2                 # soft-deleted one excluded


# 3) GET /orders/stats — status_counts + payment_counts
def test_order_stats_status_and_payment_counts():
    from admins.services.order_service import AdminOrderService
    _order(status='OPEN', paid=False)
    _order(status='PREPARING', paid=False)
    _order(status='COMPLETED', paid=True)
    _order(status='CANCELED', paid=False, total='0')
    body, st = AdminOrderService.get_order_stats()
    assert st == 200
    sc = body['data']['status_counts']
    assert set(sc) == {'OPEN', 'PREPARING', 'READY', 'COMPLETED', 'CANCELED'}
    assert sc == {'OPEN': 1, 'PREPARING': 1, 'READY': 0, 'COMPLETED': 1, 'CANCELED': 1}
    pc = body['data']['payment_counts']
    assert set(pc) == {'PAID', 'UNPAID'}
    assert pc['PAID'] == 1 and pc['UNPAID'] == 1      # UNPAID excludes OPEN & CANCELED


# 4) GET /dashboard/operations — prepByCategory mins + target
def test_operations_prep_mins_and_target():
    from base.models import OrderItem
    from admins.services.operations_dashboard_service import operations_dashboard
    c = _category('Burgers')
    p = _product(c, 'Cheeseburger')
    o = _order(status='COMPLETED')
    o.ready_at = o.created_at + timedelta(minutes=6)
    o.save(update_fields=['ready_at'])
    OrderItem.objects.create(order=o, product=p, quantity=1, price=Decimal('10000'))
    data = operations_dashboard()
    row = next(r for r in data['prepByCategory'] if r['category'] == 'Burgers')
    assert row['mins'] == 6.0 and row['target'] == 15.0
    assert row['avg_prep_seconds'] == 360            # kept for back-compat


# 5) GET /dashboard?from=&to= — category_stats in the range payload
def test_dashboard_range_category_stats():
    from base.models import OrderItem
    from admins.services import dashboard_service
    c = _category('Drinks')
    p = _product(c, 'Cola', price='8000')
    o = _order(status='COMPLETED')
    OrderItem.objects.create(order=o, product=p, quantity=3, price=Decimal('8000'))
    data = dashboard_service.get_range()             # default = today
    assert 'category_stats' in data
    row = next(r for r in data['category_stats'] if r['category'] == 'Drinks')
    assert row['category_id'] == c.id and row['quantity'] == 3
    assert Decimal(row['revenue']) == 24000          # 3 x 8000
