"""Focused regressions for settlement-ledger and shift-window analytics."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _at(day, hour, minute=0):
    return timezone.make_aware(
        datetime.combine(day, datetime.min.time()).replace(
            hour=hour, minute=minute,
        ),
        timezone.get_current_timezone(),
    )


def _cashier(branch='branch-a'):
    from base.models import User

    return User.objects.create(
        email=f'analytics-{uuid4().hex}@test.local',
        first_name='Analytics',
        last_name='Cashier',
        role='CASHIER',
        status='ACTIVE',
        password='!',
        branch_id=branch,
    )


def _paid_order(cashier, moment, *, branch='branch-a', total='100'):
    from base.models import Order

    order = Order.objects.create(
        user=cashier,
        cashier=cashier,
        branch_id=branch,
        status='COMPLETED',
        is_paid=True,
        payment_method='CASH',
        subtotal=Decimal(total),
        total_amount=Decimal(total),
        paid_at=moment,
        display_id=Order.objects.count() + 1,
    )
    Order.objects.filter(pk=order.pk).update(created_at=moment)
    order.refresh_from_db()
    return order


def test_provider_partial_refund_changes_money_not_units_lines_or_baskets():
    from base.models import Category, OrderItem, OrderRefund, Product, Shift
    from admins.services.comparison_service import compare_periods
    from admins.services.product_analytics_service import (
        products_affinity, products_overview,
    )

    business_day = date(2026, 7, 10)
    moment = _at(business_day, 12)
    cashier = _cashier()
    order = _paid_order(cashier, moment)
    category = Category.objects.create(
        name=f'Refund {uuid4().hex}', slug=f'refund-{uuid4().hex}',
    )
    product = Product.objects.create(
        name='Partial-refund meal', category=category, price=Decimal('50'),
    )
    OrderItem.objects.create(
        order=order, product=product, quantity=2,
        price=Decimal('50'), original_price=Decimal('50'),
    )
    OrderRefund.objects.create(
        order=order,
        branch_id='branch-a',
        amount=Decimal('20'),
        card_amount=Decimal('20'),
        refunded_at=moment + timedelta(minutes=5),
        source=OrderRefund.Source.COURIER_PAYMENT,
        source_id=f'provider-{uuid4().hex}',
    )

    overview = products_overview(business_day, business_day)
    assert overview['total_revenue'] == '80'
    assert overview['refund_amount'] == '20'
    assert overview['total_units'] == 2
    assert overview['refunded_units'] == 0
    assert overview['order_lines'] == 1
    assert overview['orders'] == 1
    assert overview['refunded_orders'] == 0

    comparison = compare_periods(
        business_day, business_day,
        business_day - timedelta(days=1), business_day - timedelta(days=1),
    )
    assert comparison['kpis']['net_revenue']['a'] == 80
    assert comparison['kpis']['items_sold']['a'] == 2
    assert comparison['kpis']['avg_items_per_order']['a'] == 2.0

    affinity = products_affinity(business_day, business_day)
    assert affinity['totalOrders'] == 1
    assert affinity['products'][0]['orders'] == 1

    # A terminal cancellation reverses the physical product facts once. The
    # earlier provider event remains a separate money-only ledger event.
    shift = Shift.objects.create(
        user=cashier,
        branch_id='branch-a',
        status='ENDED',
        start_time=moment - timedelta(hours=1),
        end_time=moment + timedelta(hours=1),
    )
    OrderRefund.objects.create(
        order=order,
        cashier=cashier,
        shift=shift,
        branch_id='branch-a',
        amount=Decimal('80'),
        cash_amount=Decimal('80'),
        drawer_cash_amount=Decimal('80'),
        refunded_at=moment + timedelta(minutes=10),
        source=OrderRefund.Source.ORDER_CANCEL,
        source_id=f'cancel-{uuid4().hex}',
    )
    canceled = products_overview(business_day, business_day)
    assert canceled['total_revenue'] == '0'
    assert canceled['refund_amount'] == '100'
    assert canceled['total_units'] == 0
    assert canceled['refunded_units'] == 2
    assert canceled['order_lines'] == 0
    assert canceled['orders'] == 0
    assert products_affinity(business_day, business_day)['totalOrders'] == 0


def test_affinity_does_not_subtract_a_sale_from_another_window():
    from base.models import Category, OrderItem, OrderRefund, Product
    from admins.services.product_analytics_service import products_affinity

    sale_day = date(2026, 7, 9)
    refund_day = sale_day + timedelta(days=1)
    cashier = _cashier()
    order = _paid_order(cashier, _at(sale_day, 12))
    category = Category.objects.create(
        name=f'Cross-window {uuid4().hex}',
        slug=f'cross-window-{uuid4().hex}',
    )
    product = Product.objects.create(
        name='Cross-window meal', category=category, price=Decimal('100'),
    )
    OrderItem.objects.create(
        order=order, product=product, quantity=1,
        price=Decimal('100'), original_price=Decimal('100'),
    )
    OrderRefund.objects.create(
        order=order,
        branch_id='branch-a',
        amount=Decimal('100'),
        cash_amount=Decimal('100'),
        drawer_cash_amount=Decimal('100'),
        refunded_at=_at(refund_day, 12),
        source=OrderRefund.Source.ORDER_CANCEL,
        source_id=f'cross-window-cancel-{uuid4().hex}',
    )

    sale_affinity = products_affinity(sale_day, sale_day)
    refund_affinity = products_affinity(refund_day, refund_day)

    assert sale_affinity['totalOrders'] == 1
    assert sale_affinity['products'][0]['orders'] == 1
    assert refund_affinity['totalOrders'] == 0
    assert refund_affinity['products'] == []
    assert refund_affinity['pairs'] == []


def test_shift_analytics_are_branch_scoped_and_handoff_is_half_open():
    from base.models import Shift
    from admins.services.analytics_service import shift_performance
    from admins.services.shift_analytics_service import (
        _cashier_shift_row, _hourly_daily, shift_handover_report,
    )

    day = date(2026, 7, 10)
    t0, handoff, t2 = _at(day, 10), _at(day, 11), _at(day, 12)
    cashier = _cashier()
    first = Shift.objects.create(
        user=cashier, branch_id='branch-a', status='ENDED',
        start_time=t0, end_time=handoff,
    )
    second = Shift.objects.create(
        user=cashier, branch_id='branch-a', status='ENDED',
        start_time=handoff, end_time=t2,
    )
    own = _paid_order(cashier, handoff, branch='branch-a', total='100')
    _paid_order(
        cashier, handoff + timedelta(minutes=5),
        branch='branch-b', total='900',
    )

    first_perf = shift_performance(first)
    second_perf = shift_performance(second)
    assert first_perf['orders_total'] == 0
    assert first_perf['revenue'] == '0'
    assert second_perf['orders_total'] == 1
    assert second_perf['revenue'] == '100'

    assert _cashier_shift_row(first, {})['money']['revenue'] == '0.00'
    assert _cashier_shift_row(second, {})['money']['revenue'] == '100.00'
    distribution = _hourly_daily([first, second])
    assert sum(row['orders'] for row in distribution['by_hour']) == 1
    assert sum(Decimal(row['revenue']) for row in distribution['by_hour']) == Decimal('100')
    assert shift_handover_report(first)['receipt_count'] == 0
    second_report = shift_handover_report(second)
    assert second_report['receipt_count'] == 1
    assert second_report['receipts'][0]['order_id'] == own.id


def test_non_active_shift_without_end_time_cannot_absorb_later_orders():
    """Fail closed when a legacy/corrupt closed shift lost its end timestamp.

    Only an ACTIVE shift is allowed to run to ``now``.  Treating every null
    ``end_time`` as live makes an ABANDONED/ENDED shift claim all later sales
    by the same cashier, corrupting shift revenue, handover exports and worked
    hours indefinitely.
    """
    from base.models import Shift
    from admins.services.analytics_service import shift_performance, staff_performance
    from admins.services.shift_analytics_service import (
        _cashier_shift_row, _hourly_daily, _kitchen_shift_row,
        shift_handover_report,
    )
    from admins.views.analytics_views import _shift_export_receipt_count

    day = date(2026, 7, 10)
    start = _at(day, 10)
    cashier = _cashier()
    abandoned = Shift.objects.create(
        user=cashier,
        branch_id='branch-a',
        status=Shift.Status.ABANDONED,
        start_time=start,
        end_time=None,
    )
    _paid_order(
        cashier,
        start + timedelta(hours=1),
        branch='branch-a',
        total='640000',
    )

    perf = shift_performance(abandoned)
    assert perf['duration_minutes'] == 0
    assert perf['orders_total'] == 0
    assert perf['revenue'] == '0'

    row = _cashier_shift_row(abandoned, {})
    assert row['duration_minutes'] == 0
    assert row['orders']['total'] == 0
    assert row['money']['revenue'] == '0.00'
    assert _kitchen_shift_row(abandoned, {}, 15 * 60)['orders_in_window'] == 0
    assert _hourly_daily([abandoned]) == {
        'by_hour': [], 'by_date': [], 'peak_hour': None,
    }
    assert shift_handover_report(abandoned)['receipt_count'] == 0
    assert _shift_export_receipt_count(abandoned) == 0

    staff = staff_performance(day, day)['staff']
    cashier_row = next(item for item in staff if item['user_id'] == cashier.id)
    assert cashier_row['shifts_worked'] == 1
    assert cashier_row['hours_worked'] == 0


def test_shift_distribution_query_shape_is_constant_for_many_shifts():
    from base.models import Shift
    from admins.services.shift_analytics_service import _hourly_daily

    cashier = _cashier()
    start = _at(date(2026, 6, 1), 4)
    shifts = [
        Shift.objects.create(
            user=cashier,
            branch_id='branch-a',
            status='ENDED',
            start_time=start + timedelta(hours=2 * index),
            end_time=start + timedelta(hours=2 * index + 1),
        )
        for index in range(60)
    ]

    # Warm the singleton settings lookup, then compare one shift with sixty;
    # query count must not grow with the number of interval windows.
    _hourly_daily(shifts[:1])
    with CaptureQueriesContext(connection) as single:
        _hourly_daily(shifts[:1])
    with CaptureQueriesContext(connection) as captured:
        result = _hourly_daily(shifts)

    assert result['peak_hour'] is None
    assert len(captured.captured_queries) == len(single.captured_queries)
    assert len(captured.captured_queries) <= 7


def test_shift_range_uses_business_day_cutover():
    from base.models import Shift
    from admins.services.shift_analytics_service import _shifts_in_range

    cashier = _cashier()
    shift = Shift.objects.create(
        user=cashier,
        branch_id='branch-a',
        status='ENDED',
        start_time=_at(date(2026, 7, 10), 1),
        end_time=_at(date(2026, 7, 10), 2),
    )

    assert [row.id for row in _shifts_in_range(
        date(2026, 7, 9), date(2026, 7, 9), 'CASHIER',
    )] == [shift.id]
    assert _shifts_in_range(
        date(2026, 7, 10), date(2026, 7, 10), 'CASHIER',
    ) == []


def test_forecast_targets_next_local_business_date(monkeypatch):
    from admins.services import forecast_service

    local_now = _at(date(2026, 7, 14), 4)
    monkeypatch.setattr(forecast_service.timezone, 'now', lambda: local_now)
    monkeypatch.setattr(
        forecast_service,
        'gather_history', lambda: {
            'window_days': 30,
            'products': [{
                'id': 1,
                'name': 'Meal',
                'total_qty': 30,
                'by_weekday': {'Wed': 10},
            }],
        },
    )
    data, error = forecast_service.forecast_tomorrow()

    assert error is None
    assert data['tomorrow'] == '2026-07-15'
    assert data['method'] == 'historical_weekday_blend'
    assert data['predictions'][0]['suggested_qty'] >= 1
    assert 'Wed average' in data['predictions'][0]['reason']


def test_forecast_uses_original_sale_cohort_not_refund_clock(monkeypatch):
    from base.models import Category, OrderItem, OrderRefund, Product
    from admins.services import forecast_service

    now = _at(date(2026, 7, 14), 12)
    paid_at = _at(date(2026, 7, 10), 12)       # Friday lunch
    refunded_at = _at(date(2026, 7, 13), 18)  # Monday evening
    monkeypatch.setattr(forecast_service.timezone, 'now', lambda: now)

    cashier = _cashier()
    category = Category.objects.create(
        name=f'Forecast {uuid4().hex}', slug=f'forecast-{uuid4().hex}',
    )

    kept = Product.objects.create(
        name='Provider-refunded meal', category=category, price=Decimal('100'),
    )
    kept_order = _paid_order(cashier, paid_at, total='300')
    OrderItem.objects.create(
        order=kept_order, product=kept, quantity=3,
        price=Decimal('100'), original_price=Decimal('100'),
    )
    # A provider refund changes money, not physical menu-unit demand.
    OrderRefund.objects.create(
        order=kept_order,
        branch_id='branch-a',
        amount=Decimal('20'),
        card_amount=Decimal('20'),
        refunded_at=refunded_at,
        source=OrderRefund.Source.COURIER_PAYMENT,
        source_id=f'forecast-provider-{uuid4().hex}',
    )

    ledger_cancelled = Product.objects.create(
        name='Ledger-cancelled meal', category=category, price=Decimal('100'),
    )
    ledger_order = _paid_order(cashier, paid_at, total='200')
    OrderItem.objects.create(
        order=ledger_order, product=ledger_cancelled, quantity=2,
        price=Decimal('100'), original_price=Decimal('100'),
    )
    # Keep the header non-CANCELED to prove the immutable terminal event alone
    # removes the original basket; its Monday timestamp must never become a
    # negative Monday/18 demand bucket.
    OrderRefund.objects.create(
        order=ledger_order,
        branch_id='branch-a',
        amount=Decimal('200'),
        cash_amount=Decimal('200'),
        drawer_cash_amount=Decimal('200'),
        refunded_at=refunded_at,
        source=OrderRefund.Source.ORDER_CANCEL,
        source_id=f'forecast-cancel-{uuid4().hex}',
    )

    legacy_cancelled = Product.objects.create(
        name='Legacy-cancelled meal', category=category, price=Decimal('100'),
    )
    legacy_order = _paid_order(cashier, paid_at, total='100')
    OrderItem.objects.create(
        order=legacy_order, product=legacy_cancelled, quantity=1,
        price=Decimal('100'), original_price=Decimal('100'),
    )
    # Pre-ledger paid cancellations are still terminal demand exclusions.
    legacy_order.status = 'CANCELED'
    legacy_order.save(update_fields=['status'])

    history = forecast_service.gather_history(days=30, top_n=15)
    products = {row['id']: row for row in history['products']}

    assert set(products) == {kept.id}
    assert products[kept.id]['total_qty'] == 3
    assert products[kept.id]['by_weekday'] == {'Fri': 3}
    assert products[kept.id]['by_hour'] == {'12': 3}
