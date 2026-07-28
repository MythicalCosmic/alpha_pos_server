"""Shift-settlement date-attribution tests."""

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
# Settlement just after the canonical 07:00 opening boundary. The old 03:01
# fixture now falls in the intentionally excluded 03:00-07:00 quiet gap.
PAID_AT = timezone.make_aware(datetime(2026, 7, 10, 7, 1), TASHKENT)


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
    from django.conf import settings
    from base.models import Shift

    creation_shift = Shift.objects.create(
        user=cashier,
        status='ENDED',
        start_time=CREATED_AT - timedelta(hours=1),
        end_time=CREATED_AT + timedelta(minutes=30),
        branch_id=settings.BRANCH_ID,
    )
    settlement_shift = Shift.objects.create(
        user=cashier,
        status='ENDED',
        start_time=PAID_AT - timedelta(minutes=1),
        end_time=PAID_AT + timedelta(hours=1),
        branch_id=settings.BRANCH_ID,
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
        {
            'date': '2026-07-10',
            'units': 2,
            'revenue': '100',
            'gross_units': 2,
            'refunded_units': 0,
            'gross_revenue': '100',
            'refund_amount': '0',
        },
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
    assert by_hour[7] == {'hour': 7, 'orders': 0, 'revenue': '100.00'}

    created_handover = shift_handover_report(creation_shift)
    assert created_handover['receipt_count'] == 1
    assert created_handover['settled_receipt_count'] == 0
    assert created_handover['receipts'][0]['line_items'] == 1
    assert created_handover['receipts'][0]['units'] == 2
    assert created_handover['products'] == []
    assert created_handover['receipt_scopes']['receipts'] == {
        'clock': 'created_at',
        'label': 'Orders taken during this shift',
        'drives_money_totals': False,
    }

    settled_handover = shift_handover_report(settlement_shift)
    assert settled_handover['receipt_count'] == 0
    assert settled_handover['settled_receipt_count'] == 1
    settled_receipt = settled_handover['settled_receipts'][0]
    assert settled_receipt['order_id'] == order.id
    assert settled_receipt['created_in_this_shift'] is False
    assert settled_receipt['cash_amount'] == '100.00'
    assert settled_receipt['card_amount'] == '0.00'
    assert settled_receipt['payme_amount'] == '0.00'
    assert settled_receipt['unknown_amount'] == '0.00'
    assert settled_receipt['drawer_cash_amount'] == '100.00'
    assert settled_receipt['has_concrete_payment_evidence'] is False
    assert settled_receipt['tender_attribution_complete'] is True
    assert settled_handover['receipt_scopes']['settled_receipts'] == {
        'clock': 'paid_at',
        'label': 'Orders paid during this shift',
        'drives_money_totals': True,
    }
    assert settled_handover['products'][0]['product_id'] == product.id
    assert settled_handover['products'][0]['units_sold'] == 2


def test_shift_handover_settled_receipt_uses_canonical_mixed_tender_math():
    from base.models import OrderPayment
    from admins.services.shift_analytics_service import shift_handover_report

    cashier, order, _item, _product = _sale()
    _creation_shift, settlement_shift = _shifts(cashier)
    order.payment_method = 'MIXED'
    order.save(update_fields=['payment_method'])
    # Customer tenders 80 cash + 40 HUMO against a 100 bill. The 20 change is
    # not revenue and must not inflate either the shift or receipt drawer cash.
    OrderPayment.objects.create(
        order=order,
        method='CASH',
        amount='80.00',
        branch_id=order.branch_id,
    )
    OrderPayment.objects.create(
        order=order,
        method='HUMO',
        amount='40.00',
        branch_id=order.branch_id,
    )

    report = shift_handover_report(settlement_shift)
    receipt = report['settled_receipts'][0]

    assert receipt['cash_amount'] == '60.00'
    assert receipt['card_amount'] == '40.00'
    assert receipt['humo_amount'] == '40.00'
    assert receipt['drawer_cash_amount'] == '60.00'
    assert receipt['has_concrete_payment_evidence'] is True
    assert receipt['tender_attribution_complete'] is True
    assert (
        Decimal(receipt['cash_amount'])
        + Decimal(receipt['card_amount'])
        + Decimal(receipt['payme_amount'])
        + Decimal(receipt['unknown_amount'])
    ) == Decimal(receipt['total_amount'])


def test_active_shift_handover_uses_one_authoritative_cutoff(product):
    from django.conf import settings
    from base.models import Order, OrderItem, OrderRefund, Shift, User
    from cashbox.models import CashboxExpense
    from admins.services.shift_analytics_service import (
        cashier_shift_analytics,
        kitchen_shift_analytics,
        shift_handover_report,
    )
    from admins.views.analytics_views import _shift_export_receipt_count

    cutoff = timezone.make_aware(
        datetime(2026, 7, 24, 12, 0),
        TASHKENT,
    )
    shift_start = cutoff - timedelta(hours=1)
    cashier = User.objects.create(
        email=f'active-cutoff-{uuid4().hex}@test.local',
        first_name='Active',
        last_name='Cutoff',
        role='CASHIER',
        status='ACTIVE',
        password='!',
        branch_id=settings.BRANCH_ID,
    )
    shift = Shift.objects.create(
        user=cashier,
        status=Shift.Status.ACTIVE,
        start_time=shift_start,
        branch_id=settings.BRANCH_ID,
    )

    def paid_order(display_id, moment, amount):
        order = Order.objects.create(
            user=cashier,
            cashier=cashier,
            branch_id=settings.BRANCH_ID,
            status=Order.Status.COMPLETED,
            is_paid=True,
            payment_method='CASH',
            paid_at=moment,
            subtotal=Decimal(amount),
            total_amount=Decimal(amount),
            display_id=display_id,
        )
        Order.objects.filter(pk=order.pk).update(created_at=moment)
        order.refresh_from_db()
        return order

    included = paid_order(9801, cutoff - timedelta(minutes=1), '100')
    paid_order(9802, cutoff + timedelta(minutes=1), '900')
    OrderItem.objects.create(
        order=included,
        product=product,
        quantity=1,
        price=Decimal('100'),
        original_price=Decimal('100'),
    )
    OrderRefund.objects.create(
        order=included,
        shift=shift,
        cashier=cashier,
        amount=Decimal('25'),
        cash_amount=Decimal('25'),
        drawer_cash_amount=Decimal('25'),
        refunded_at=cutoff + timedelta(minutes=1),
        source=OrderRefund.Source.ORDER_CANCEL,
        source_id=f'future-cutoff-{uuid4().hex}',
        branch_id=settings.BRANCH_ID,
    )
    included_expense = CashboxExpense.objects.create(
        shift=shift,
        amount=Decimal('10'),
        branch_id=settings.BRANCH_ID,
    )
    CashboxExpense.objects.filter(pk=included_expense.pk).update(
        created_at=cutoff - timedelta(minutes=1),
    )
    excluded_expense = CashboxExpense.objects.create(
        shift=shift,
        amount=Decimal('90'),
        branch_id=settings.BRANCH_ID,
    )
    CashboxExpense.objects.filter(pk=excluded_expense.pk).update(
        created_at=cutoff + timedelta(minutes=1),
    )

    report = shift_handover_report(shift, now=cutoff)

    assert report['shift']['money']['revenue'] == '100.00'
    assert report['shift']['refunds']['count'] == 0
    assert report['shift']['items']['units_sold'] == 1
    assert report['receipt_count'] == 1
    assert report['settled_receipt_count'] == 1
    assert report['refunds'] == []
    assert report['cash_expenses'] == [{
        'category': 'Uncategorized',
        'total': '10.00',
        'count': 1,
    }]
    assert report['receipts'][0]['order_id'] == included.id
    assert report['settled_receipts'][0]['order_id'] == included.id
    assert sum(
        Decimal(row['revenue'])
        for row in report['distribution']['by_hour']
    ) == Decimal('100.00')
    assert sum(
        row['orders'] for row in report['distribution']['by_hour']
    ) == 1
    # One operational row plus one settlement row for the included order.
    assert _shift_export_receipt_count(shift, now=cutoff) == 2

    business_date = timezone.localtime(cutoff).date()
    cashier_analytics = cashier_shift_analytics(
        business_date, business_date, now=cutoff,
    )
    assert cashier_analytics['summary']['money']['revenue'] == '100.00'
    assert cashier_analytics['shifts'][0]['items']['units_sold'] == 1
    assert sum(
        Decimal(row['revenue'])
        for row in cashier_analytics['distribution']['by_hour']
    ) == Decimal('100.00')

    kitchen_analytics = kitchen_shift_analytics(
        business_date, business_date, role='CASHIER', now=cutoff,
    )
    assert kitchen_analytics['summary']['orders_in_window'] == 1
    assert kitchen_analytics['shifts'][0]['orders_in_window'] == 1
    assert sum(
        Decimal(row['revenue'])
        for row in kitchen_analytics['distribution']['by_hour']
    ) == Decimal('100.00')


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
