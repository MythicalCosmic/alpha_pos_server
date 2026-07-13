from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone


pytestmark = pytest.mark.django_db
TASHKENT = ZoneInfo('Asia/Tashkent')
CREATED_AT = timezone.make_aware(datetime(2026, 7, 9, 23, 59), TASHKENT)
PAID_AT = timezone.make_aware(datetime(2026, 7, 10, 3, 1), TASHKENT)


def _sale(*, discount_percent='0'):
    from base.models import Category, Order, OrderItem, Product, User

    suffix = uuid4().hex
    cashier = User.objects.create(
        email=f'settlement-{suffix}@test.local',
        first_name='Settle',
        last_name='Cashier',
        role='CASHIER',
        status='ACTIVE',
        password='!',
    )
    category = Category.objects.create(
        name=f'Settlement {suffix}',
        slug=f'settlement-{suffix}',
    )
    product = Product.objects.create(
        name=f'Cross-cutoff item {suffix}',
        category=category,
        price=Decimal('50'),
    )
    order = Order.objects.create(
        user=cashier,
        cashier=cashier,
        order_type='HALL',
        status='COMPLETED',
        is_paid=True,
        payment_method='CASH',
        subtotal=Decimal('100'),
        total_amount=Decimal('100'),
        discount_percent=Decimal(discount_percent),
        paid_at=PAID_AT,
        display_id=Order.objects.count() + 1,
    )
    Order.objects.filter(pk=order.pk).update(
        created_at=CREATED_AT,
        paid_at=PAID_AT,
    )
    order.refresh_from_db()
    item = OrderItem.objects.create(
        order=order,
        product=product,
        quantity=2,
        price=Decimal('50'),
        original_price=Decimal('50'),
    )
    return cashier, order, item, product


def _shifts(cashier):
    from base.models import Shift

    creation_shift = Shift.objects.create(
        user=cashier,
        status='ENDED',
        start_time=CREATED_AT - timedelta(hours=1),
        end_time=CREATED_AT + timedelta(minutes=30),
    )
    settlement_shift = Shift.objects.create(
        user=cashier,
        status='ENDED',
        start_time=PAID_AT - timedelta(minutes=1),
        end_time=PAID_AT + timedelta(hours=1),
    )
    return creation_shift, settlement_shift


def test_staff_shift_and_menu_split_creation_from_settlement():
    from admins.services.analytics_service import (
        menu_engineering,
        shift_performance,
        staff_performance,
    )

    cashier, _order, _item, _product = _sale()
    creation_shift, settlement_shift = _shifts(cashier)

    created_row = shift_performance(creation_shift)
    assert created_row['orders_total'] == 1
    assert created_row['orders_completed'] == 1
    assert created_row['orders_paid'] == 0
    assert created_row['revenue'] == '0'

    settled_row = shift_performance(settlement_shift)
    assert settled_row['orders_total'] == 0
    assert settled_row['orders_completed'] == 0
    assert settled_row['orders_paid'] == 1
    assert settled_row['revenue'] == '100'

    created_staff = staff_performance(date(2026, 7, 9), date(2026, 7, 9))['staff'][0]
    assert created_staff['orders_total'] == 1
    assert created_staff['orders_completed'] == 1
    assert created_staff['orders_paid'] == 0
    assert created_staff['revenue'] == '0'
    assert created_staff['units_sold'] == 0

    settled_staff = staff_performance(date(2026, 7, 10), date(2026, 7, 10))['staff'][0]
    assert settled_staff['orders_total'] == 0
    assert settled_staff['orders_paid'] == 1
    assert settled_staff['revenue'] == '100'
    assert settled_staff['units_sold'] == 2

    assert menu_engineering(date(2026, 7, 9), date(2026, 7, 9))['items'] == []
    settled_menu = menu_engineering(date(2026, 7, 10), date(2026, 7, 10))
    assert settled_menu['items'][0]['qty_sold'] == 2
    assert Decimal(settled_menu['items'][0]['revenue']) == Decimal('100')


def test_product_sales_and_trends_follow_paid_business_date():
    from admins.services.product_analytics_service import (
        products_affinity,
        products_overview,
        products_trends,
    )

    _cashier, _order, _item, product = _sale()

    created = products_overview(date(2026, 7, 9), date(2026, 7, 9))
    assert created['total_units'] == 0
    assert created['total_revenue'] == '0'

    settled = products_overview(date(2026, 7, 10), date(2026, 7, 10))
    assert settled['total_units'] == 2
    assert settled['total_revenue'] == '100'
    assert settled['top_products'][0]['product_id'] == product.id

    trend = products_trends(date(2026, 7, 10), date(2026, 7, 10))
    assert trend['daily'] == [
        {'date': '2026-07-10', 'units': 2, 'revenue': '100'},
    ]
    assert products_affinity(
        date(2026, 7, 9), date(2026, 7, 9),
    )['totalOrders'] == 0
    assert products_affinity(
        date(2026, 7, 10), date(2026, 7, 10),
    )['totalOrders'] == 1


