"""Shift performance + menu engineering analytics tests."""
import secrets
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from base.repositories.session import SessionRepository
from admins.services.analytics_service import menu_engineering, shift_performance


pytestmark = pytest.mark.django_db


def _make_shift(user, hours_long=4, status='COMPLETED'):
    from base.models import Shift
    start = timezone.now() - timedelta(hours=hours_long)
    end = timezone.now() if status == 'COMPLETED' else None
    return Shift.objects.create(
        user=user, status=status, start_time=start, end_time=end,
    )


def _make_order(user, cashier, status='COMPLETED', is_paid=True, total='100',
                ready_offset=None, created_offset=timedelta(hours=2)):
    from base.models import Order
    o = Order.objects.create(
        user=user, cashier=cashier, phone_number='998901111111',
        order_type='PICKUP', status=status, is_paid=is_paid,
        payment_method='CASH' if is_paid else None,
        total_amount=Decimal(total), subtotal=Decimal(total),
        display_id=Order.objects.count() + 1,
    )
    from base.models import Order as O
    created_at = timezone.now() - created_offset
    O.objects.filter(pk=o.pk).update(
        created_at=created_at,
        paid_at=created_at if is_paid else None,
    )
    o.refresh_from_db()
    if ready_offset is not None and status in ('READY', 'COMPLETED'):
        o.ready_at = o.created_at + ready_offset
        o.save(update_fields=['ready_at'])
    return o


def _add_item(order, name, price, qty, slug=None):
    from base.models import Category, OrderItem, Product
    cat, _ = Category.objects.get_or_create(name='c', slug=slug or name.lower())
    p = Product.objects.create(name=name, price=Decimal(price), category=cat)
    OrderItem.objects.create(
        order=order, product=p, quantity=qty,
        price=Decimal(price), original_price=Decimal(price),
    )
    return p


# ---- shift_performance service -----------------------------------------

class TestShiftPerformance:
    def test_basic_kpis(self, cashier_user, regular_user):
        shift = _make_shift(cashier_user)
        _make_order(regular_user, cashier_user, total='100')
        _make_order(regular_user, cashier_user, total='200')
        data = shift_performance(shift)
        assert data['orders_total'] == 2
        assert data['orders_paid'] == 2
        assert data['revenue'] == '300'

    def test_cancel_rate(self, cashier_user, regular_user):
        shift = _make_shift(cashier_user)
        _make_order(regular_user, cashier_user, status='COMPLETED', is_paid=True)
        _make_order(regular_user, cashier_user, status='CANCELED', is_paid=False)
        data = shift_performance(shift)
        assert data['orders_cancelled'] == 1
        assert data['cancel_rate_pct'] == 50.0

    def test_avg_prep_time(self, cashier_user, regular_user):
        shift = _make_shift(cashier_user)
        _make_order(regular_user, cashier_user, ready_offset=timedelta(minutes=5))
        _make_order(regular_user, cashier_user, ready_offset=timedelta(minutes=10))
        data = shift_performance(shift)
        # avg ~7.5 min = ~450s
        assert 400 <= data['avg_prep_seconds'] <= 500

    def test_orders_per_hour_for_4h_shift(self, cashier_user, regular_user):
        shift = _make_shift(cashier_user, hours_long=4)
        for _ in range(8):
            _make_order(regular_user, cashier_user)
        data = shift_performance(shift)
        assert data['orders_per_hour'] == 2.0


# ---- menu_engineering service ------------------------------------------

