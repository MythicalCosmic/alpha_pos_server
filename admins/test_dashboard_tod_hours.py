"""Dashboard/orders: time-of-day (tod) filter, hourly single-day series,
configurable working hours, and the product_ids order filter."""
import secrets
from datetime import date, datetime, timedelta, timezone as _tz
from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db

# A fixed operating date. Canonical window [08 07:00 +05, 09 03:00 +05).
D = date(2026, 7, 8)


def _u():
    from base.models import User
    return User.objects.create(email=f'{secrets.token_hex(4)}@x.local', first_name='U',
                               last_name='X', role='CASHIER', status='ACTIVE', password='!')


def _order(total, when_utc, cashier=None, is_paid=True, status='COMPLETED'):
    from base.models import Order
    u = cashier or _u()
    o = Order.objects.create(
        user=u, cashier=u, order_type='HALL', status=status, is_paid=is_paid,
        payment_method='CASH' if is_paid else None,
        total_amount=Decimal(total), subtotal=Decimal(total),
        display_id=Order.objects.count() + 1)
    from base.models import Order as O
    O.objects.filter(pk=o.pk).update(created_at=when_utc, paid_at=when_utc if is_paid else None)
    o.refresh_from_db()
    return o


def _item(order, product, qty, price):
    from base.models import OrderItem
    OrderItem.objects.create(order=order, product=product, quantity=qty,
                             price=Decimal(price), original_price=Decimal(price))


def _utc(y, mo, d, h):
    return datetime(y, mo, d, h, 0, tzinfo=_tz.utc)


def _seed_tod_orders():
    # local = UTC + 5h. The 04:00 row is in the excluded 03:00-07:00 gap.
    _order(100000, _utc(2026, 7, 8, 5))    # 10:00 local  (in 09-23)
    _order(50000, _utc(2026, 7, 8, 16))    # 21:00 local  (in 09-23)
    _order(30000, _utc(2026, 7, 7, 23))    # 04:00 local  (out: < 09:00)
    _order(20000, _utc(2026, 7, 8, 3))     # 08:00 local  (out: < 09:00)


def test_get_range_tod_filter():
    from admins.services.dashboard_service import get_range
    _seed_tod_orders()
    full = get_range('2026-07-08', '2026-07-08')
    assert full['revenue'] == '170000', full          # 07:00 -> next-day 03:00
    win = get_range('2026-07-08', '2026-07-08', tod_from='09:00', tod_to='23:00')
    assert win['revenue'] == '150000', win            # only 10:00 + 21:00
    assert win['orders'] == 2


def test_sales_dashboard_hourly_granularity():
    from admins.services.sales_dashboard_service import sales_dashboard
    _seed_tod_orders()
    data = sales_dashboard(date_from='2026-07-08', date_to='2026-07-08', granularity='hour')
    assert data['range']['granularity'] == 'hour'
    assert len(data['dayLabels']) == 20
    assert data['dayLabels'][0] == '07:00' and data['dayLabels'][-1] == '02:00'
    assert len(data['revenue30']) == 20
    # 10:00 local order -> hour 10 -> index 3 in [7,8,9,10,...]
    assert data['dayLabels'][3] == '10:00'
    assert data['revenue30'][3] == '100000'
    # 21:00 local -> index 14
    assert data['dayLabels'][14] == '21:00' and data['revenue30'][14] == '50000'


def test_product_ids_filter_orders_and_stats():
    from base.models import Category, Product
    from admins.services.order_service import AdminOrderService
    cat = Category.objects.create(name='C', slug='c')
    p1 = Product.objects.create(name='P1', price=Decimal('10000'), category=cat)
    p2 = Product.objects.create(name='P2', price=Decimal('10000'), category=cat)
    c = _u()
    o1 = _order(20000, _utc(2026, 7, 8, 6), cashier=c); _item(o1, p1, 2, 10000)
    o2 = _order(30000, _utc(2026, 7, 8, 6), cashier=c); _item(o2, p2, 3, 10000)

    res = AdminOrderService.get_all_orders(date_from='2026-07-08', date_to='2026-07-08',
                                           product_ids=str(p1.id))
    ids = {o['id'] for o in res[0]['data']['orders']}
    assert ids == {o1.id}, ids

    stats = AdminOrderService.get_order_stats('2026-07-08', '2026-07-08', product_ids=str(p1.id))
    d = stats[0]['data']
    assert d['total_orders'] == 1 and d['total_revenue'] == '20000', d


def test_working_hours_settings_get_put():
    from admins.services.app_settings_service import AppSettingsService
    g = AppSettingsService.get_all()[0]['data']['settings']
    assert g['business_open'] == '07:00' and g['business_close'] == '03:00'
    upd = AppSettingsService.update(business_open='08:30', business_close='22:00')
    s = upd[0]['data']['settings']
    assert s['business_open'] == '08:30' and s['business_close'] == '22:00'
    bad = AppSettingsService.update(business_open='nope')
    assert bad[1] == 422


def test_daily_stats_bucket_is_business_day():
    from base.repositories.order import OrderRepository
    # 2026-07-08 01:00 local (= 2026-07-07 20:00 UTC) is before the 03:00 cutover,
    # so it belongs to business day 2026-07-07.
    _order(10000, _utc(2026, 7, 7, 20))    # 01:00 local 07-08 -> business day 07-07
    _order(10000, _utc(2026, 7, 8, 6))     # 11:00 local 07-08 -> business day 07-08
    rows = OrderRepository.get_daily_stats(
        _utc(2026, 7, 6, 0), _utc(2026, 7, 9, 0))
    by_date = {r['date']: r['orders'] for r in rows}
    assert by_date.get(date(2026, 7, 7)) == 1, by_date   # the 01:00 order
    assert by_date.get(date(2026, 7, 8)) == 1, by_date
