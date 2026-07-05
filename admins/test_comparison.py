"""Compare-Periods analytics endpoint + service."""
import json
import secrets
from datetime import date, datetime, timedelta, timezone as _tz
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from admins.services.comparison_service import compare_periods

pytestmark = pytest.mark.django_db

# Two clearly-separated windows (Asia/Tashkent = UTC+5).
A_START, A_END = date(2026, 6, 10), date(2026, 6, 12)
B_START, B_END = date(2026, 6, 1), date(2026, 6, 3)


def _u(role='CASHIER', email=None):
    from base.models import User
    return User.objects.create(
        email=email or f'{secrets.token_hex(4)}@x.local',
        first_name='T', last_name='User', role=role, status='ACTIVE', password='!')


def _order(cashier, when_utc, total, pm='CASH', otype='HALL',
           status='COMPLETED', is_paid=True):
    """Create an order pinned to a specific UTC instant (created_at is auto_now_add,
    so we overwrite it after insert like the other analytics tests do)."""
    from base.models import Order
    o = Order.objects.create(
        user=cashier, cashier=cashier, order_type=otype, status=status,
        is_paid=is_paid, payment_method=pm if is_paid else None,
        total_amount=Decimal(total), subtotal=Decimal(total),
        display_id=Order.objects.count() + 1)
    Order.objects.filter(pk=o.pk).update(created_at=when_utc)
    o.refresh_from_db()
    return o


def _cat(name):
    from base.models import Category
    c, _ = Category.objects.get_or_create(name=name, defaults={'slug': name.lower()})
    return c


def _prod(name, price, cat):
    from base.models import Product
    return Product.objects.create(name=name, price=Decimal(price), category=cat)


def _item(order, product, qty, price):
    from base.models import OrderItem
    OrderItem.objects.create(order=order, product=product, quantity=qty,
                             price=Decimal(price), original_price=Decimal(price))


@pytest.fixture
def dataset():
    drinks, food = _cat('Drinks'), _cat('Food')
    x = _prod('X', 30000, drinks)
    y = _prod('Y', 40000, food)
    z = _prod('Z', 50000, food)
    c1 = _u(role='CASHIER', email='cash1@x.local')

    def utc(y_, m, d, h):
        return datetime(y_, m, d, h, 0, tzinfo=_tz.utc)

    # Period A: 2026-06-11 14:00 TAS and 2026-06-12 11:00 TAS
    o1 = _order(c1, utc(2026, 6, 11, 9), 100000, pm='CASH', otype='HALL')
    _item(o1, x, 2, 30000)   # 60000 Drinks
    _item(o1, y, 1, 40000)   # 40000 Food
    o2 = _order(c1, utc(2026, 6, 12, 6), 50000, pm='UZCARD', otype='DELIVERY')
    _item(o2, x, 1, 30000)   # 30000 Drinks
    # Noise that must be excluded from A: a canceled + an unpaid order.
    _order(c1, utc(2026, 6, 11, 9), 999999, status='CANCELED')
    _order(c1, utc(2026, 6, 11, 9), 888888, is_paid=False, status='OPEN')

    # Period B: 2026-06-02 14:00 TAS
    o3 = _order(c1, utc(2026, 6, 2, 9), 80000, pm='CASH', otype='HALL')
    _item(o3, x, 1, 30000)   # 30000 Drinks
    _item(o3, z, 1, 50000)   # 50000 Food
    return {'c1': c1}


def _run():
    return compare_periods(A_START, A_END, B_START, B_END,
                           granularity='day', tz_name='Asia/Tashkent')


def test_kpis(dataset):
    d = _run()
    k = d['kpis']
    assert k['net_revenue']['a'] == 150000 and k['net_revenue']['b'] == 80000
    assert k['net_revenue']['delta'] == 70000
    assert k['net_revenue']['delta_pct'] == 87.5
    assert k['net_revenue']['is_up_good'] is True
    assert k['gross_revenue']['a'] == 150000            # no discounts -> == net
    assert k['orders']['a'] == 2 and k['orders']['b'] == 1     # canceled/unpaid excluded
    assert k['items_sold']['a'] == 4 and k['items_sold']['b'] == 2
    assert k['aov']['a'] == 75000 and k['aov']['b'] == 80000
    assert k['discounts']['a'] == 0 and k['discounts']['delta_pct'] == 0.0
    assert k['avg_items_per_order']['a'] == 2.0          # 4 items / 2 orders
    assert k['avg_items_per_order']['is_up_good'] is True
    # Omitted (no data): refunds, gross_profit, margin_pct.
    assert 'refunds' not in k and 'gross_profit' not in k and 'margin_pct' not in k


