"""Owner dashboard endpoint tests."""
import secrets
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from base.repositories.session import SessionRepository
from admins.services.dashboard_service import get_today


pytestmark = pytest.mark.django_db


def _make_paid_order(user, total='100000', minutes_ago=10, cancelled=False):
    from base.models import Order
    settled_at = timezone.now() - timedelta(minutes=minutes_ago)
    o = Order.objects.create(
        user=user, phone_number='998900000001', order_type='PICKUP',
        status='CANCELED' if cancelled else 'COMPLETED',
        is_paid=not cancelled, payment_method='CASH' if not cancelled else None,
        paid_at=settled_at if not cancelled else None,
        total_amount=Decimal(total), subtotal=Decimal(total),
        display_id=Order.objects.count() + 1,
    )
    from base.models import Order as O
    O.objects.filter(pk=o.pk).update(
        created_at=settled_at,
        paid_at=settled_at if not cancelled else None,
    )
    o.refresh_from_db()
    return o


def _add_item(order, name, price, qty, slug=None):
    from base.models import Category, OrderItem, Product
    cat, _ = Category.objects.get_or_create(
        name='c', slug=slug or name.lower(),
    )
    p = Product.objects.create(
        name=name, price=Decimal(price), category=cat,
    )
    OrderItem.objects.create(
        order=order, product=p, quantity=qty,
        price=Decimal(price), original_price=Decimal(price),
    )
    return p


class TestGetTodayService:
    def test_revenue_and_count(self, regular_user):
        _make_paid_order(regular_user, total='100000')
        _make_paid_order(regular_user, total='50000')
        data = get_today()
        assert data['today']['revenue'] == '150000'
        assert data['today']['paid_orders'] == 2
        assert data['today']['orders'] == 2

    def test_excludes_yesterday(self, regular_user):
        _make_paid_order(regular_user, minutes_ago=60 * 30)  # 30h ago
        data = get_today()
        assert data['today']['revenue'] == '0'
        assert data['today']['orders'] == 0

    def test_cancelled_excluded_from_revenue_but_counted(self, regular_user):
        _make_paid_order(regular_user, total='100000', cancelled=True)
        _make_paid_order(regular_user, total='200000')
        data = get_today()
        assert data['today']['revenue'] == '200000'
        assert data['today']['orders'] == 2
        assert data['today']['cancelled'] == 1

    def test_open_orders_counted(self, regular_user):
        from base.models import Order
        Order.objects.create(
            user=regular_user, phone_number='998900000001',
            order_type='PICKUP', status='PREPARING',
            is_paid=False, total_amount=Decimal('10000'),
            subtotal=Decimal('10000'), display_id=1,
        )
        data = get_today()
        assert data['today']['open'] == 1

    def test_top_products_today(self, regular_user):
        o1 = _make_paid_order(regular_user, total='100000')
        _add_item(o1, 'Pizza', '50000', 2, slug='pizza')
        o2 = _make_paid_order(regular_user, total='30000')
        _add_item(o2, 'Salad', '30000', 1, slug='salad')

        data = get_today()
        top = data['top_products_today']
        # Pizza qty 2 > Salad qty 1.
        assert top[0]['product_name'] == 'Pizza'
        assert top[0]['quantity'] == 2

    def test_units_sold_excludes_unpaid_and_soft_deleted_items(self, regular_user):
        paid = _make_paid_order(regular_user, total='20000')
        _add_item(paid, 'Paid', '10000', 2, slug='paid')

        unpaid = _make_paid_order(regular_user, total='50000')
        unpaid.is_paid = False
        unpaid.payment_method = None
        unpaid.save(update_fields=['is_paid', 'payment_method'])
        _add_item(unpaid, 'Unpaid', '10000', 5, slug='unpaid')

        removed_order = _make_paid_order(regular_user, total='30000')
        _add_item(removed_order, 'Removed', '10000', 3, slug='removed')
        removed_order.items.first().delete()

        data = get_today()
        assert data['today']['units_sold'] == 2

    def test_clocked_in_lists_active_shifts(self, cashier_user):
        from base.models import Shift
        Shift.objects.create(
            user=cashier_user, status='ACTIVE',
            start_time=timezone.now() - timedelta(hours=2),
        )
        data = get_today()
        names = [s['name'] for s in data['clocked_in']]
        assert 'Cashier One' in names

    def test_low_stock_count_zero_when_no_items(self, regular_user):
        data = get_today()
        # Default (no stock items) → 0.
        assert data['low_stock_count'] == 0


@pytest.fixture
def admin_session(admin_user):
    from base.models import Session
    payload = secrets.token_hex(32)
    Session.objects.create(
        user_id=admin_user, ip_address='127.0.0.1', payload=SessionRepository.hash_token(payload),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return payload


class TestTodayEndpoint:
    def test_admin_can_fetch(self, admin_session, regular_user):
        _make_paid_order(regular_user)
        client = Client()
        resp = client.get(
            '/api/admins/dashboard/today',
            HTTP_AUTHORIZATION=f'Bearer {admin_session}',
        )
        assert resp.status_code == 200
        data = resp.json()['data']
        assert 'today' in data
        assert 'top_products_today' in data
        assert 'low_stock_count' in data
        assert 'clocked_in' in data

    def test_requires_auth(self):
        client = Client()
        resp = client.get('/api/admins/dashboard/today')
        assert resp.status_code == 401

    def test_cashier_blocked(self, cashier_user):
        from base.models import Session
        payload = secrets.token_hex(32)
        Session.objects.create(
            user_id=cashier_user, ip_address='127.0.0.1', payload=SessionRepository.hash_token(payload),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        client = Client()
        resp = client.get(
            '/api/admins/dashboard/today',
            HTTP_AUTHORIZATION=f'Bearer {payload}',
        )
        assert resp.status_code == 403
