from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

import pytest


pytestmark = pytest.mark.django_db


OPENED = datetime(2026, 7, 1, 12, 0, tzinfo=dt_timezone.utc)
PAID = OPENED + timedelta(days=1, hours=2)


def _settled_later(order_factory, opened=OPENED, paid=PAID):
    from base.models import Order

    order = order_factory(status='COMPLETED', is_paid=True)
    Order.objects.filter(pk=order.pk).update(
        created_at=opened,
        paid_at=paid,
        payment_method='CASH',
    )
    order.refresh_from_db()
    return order


def test_range_dashboard_splits_order_volume_from_settled_sales(order_factory):
    from admins.services.dashboard_service import get_range
    from admins.services.sales_dashboard_service import sales_dashboard
    from base.services.business_day import business_date

    order = _settled_later(order_factory)
    opened_day = business_date(OPENED).isoformat()
    paid_day = business_date(PAID).isoformat()

    opened = get_range(opened_day, opened_day)
    settled = get_range(paid_day, paid_day)
    assert opened['orders'] == 1
    assert opened['revenue'] == '0'
    assert opened['units_sold'] == 0
    assert settled['orders'] == 0
    assert settled['revenue'] == str(int(order.total_amount))
    assert settled['units_sold'] == 1

    opened_series = sales_dashboard(
        date_from=opened_day, date_to=opened_day,
    )
    paid_series = sales_dashboard(
        date_from=paid_day, date_to=paid_day,
    )
    assert opened_series['monthRevenue'] == '0'
    assert opened_series['channelDays'][0]['hall'] == 1
    assert paid_series['monthRevenue'] == str(int(order.total_amount))
    assert paid_series['channelDays'][0]['hall'] == 0


def test_today_and_inkassa_stats_credit_payment_day(order_factory):
    from admins.services.dashboard_service import get_today
    from admins.services.inkassa_service import AdminInkassaService
    from base.services.business_day import today_window
    from django.utils import timezone

    start, _end = today_window()
    order = _settled_later(
        order_factory,
        opened=start - timedelta(hours=1),
        paid=timezone.now(),
    )

    today = get_today()['today']
    assert today['orders'] == 0
    assert today['revenue'] == str(int(order.total_amount))
    assert today['units_sold'] == 1

    body, status = AdminInkassaService.get_stats()
    assert status == 200
    assert int(Decimal(body['data']['stats']['today']['total_revenue'])) == int(
        Decimal(str(order.total_amount)),
    )
