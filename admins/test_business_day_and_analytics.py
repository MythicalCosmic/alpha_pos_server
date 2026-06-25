"""Items 8-10: business-day window helper, business-day-windowed dashboard/stats,
products analytics (overview/categories/pareto/trends), and staff performance."""
import secrets
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from base.repositories.session import SessionRepository

pytestmark = pytest.mark.django_db


# ── helpers ───────────────────────────────────────────────────────────────

def _aware(y, m, d, hh=12, mm=0):
    return timezone.make_aware(datetime(y, m, d, hh, mm))


def _u(role='CASHIER'):
    from base.models import User
    return User.objects.create(
        email=f'b{secrets.token_hex(4)}@x.local', first_name='a', last_name='b',
        role=role, status='ACTIVE', password='!')


def _order_at(created, cashier=None, total='100', method='CASH',
              status='COMPLETED', paid=True):
    from base.models import Order
    cashier = cashier or _u()
    o = Order.objects.create(
        user=cashier, cashier=cashier, status=status, is_paid=paid,
        display_id=Order.objects.count() + 1,
        subtotal=Decimal(total), total_amount=Decimal(total),
        payment_method=(method if paid else None),
        paid_at=(created if paid else None))
    # created_at is auto_now_add — override via update() to place the order in time.
    Order.objects.filter(pk=o.pk).update(created_at=created)
    o.refresh_from_db()
    return o


def _cat(name='c'):
    from base.models import Category
    cat, _ = Category.objects.get_or_create(name=name, slug=name.lower())
    return cat


def _product(name, price, cat=None):
    from base.models import Product
    return Product.objects.create(name=name, price=Decimal(price), category=cat or _cat())


def _item(order, product, qty):
    from base.models import OrderItem
    OrderItem.objects.create(
        order=order, product=product, quantity=qty,
        price=product.price, original_price=product.price)


@pytest.fixture
def admin_session(admin_user):
    from base.models import Session
    payload = secrets.token_hex(32)
    Session.objects.create(
        user_id=admin_user, ip_address='127.0.0.1',
        payload=SessionRepository.hash_token(payload),
        expires_at=timezone.now() + timedelta(hours=1))
    return payload


# ── business-day helper (item 8) ───────────────────────────────────────────

class TestBusinessDayHelper:
    def test_before_cutover_is_previous_day(self):
        from base.services.business_day import business_date
        assert business_date(_aware(2026, 3, 11, 1, 30), start=time(3, 0)) == date(2026, 3, 10)
        assert business_date(_aware(2026, 3, 11, 4, 0), start=time(3, 0)) == date(2026, 3, 11)

    def test_range_window_spans_cutovers(self):
        from base.services.business_day import range_window
        lo, hi = range_window(date(2026, 3, 10), date(2026, 3, 10), start=time(3, 0))
        assert lo == _aware(2026, 3, 10, 3, 0)
        assert hi == _aware(2026, 3, 11, 3, 0)


# ── dashboard windowed on the business day (item 8) ────────────────────────

class TestDashboardBusinessWindow:
    def test_after_midnight_order_counts_to_previous_business_day(self):
        from admins.services import dashboard_service
        # 01:30 on the 11th belongs to business day the 10th (cutover 03:00).
        _order_at(_aware(2026, 3, 11, 1, 30), total='500')
        d10 = dashboard_service.get_range('2026-03-10', '2026-03-10')
        d11 = dashboard_service.get_range('2026-03-11', '2026-03-11')
        assert d10['revenue'] == '500'
        assert d11['revenue'] == '0'


# ── order-stats date windows (item 8) ──────────────────────────────────────

class TestOrderStatsParseDate:
    def test_parse_date_anchors_to_business_start(self):
        from admins.services.order_service import _parse_date, _parse_date_to
        d = _parse_date('2026-03-10')
        assert d.date() == date(2026, 3, 10)
        assert (d.hour, d.minute) == (3, 0)
        dt = _parse_date_to('2026-03-10')
        # inclusive end = last microsecond before the next 03:00 cutover.
        assert dt.date() == date(2026, 3, 11)
        assert (dt.hour, dt.minute) == (2, 59)


# ── products analytics (item 9) ────────────────────────────────────────────