class TestMenuEngineering:
    def test_empty_window_returns_zero_counts(self):
        data = menu_engineering(date(2026, 5, 1), date(2026, 5, 30))
        assert data['summary']['stars'] == 0
        assert data['items'] == []

    def test_classifies_into_four_buckets(self, regular_user, cashier_user):
        # 4 products to force one in each bucket relative to the means.
        from base.models import Category, Product
        cat, _ = Category.objects.get_or_create(name='c', slug='c')

        def add(name, price, qty):
            p = Product.objects.create(name=name, price=Decimal(price), category=cat)
            o = _make_order(regular_user, cashier_user, total='100')
            _add_item_at_price(o, p, qty)
            return p

        # High qty + high price → Star
        add('A', '100000', 20)
        # High qty + low price → Plowhorse
        add('B', '5000', 18)
        # Low qty + high price → Puzzle
        add('C', '90000', 2)
        # Low qty + low price → Dog
        add('D', '4000', 1)

        data = menu_engineering(timezone.localdate() - timedelta(days=5), timezone.localdate())
        klasses = {i['product_name']: i['class'] for i in data['items']}
        assert klasses['A'] == 'Star'
        assert klasses['D'] == 'Dog'
        # B / C should be Plowhorse / Puzzle (high-qty-low-margin / low-qty-high-margin)
        assert klasses['B'] in ('Plowhorse', 'Star')
        assert klasses['C'] in ('Puzzle', 'Dog')

    def test_cogs_fraction_adjusts_margins(self, regular_user, cashier_user):
        from base.models import Category, Product
        cat, _ = Category.objects.get_or_create(name='c', slug='c')
        p = Product.objects.create(name='A', price=Decimal('100000'), category=cat)
        o = _make_order(regular_user, cashier_user, total='100')
        _add_item_at_price(o, p, 1)
        d1 = menu_engineering(timezone.localdate() - timedelta(days=5), timezone.localdate(),
                              cogs_fraction=Decimal('0.20'))
        d2 = menu_engineering(timezone.localdate() - timedelta(days=5), timezone.localdate(),
                              cogs_fraction=Decimal('0.80'))
        m1 = Decimal(d1['items'][0]['margin_per_unit'])
        m2 = Decimal(d2['items'][0]['margin_per_unit'])
        assert m1 > m2  # smaller cogs fraction → bigger margin

    def test_excludes_unpaid_and_soft_deleted_lines(self, regular_user, cashier_user):
        from base.models import Category, Product
        cat, _ = Category.objects.get_or_create(name='food', slug='food')
        p = Product.objects.create(name='Burger', price=Decimal('10000'), category=cat)
        paid = _make_order(regular_user, cashier_user, total='20000')
        paid.discount_amount = Decimal('5000')
        paid.total_amount = Decimal('15000')
        paid.save(update_fields=['discount_amount', 'total_amount'])
        _add_item_at_price(paid, p, 2)
        unpaid = _make_order(
            regular_user, cashier_user, status='OPEN', is_paid=False, total='50000')
        _add_item_at_price(unpaid, p, 5)
        removed = _make_order(regular_user, cashier_user, total='30000')
        _add_item_at_price(removed, p, 3)
        removed.items.first().delete()

        # Analytics windows use the configured business-day cutover. Between
        # midnight and that cutover, the calendar date is already tomorrow
        # while a just-paid order still belongs to the prior business day.
        from base.services.business_day import business_date
        today = business_date()
        data = menu_engineering(today, today)
        burger = next(i for i in data['items'] if i['product_id'] == p.id)
        assert burger['qty_sold'] == 2, burger
        assert Decimal(burger['revenue']) == Decimal('15000'), burger
        assert Decimal(burger['profit']) == Decimal('8000.00'), burger


def _add_item_at_price(order, product, qty):
    from base.models import OrderItem
    OrderItem.objects.create(
        order=order, product=product, quantity=qty,
        price=product.price, original_price=product.price,
    )


# ---- endpoints ----------------------------------------------------------

@pytest.fixture
def admin_session(admin_user):
    from base.models import Session
    payload = secrets.token_hex(32)
    Session.objects.create(
        user_id=admin_user, ip_address='127.0.0.1', payload=SessionRepository.hash_token(payload),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return payload


class TestShiftEndpoint:
    def test_admin_can_fetch(self, admin_session, cashier_user):
        shift = _make_shift(cashier_user)
        client = Client()
        resp = client.get(
            f'/api/admins/analytics/shifts/{shift.id}',
            HTTP_AUTHORIZATION=f'Bearer {admin_session}',
        )
        assert resp.status_code == 200
        assert resp.json()['data']['user_id'] == cashier_user.id

    def test_unknown_shift_returns_404(self, admin_session):
        client = Client()
        resp = client.get(
            '/api/admins/analytics/shifts/99999',
            HTTP_AUTHORIZATION=f'Bearer {admin_session}',
        )
        assert resp.status_code == 404


class TestMenuEngineeringEndpoint:
    def test_happy_path(self, admin_session, regular_user, cashier_user):
        from base.models import Category, Product
        cat, _ = Category.objects.get_or_create(name='c', slug='c')
        p = Product.objects.create(name='A', price=Decimal('100'), category=cat)
        o = _make_order(regular_user, cashier_user, total='100')
        _add_item_at_price(o, p, 1)
        client = Client()
        today = timezone.localdate().isoformat()
        resp = client.get(
            f'/api/admins/analytics/menu-engineering?from=2026-05-01&to={today}',
            HTTP_AUTHORIZATION=f'Bearer {admin_session}',
        )
        assert resp.status_code == 200

    def test_missing_dates_returns_422(self, admin_session):
        client = Client()
        resp = client.get(
            '/api/admins/analytics/menu-engineering',
            HTTP_AUTHORIZATION=f'Bearer {admin_session}',
        )
        assert resp.status_code == 422

    def test_inverted_range_returns_422(self, admin_session):
        client = Client()
        resp = client.get(
            '/api/admins/analytics/menu-engineering?from=2026-05-30&to=2026-05-01',
            HTTP_AUTHORIZATION=f'Bearer {admin_session}',
        )
        assert resp.status_code == 422

    def test_invalid_cogs_returns_422(self, admin_session):
        client = Client()
        resp = client.get(
            '/api/admins/analytics/menu-engineering?from=2026-05-01&to=2026-05-30&cogs_fraction=2',
            HTTP_AUTHORIZATION=f'Bearer {admin_session}',
        )
        assert resp.status_code == 422