def test_comparison_uses_created_at_for_volume_and_paid_at_for_sales():
    from admins.services.comparison_service import compare_periods

    _sale()
    data = compare_periods(
        date(2026, 7, 9),
        date(2026, 7, 9),
        date(2026, 7, 10),
        date(2026, 7, 10),
        tz_name='Asia/Tashkent',
    )

    assert data['kpis']['orders']['a'] == 1
    assert data['kpis']['orders']['b'] == 0
    assert data['kpis']['net_revenue']['a'] == 0
    assert data['kpis']['net_revenue']['b'] == 100
    assert data['kpis']['items_sold']['a'] == 0
    assert data['kpis']['items_sold']['b'] == 2
    assert data['kpis']['aov']['b'] == 100

    heat_a = {row['hour']: row['value'] for row in data['by_hour']['a']}
    heat_b = {row['hour']: row['value'] for row in data['by_hour']['b']}
    assert heat_a[23] == 1
    assert sum(heat_b.values()) == 0
    assert data['revenue_timeseries']['b'] == [
        {'index': 1, 'date': '2026-07-10', 'value': 100},
    ]
    payment_b = {
        row['method']: row['value']
        for row in data['payment_methods']['b']
    }
    assert payment_b['cash'] == 100


def test_shift_distribution_handover_and_discount_use_settlement_clock():
    from base.models import OrderItem, Product
    from admins.services.shift_analytics_service import (
        _cashier_shift_row,
        _hourly_daily,
        shift_handover_report,
    )

    cashier, order, _item, product = _sale(discount_percent='10')
    creation_shift, settlement_shift = _shifts(cashier)

    # A removed line must not inflate receipt line/quantity counts.
    removed_product = Product.objects.create(
        name=f'Removed {uuid4().hex}',
        category=product.category,
        price=Decimal('10'),
    )
    removed = OrderItem.objects.create(
        order=order,
        product=removed_product,
        quantity=5,
        price=Decimal('10'),
        original_price=Decimal('10'),
    )
    removed.delete()

    created_row = _cashier_shift_row(creation_shift, {})
    assert created_row['orders']['total'] == 1
    assert created_row['orders']['paid'] == 0
    assert created_row['items']['units_sold'] == 0
    assert created_row['money']['revenue'] == '0.00'

    settled_row = _cashier_shift_row(settlement_shift, {})
    assert settled_row['orders']['total'] == 0
    assert settled_row['orders']['paid'] == 1
    assert settled_row['items']['units_sold'] == 2
    assert settled_row['money']['revenue'] == '100.00'
    assert settled_row['discounts']['discount_rate_pct'] == 100.0

    distribution = _hourly_daily([creation_shift, settlement_shift])
    by_hour = {row['hour']: row for row in distribution['by_hour']}
    assert by_hour[23] == {'hour': 23, 'orders': 1, 'revenue': '0.00'}
    assert by_hour[3] == {'hour': 3, 'orders': 0, 'revenue': '100.00'}

    created_handover = shift_handover_report(creation_shift)
    assert created_handover['receipt_count'] == 1
    assert created_handover['receipts'][0]['line_items'] == 1
    assert created_handover['receipts'][0]['units'] == 2
    assert created_handover['products'] == []

    settled_handover = shift_handover_report(settlement_shift)
    assert settled_handover['receipt_count'] == 0
    assert settled_handover['products'][0]['product_id'] == product.id
    assert settled_handover['products'][0]['units_sold'] == 2


def test_realized_export_uses_local_paid_date_and_operational_mode_uses_created():
    from admins.services.export_service import build_export

    _cashier, _order, _item, _product = _sale()

    _xml, creation_count = build_export(
        date(2026, 7, 9), date(2026, 7, 9),
    )
    assert creation_count == 0

    xml, settlement_count = build_export(
        date(2026, 7, 10), date(2026, 7, 10),
    )
    assert settlement_count == 1
    document = ET.fromstring(xml).find('Документ')
    # PAID_AT is still July 9 in UTC; CommerceML must use local Tashkent date.
    assert document.find('Дата').text == '2026-07-10'

    mixed_xml, mixed_count = build_export(
        date(2026, 7, 9),
        date(2026, 7, 9),
        include_unpaid=True,
    )
    assert mixed_count == 1
    mixed_document = ET.fromstring(mixed_xml).find('Документ')
    assert mixed_document.find('Дата').text == '2026-07-09'
