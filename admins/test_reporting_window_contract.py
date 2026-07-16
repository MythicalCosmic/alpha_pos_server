"""Canonical 07:00->03:00 reporting-window and expense-list contracts."""
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from base.repositories.session import SessionRepository


pytestmark = pytest.mark.django_db


def _at(day, hour, minute=0):
    return timezone.make_aware(
        datetime.combine(day, datetime.min.time()).replace(
            hour=hour, minute=minute,
        ),
        timezone.get_current_timezone(),
    )


def _order(user, cashier, when, amount='100'):
    from base.models import Order
    order = Order.objects.create(
        user=user,
        cashier=cashier,
        status=Order.Status.COMPLETED,
        is_paid=True,
        payment_method=Order.PaymentMethod.CASH,
        paid_at=when,
        subtotal=Decimal(amount),
        total_amount=Decimal(amount),
        display_id=Order.objects.count() + 1,
    )
    Order.objects.filter(pk=order.pk).update(created_at=when)
    return order


def _auth(user):
    from base.models import Session
    token = secrets.token_hex(32)
    Session.objects.create(
        user_id=user,
        ip_address='127.0.0.1',
        payload=SessionRepository.hash_token(token),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return {'HTTP_AUTHORIZATION': f'Bearer {token}'}


def test_business_date_boundaries_exclude_0659_and_next_day_0300(
        regular_user, cashier_user):
    from admins.services.dashboard_service import get_range
    from base.services.business_day import resolve_reporting_window

    day = date(2026, 7, 10)
    window = resolve_reporting_window(day, day)
    assert timezone.localtime(window.start_at) == _at(day, 7)
    assert timezone.localtime(window.end_at) == _at(day + timedelta(days=1), 3)

    _order(regular_user, cashier_user, _at(day, 6, 59), '10')
    included = _order(regular_user, cashier_user, _at(day, 7), '20')
    _order(regular_user, cashier_user, _at(day + timedelta(days=1), 3), '40')

    data = get_range(day.isoformat(), day.isoformat())
    assert data['revenue'] == '20'
    assert data['orders'] == 1
    assert data['live_order_feed'] == []
    assert data['range']['start_at'] == window.start_at.isoformat()
    assert data['range']['end_at'] == window.end_at.isoformat()
    assert included.created_at is not None


def test_exact_iso_custom_range_is_one_continuous_interval(
        regular_user, cashier_user):
    from admins.services.dashboard_service import get_range

    first = date(2026, 7, 10)
    last = date(2026, 7, 11)
    _order(regular_user, cashier_user, _at(first, 9, 59), '10')
    _order(regular_user, cashier_user, _at(first, 10), '20')
    _order(regular_user, cashier_user, _at(last, 21, 59), '30')
    _order(regular_user, cashier_user, _at(last, 22), '40')

    data = get_range(
        datetime_from=_at(first, 10).isoformat(),
        datetime_to=_at(last, 22).isoformat(),
    )
    assert data['range']['mode'] == 'custom'
    assert data['range']['start_at'] == _at(first, 10).isoformat()
    assert data['range']['end_at'] == _at(last, 22).isoformat()
    assert data['revenue'] == '50'
    assert data['orders'] == 2


def test_legacy_multiday_overnight_clock_ends_after_final_selected_date():
    from base.services.business_day import resolve_reporting_window

    window = resolve_reporting_window(
        '2026-07-10', '2026-07-11',
        tod_from='22:00', tod_to='02:00',
    )
    assert window.mode == 'custom'
    assert window.start_at == _at(date(2026, 7, 10), 22)
    assert window.end_at == _at(date(2026, 7, 12), 2)


def test_custom_sales_previous_series_is_equal_duration_and_aligned():
    from admins.services.sales_dashboard_service import sales_dashboard

    start = _at(date(2026, 7, 10), 10)
    end = _at(date(2026, 7, 11), 22)
    data = sales_dashboard(
        datetime_from=start.isoformat(),
        datetime_to=end.isoformat(),
    )
    previous = data['previous_period']
    previous_start = datetime.fromisoformat(previous['range']['start_at'])
    previous_end = datetime.fromisoformat(previous['range']['end_at'])

    assert previous_end - previous_start == end - start
    assert previous_end == start
    assert len(data['dayLabels']) == 2
    assert len(previous['labels']) == len(data['dayLabels'])
    assert len(previous['revenue_series']) == len(data['revenue30'])
    assert len(previous['expense_series']) == len(data['expense30'])
    assert len(previous['order_series']) == len(data['channelDays'])


def test_product_trends_returns_exact_previous_query_hint():
    from admins.services.product_analytics_service import products_trends
    from base.services.business_day import resolve_reporting_window

    current = resolve_reporting_window(
        datetime_from=_at(date(2026, 7, 10), 10),
        datetime_to=_at(date(2026, 7, 11), 22),
    )
    data = products_trends(
        current.date_from, current.date_to, top_n=7, window=current,
    )
    hint = data['previous_period']
    query = hint['query']
    previous = resolve_reporting_window(
        datetime_from=query['datetime_from'],
        datetime_to=query['datetime_to'],
    )
    assert previous.end_at == current.start_at
    assert previous.end_at - previous.start_at == current.end_at - current.start_at
    assert query['top_n'] == 7
    assert hint['range'] == previous.metadata()


def test_four_day_previous_period_has_equal_operating_date_count():
    from admins.services.sales_dashboard_service import sales_dashboard

    data = sales_dashboard(date_from='2026-07-10', date_to='2026-07-13')
    assert data['range']['days'] == 4
    assert data['range']['start_at'] == _at(date(2026, 7, 10), 7).isoformat()
    assert data['range']['end_at'] == _at(date(2026, 7, 14), 3).isoformat()
    previous = data['previous_period']
    assert previous['range']['days'] == 4
    assert previous['range']['from'] == '2026-07-06'
    assert previous['range']['to'] == '2026-07-09'
    assert previous['range']['start_at'] == _at(date(2026, 7, 6), 7).isoformat()
    assert previous['range']['end_at'] == _at(date(2026, 7, 10), 3).isoformat()
    assert len(previous['labels']) == 4


def test_iso_window_is_consistent_for_orders_stats_and_shift_summary(
        admin_user, regular_user, cashier_user):
    from base.models import Shift

    day = date(2026, 7, 10)
    inside = _order(regular_user, cashier_user, _at(day, 12), '200')
    _order(regular_user, cashier_user, _at(day, 23), '900')
    included_shift = Shift.objects.create(
        user=cashier_user,
        start_time=_at(day, 11),
        end_time=_at(day, 15),
        status=Shift.Status.ENDED,
    )
    Shift.objects.create(
        user=cashier_user,
        start_time=_at(day, 23),
        end_time=_at(day + timedelta(days=1), 1),
        status=Shift.Status.ENDED,
    )
    query = {
        'datetime_from': _at(day, 10).isoformat(),
        'datetime_to': _at(day, 22).isoformat(),
    }
    client = Client()
    auth = _auth(admin_user)
    rows = client.get('/api/admins/orders', query, **auth)
    assert rows.status_code == 200, rows.content
    assert [row['id'] for row in rows.json()['data']['orders']] == [inside.id]

    stats = client.get('/api/admins/orders/stats', query, **auth)
    assert stats.status_code == 200, stats.content
    assert stats.json()['data']['total_orders'] == 1
    assert stats.json()['data']['total_revenue'] == '200'

    shifts = client.get('/api/admins/shifts', query, **auth)
    assert shifts.status_code == 200, shifts.content
    data = shifts.json()['data']
    assert [row['id'] for row in data['shifts']] == [included_shift.id]
    assert data['summary']['shift_count'] == 1
    assert data['filters']['start_at'] == query['datetime_from']
    assert data['filters']['end_at'] == query['datetime_to']


def test_iso_only_analytics_endpoints_use_custom_dates_not_today(admin_user):
    start = _at(date(2026, 2, 10), 10)
    end = _at(date(2026, 2, 11), 22)
    query = {
        'datetime_from': start.isoformat(),
        'datetime_to': end.isoformat(),
    }
    client = Client()
    auth = _auth(admin_user)
    endpoints = (
        '/api/admins/analytics/menu-engineering',
        '/api/admins/analytics/products/overview',
        '/api/admins/analytics/products/categories',
        '/api/admins/analytics/products/pareto',
        '/api/admins/analytics/products/trends',
        '/api/admins/analytics/products/affinity',
        '/api/admins/analytics/shifts/cashiers',
        '/api/admins/analytics/shifts/kitchen',
        '/api/admins/staff/performance',
    )
    for endpoint in endpoints:
        response = client.get(endpoint, query, **auth)
        assert response.status_code == 200, (endpoint, response.content)
        range_ = response.json()['data']['range']
        assert range_['from'] == '2026-02-10', endpoint
        assert range_['to'] == '2026-02-11', endpoint
        assert range_['start_at'] == start.isoformat(), endpoint
        assert range_['end_at'] == end.isoformat(), endpoint


def test_sales_rejects_invalid_explicit_or_half_iso_range(admin_user):
    client = Client()
    auth = _auth(admin_user)
    invalid = client.get(
        '/api/admins/dashboard/sales', {'from': 'not-a-date'}, **auth,
    )
    assert invalid.status_code == 422
    assert 'range' in invalid.json()['errors']

    half_iso = client.get(
        '/api/admins/dashboard/sales',
        {'datetime_from': _at(date(2026, 7, 10), 10).isoformat()},
        **auth,
    )
    assert half_iso.status_code == 422
    assert 'supplied together' in half_iso.json()['message']


def test_dashboard_sales_expenses_itemized_total_reconciles(
        admin_user, cashier_user):
    from base.models import Shift
    from cashbox.models import CashboxExpense, CashboxExpenseCategory

    day = date(2026, 7, 10)
    shift = Shift.objects.create(
        user=cashier_user,
        start_time=_at(day, 7),
        end_time=_at(day, 23),
        status=Shift.Status.ENDED,
    )
    category = CashboxExpenseCategory.objects.create(name='Supplies')
    visible = CashboxExpense.objects.create(
        shift=shift,
        category=category,
        amount=Decimal('15000'),
        comment='Napkins',
        created_by=cashier_user,
    )
    outside = CashboxExpense.objects.create(
        shift=shift,
        category=category,
        amount=Decimal('99000'),
        comment='Outside',
        created_by=cashier_user,
    )
    deleted = CashboxExpense.objects.create(
        shift=shift,
        category=category,
        amount=Decimal('5000'),
        comment='Deleted',
        created_by=cashier_user,
        is_deleted=True,
    )
    CashboxExpense.objects.filter(pk=visible.pk).update(created_at=_at(day, 12))
    CashboxExpense.objects.filter(pk=outside.pk).update(
        created_at=_at(day + timedelta(days=1), 4),
    )
    CashboxExpense.objects.filter(pk=deleted.pk).update(created_at=_at(day, 13))

    client = Client()
    response = client.get(
        '/api/admins/dashboard/sales/expenses',
        {'from': day.isoformat(), 'to': day.isoformat(), 'limit': 10},
        **_auth(admin_user),
    )
    assert response.status_code == 200
    data = response.json()['data']
    assert data['total_expense'] == '15000'
    assert data['pagination']['total'] == 1
    expense_row = data['expenses'][0]
    from django.utils.dateparse import parse_datetime
    assert parse_datetime(expense_row.pop('created_at')) == _at(day, 12)
    assert data['expenses'] == [{
        'id': visible.id,
        'amount': '15000',
        'category': 'Supplies',
        'comment': 'Napkins',
        'shift_id': shift.id,
        'cashier_name': 'Cashier One',
    }]

    sales = client.get(
        '/api/admins/dashboard/sales',
        {'from': day.isoformat(), 'to': day.isoformat()},
        **_auth(admin_user),
    ).json()['data']
    assert sum(Decimal(value) for value in sales['expense30']) == Decimal(
        data['total_expense'],
    )