class TestProductAnalytics:
    def test_overview_and_pareto(self):
        from admins.services.product_analytics_service import products_overview, products_pareto
        cat = _cat()
        o = _order_at(_aware(2026, 3, 10, 12, 0))
        _item(o, _product('A', '100', cat), 5)   # revenue 500
        _item(o, _product('B', '10', cat), 3)    # revenue 30
        ov = products_overview(date(2026, 3, 10), date(2026, 3, 10))
        assert ov['total_units'] == 8
        assert ov['total_revenue'] == '530'
        assert ov['distinct_products_sold'] == 2
        assert ov['top_products'][0]['product_name'] == 'A'

        par = products_pareto(date(2026, 3, 10), date(2026, 3, 10))
        assert par['products'][0]['class'] == 'A'   # A makes ~94% of revenue
        assert par['summary']['total_products'] == 2

    def test_categories_split_revenue(self):
        from admins.services.product_analytics_service import products_categories
        o = _order_at(_aware(2026, 3, 10, 12, 0))
        _item(o, _product('Burger', '100', _cat('food')), 2)   # 200
        _item(o, _product('Cola', '50', _cat('drink')), 1)     # 50
        data = products_categories(date(2026, 3, 10), date(2026, 3, 10))
        cats = {c['category']: c for c in data['categories']}
        assert cats['food']['revenue'] == '200'
        assert cats['drink']['units'] == 1

    def test_trends_bucket_by_business_day(self):
        from admins.services.product_analytics_service import products_trends
        # 01:30 on the 11th -> business day the 10th.
        o = _order_at(_aware(2026, 3, 11, 1, 30))
        _item(o, _product('A', '100', _cat()), 2)
        data = products_trends(date(2026, 3, 10), date(2026, 3, 11))
        days = {d['date']: d for d in data['daily']}
        assert days.get('2026-03-10', {}).get('units') == 2


# ── staff performance (item 10) ────────────────────────────────────────────

class TestStaffPerformance:
    def test_per_staff_revenue_and_summary(self):
        from admins.services.analytics_service import staff_performance
        c1, c2 = _u(), _u()
        _order_at(_aware(2026, 3, 10, 12, 0), cashier=c1, total='300')
        _order_at(_aware(2026, 3, 10, 13, 0), cashier=c1, total='200')
        _order_at(_aware(2026, 3, 10, 12, 0), cashier=c2, total='100')
        data = staff_performance(date(2026, 3, 10), date(2026, 3, 10))
        by = {s['user_id']: s for s in data['staff']}
        assert by[c1.id]['revenue'] == '500'
        assert by[c1.id]['orders_total'] == 2
        assert by[c2.id]['revenue'] == '100'
        assert data['summary']['staff_count'] == 2
        assert data['summary']['top_performer'] == by[c1.id]['name']


# ── app-settings + /auth/me expose business_day_start (item 8) ──────────────

class TestBusinessDayStartExposed:
    def test_app_settings_get_and_update(self):
        from admins.services.app_settings_service import AppSettingsService
        from base.models import AppSettings
        from django.core.cache import cache
        res, status = AppSettingsService.get_all()
        assert status == 200
        assert res['data']['settings']['business_day_start'] == '03:00'

        res, status = AppSettingsService.update(business_day_start='05:30')
        assert status == 200
        cache.clear()
        assert AppSettings.load().business_day_start.strftime('%H:%M') == '05:30'

    def test_app_settings_update_rejects_bad_time(self):
        from admins.services.app_settings_service import AppSettingsService
        _, status = AppSettingsService.update(business_day_start='not-a-time')
        assert status == 422

    def test_auth_me_includes_business_day_start(self, admin_session):
        resp = Client().get('/api/admins/auth-me', HTTP_AUTHORIZATION=f'Bearer {admin_session}')
        assert resp.status_code == 200
        assert resp.json()['data']['business_day_start'] == '03:00'


# ── new endpoints wired + manager-gated (items 9 & 10) ─────────────────────

class TestNewEndpoints:
    def test_products_overview_endpoint(self, admin_session):
        resp = Client().get(
            '/api/admins/analytics/products/overview?from=2026-03-10&to=2026-03-10',
            HTTP_AUTHORIZATION=f'Bearer {admin_session}')
        assert resp.status_code == 200
        assert 'total_revenue' in resp.json()['data']

    def test_products_pareto_endpoint(self, admin_session):
        resp = Client().get(
            '/api/admins/analytics/products/pareto?from=2026-03-10&to=2026-03-10',
            HTTP_AUTHORIZATION=f'Bearer {admin_session}')
        assert resp.status_code == 200

    def test_staff_performance_range_token(self, admin_session):
        resp = Client().get(
            '/api/admins/staff/performance?range=7d',
            HTTP_AUTHORIZATION=f'Bearer {admin_session}')
        assert resp.status_code == 200
        assert 'staff' in resp.json()['data']

    def test_products_overview_requires_auth(self):
        resp = Client().get('/api/admins/analytics/products/overview?from=2026-03-10&to=2026-03-10')
        assert resp.status_code in (401, 403)