def test_categories_and_products(dataset):
    d = _run()
    cats = {c['name']: c for c in d['categories']}
    assert cats['Drinks']['a_revenue'] == 90000 and cats['Drinks']['b_revenue'] == 30000
    assert cats['Drinks']['delta_pct'] == 200.0
    assert cats['Food']['a_revenue'] == 40000 and cats['Food']['delta_pct'] == -20.0
    # sorted by A revenue desc
    assert d['categories'][0]['name'] == 'Drinks'

    prods = {p['name']: p for p in d['products']}
    assert prods['X']['a_revenue'] == 90000 and prods['X']['b_revenue'] == 30000
    assert prods['Y']['a_revenue'] == 40000 and prods['Y']['delta_pct'] is None  # new in A
    assert prods['Z']['a_revenue'] == 0 and prods['Z']['delta_pct'] == -100.0


def test_gainers_losers(dataset):
    d = _run()
    gain = {g['name']: g for g in d['top_gainers']}
    assert gain['X']['delta'] == 60000 and gain['Y']['delta'] == 40000
    assert all(g['delta'] > 0 for g in d['top_gainers'])
    los = {l['name']: l for l in d['top_losers']}
    assert 'Z' in los and los['Z']['delta'] == -50000


def test_timeseries_hour_weekday_matrix(dataset):
    d = _run()
    ts = d['revenue_timeseries']
    assert ts['granularity'] == 'day'
    assert [p['index'] for p in ts['a']] == [1, 2, 3]
    assert ts['a'][0]['date'] == '2026-06-10' and ts['a'][0]['value'] == 0
    assert ts['a'][1]['value'] == 100000 and ts['a'][2]['value'] == 50000

    # by_hour marginals (Tashkent): 14:00 and 11:00 each have one order in A.
    hours_a = {row['hour']: row['value'] for row in d['by_hour']['a']}
    assert hours_a[14] == 1 and hours_a[11] == 1
    assert sum(hours_a.values()) == 2

    # 7x24 matrix, sums to the order count of the period.
    hw = d['hour_weekday']['a']
    assert len(hw) == 7 and all(len(r) == 24 for r in hw)
    assert sum(sum(r) for r in hw) == 2
    # weekday marginal equals the matrix row sums
    wd_a = {row['weekday']: row['value'] for row in d['by_weekday']['a']}
    for w in range(7):
        assert wd_a[w] == sum(hw[w])


def test_payment_and_order_types(dataset):
    d = _run()
    pay_a = {p['method']: p for p in d['payment_methods']['a']}
    assert pay_a['CASH']['value'] == 100000 and pay_a['UZCARD']['value'] == 50000
    assert round(pay_a['CASH']['share'] + pay_a['UZCARD']['share'], 4) == 1.0
    ot_a = {o['type']: o['value'] for o in d['order_types']['a']}
    assert ot_a['HALL'] == 100000 and ot_a['DELIVERY'] == 50000


def test_by_cashier_and_periods(dataset):
    d = _run()
    assert d['period_a']['days'] == 3 and d['period_b']['days'] == 3
    cash = {c['name']: c for c in d['by_cashier']}
    name = next(iter(cash))
    assert cash[name]['a'] == 150000 and cash[name]['b'] == 80000


# ---- view: auth + validation ------------------------------------------------

def _admin_token():
    from base.models import Session
    from base.repositories.session import SessionRepository
    u = _u(role='ADMIN', email='admin@x.local')
    tok = secrets.token_hex(32)
    Session.objects.create(user_id=u, ip_address='127.0.0.1',
                           payload=SessionRepository.hash_token(tok),
                           expires_at=timezone.now() + timedelta(hours=1))
    return tok


def test_view_requires_auth():
    c = Client()
    r = c.get('/api/admins/analytics/comparison/',
              {'a_start': '2026-06-10', 'a_end': '2026-06-12',
               'b_start': '2026-06-01', 'b_end': '2026-06-03'})
    assert r.status_code in (401, 403)


def test_view_validates_dates():
    tok = _admin_token()
    c = Client()
    r = c.get('/api/admins/analytics/comparison/', {'a_start': '2026-06-10'},
              HTTP_AUTHORIZATION=f'Bearer {tok}')
    assert r.status_code == 422


def test_view_happy_path(dataset):
    tok = _admin_token()
    c = Client()
    r = c.get('/api/admins/analytics/comparison/',
              {'a_start': '2026-06-10', 'a_end': '2026-06-12',
               'b_start': '2026-06-01', 'b_end': '2026-06-03',
               'granularity': 'day', 'tz': 'Asia/Tashkent'},
              HTTP_AUTHORIZATION=f'Bearer {tok}')
    assert r.status_code == 200, r.content
    body = r.json()
    assert body['success'] is True
    assert body['data']['kpis']['net_revenue']['a'] == 150000
