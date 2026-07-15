"""Regression contracts for the admin Orders/Shifts pages and exports."""
from datetime import date, timedelta
from decimal import Decimal

import pytest


pytestmark = pytest.mark.django_db
BUSINESS_DATE = date(2026, 7, 15)


def _window():
    from base.services.business_day import range_window
    return range_window(BUSINESS_DATE, BUSINESS_DATE)


def _order(user, cashier, product, *, total, when, paid=True,
           status='COMPLETED', method='CASH', order_type='HALL',
           phone='99890-needle', branch_id='branch1'):
    from base.models import Order, OrderItem

    order = Order.objects.create(
        user=user,
        cashier=cashier,
        branch_id=branch_id,
        order_type=order_type,
        status=status,
        is_paid=paid,
        payment_method=method if paid else None,
        paid_at=when if paid else None,
        phone_number=phone,
        description='Needle contract order',
        subtotal=Decimal(total),
        total_amount=Decimal(total),
        display_id=Order.objects.count() + 1,
        order_number=Order.objects.count() + 1,
    )
    Order.objects.filter(pk=order.pk).update(created_at=when)
    OrderItem.objects.create(
        order=order,
        product=product,
        quantity=1,
        price=Decimal(total),
        original_price=Decimal(total),
    )
    order.refresh_from_db()
    return order


def _product(name, slug, price='100'):
    from base.models import Category, Product

    category = Category.objects.create(name=f'{name} category', slug=slug)
    return category, Product.objects.create(
        name=name,
        category=category,
        price=Decimal(price),
    )


def test_orders_list_and_stats_share_every_filter_and_stats_are_global(
        regular_user, cashier_user, other_cashier_user):
    from admins.services.order_service import AdminOrderService

    lo, _ = _window()
    when = lo + timedelta(hours=4)
    category, needle = _product('Needle Burger', 'needle-burger')
    _, other = _product('Other Burger', 'other-burger')

    matching = {
        _order(
            regular_user, cashier_user, needle,
            total='100', when=when,
        ).id,
        _order(
            regular_user, cashier_user, needle,
            total='200', when=when + timedelta(minutes=1),
        ).id,
    }
    # Each row below violates one of the shared filters.
    _order(
        regular_user, cashier_user, needle,
        total='300', when=when, method='PAYME',
    )
    _order(
        regular_user, other_cashier_user, needle,
        total='400', when=when,
    )
    _order(
        regular_user, cashier_user, other,
        total='500', when=when,
    )
    _order(
        regular_user, cashier_user, needle,
        total='600', when=when, order_type='DELIVERY',
    )
    _order(
        regular_user, cashier_user, needle,
        total='700', when=when, paid=False, status='PREPARING',
    )

    filters = {
        'statuses': 'COMPLETED',
        'payment_status': 'PAID',
        'payment_method': 'CASH',
        'category_ids': str(category.id),
        'product_ids': str(needle.id),
        'cashier_id': str(cashier_user.id),
        'order_type': 'HALL',
        'date_from': BUSINESS_DATE.isoformat(),
        'date_to': BUSINESS_DATE.isoformat(),
        'search': 'needle',
    }
    first, status = AdminOrderService.get_all_orders(
        page=1, per_page=1, include_items=False, **filters,
    )
    assert status == 200
    assert first['data']['pagination']['total_orders'] == 2
    assert len(first['data']['orders']) == 1

    second, status = AdminOrderService.get_all_orders(
        page=2, per_page=1, include_items=False, **filters,
    )
    assert status == 200
    listed = {
        first['data']['orders'][0]['id'],
        second['data']['orders'][0]['id'],
    }
    assert listed == matching

    stats, status = AdminOrderService.get_order_stats(**filters)
    assert status == 200
    data = stats['data']
    assert data['total_orders'] == 2
    assert data['paid_orders'] == 2
    assert Decimal(data['total_revenue']) == Decimal('300')
    assert Decimal(data['payment_breakdown']['cash']) == Decimal('300')
    assert data['filters']['search'] == 'needle'
    assert data['filters']['product_ids'] == [needle.id]


def test_shift_rows_and_summary_share_business_filters_not_page_or_sort(
        cashier_user, regular_user):
    from admins.services.shift_service import ShiftService
    from base.models import Shift

    lo, _ = _window()
    closed = Shift.objects.create(
        user=cashier_user,
        branch_id='branch1',
        start_time=lo + timedelta(hours=1),
        end_time=lo + timedelta(hours=3),
        status=Shift.Status.COMPLETED,
        total_orders=2,
        total_revenue=Decimal('100'),
        cash_collected=Decimal('100'),
    )
    live = Shift.objects.create(
        user=cashier_user,
        branch_id='branch1',
        start_time=lo + timedelta(hours=4),
        status=Shift.Status.ACTIVE,
    )
    _order(
        regular_user, cashier_user,
        _product('Live Product', 'live-product', '50')[1],
        total='50', when=lo + timedelta(hours=5),
    )

    common = {
        'user_id': str(cashier_user.id),
        'date_from': BUSINESS_DATE.isoformat(),
        'date_to': BUSINESS_DATE.isoformat(),
        'per_page': 1,
    }
    first, status = ShiftService.list(page=1, order_by='start_time', **common)
    second, status2 = ShiftService.list(page=2, order_by='start_time', **common)
    reverse, status3 = ShiftService.list(page=1, order_by='-start_time', **common)
    assert status == status2 == status3 == 200
    assert {
        first['data']['shifts'][0]['id'],
        second['data']['shifts'][0]['id'],
    } == {closed.id, live.id}
    assert first['data']['summary'] == second['data']['summary']
    assert first['data']['summary'] == reverse['data']['summary']
    summary = first['data']['summary']
    assert summary['shift_count'] == 2
    assert summary['live_count'] == 1
    assert summary['closed_count'] == 1
    assert summary['total_orders'] == 3
    assert Decimal(summary['total_revenue']) == Decimal('150')
    assert first['data']['pagination']['total_shifts'] == 2

    closed_only, status = ShiftService.list(
        closed_only=True,
        order_by='-total_revenue',
        **common,
    )
    assert status == 200
    assert closed_only['data']['summary']['shift_count'] == 1
    assert closed_only['data']['shifts'][0]['id'] == closed.id


def test_shift_top_products_use_cashier_window_and_paid_noncancelled_set(
        regular_user, cashier_user, other_cashier_user):
    from admins.services.analytics_service import shift_performance
    from base.models import Shift

    lo, _ = _window()
    start = lo + timedelta(hours=2)
    end = lo + timedelta(hours=6)
    shift = Shift.objects.create(
        user=cashier_user,
        branch_id='branch1',
        start_time=start,
        end_time=end,
        status=Shift.Status.COMPLETED,
    )
    _, sold = _product('Sold Product', 'sold-product', '100')
    _, unpaid = _product('Unpaid Product', 'unpaid-product', '200')
    _, cancelled = _product('Cancelled Product', 'cancelled-product', '300')
    _, other_cashier = _product('Other Cashier Product', 'other-cashier-product', '400')
    _, outside = _product('Outside Product', 'outside-product', '500')

    _order(regular_user, cashier_user, sold, total='100', when=start + timedelta(minutes=10))
    _order(
        regular_user, cashier_user, unpaid, total='200',
        when=start + timedelta(minutes=20), paid=False, status='PREPARING',
    )
    _order(
        regular_user, cashier_user, cancelled, total='300',
        when=start + timedelta(minutes=30), status='CANCELED',
    )
    _order(
        regular_user, other_cashier_user, other_cashier, total='400',
        when=start + timedelta(minutes=40),
    )
    _order(
        regular_user, cashier_user, outside, total='500',
        when=end + timedelta(minutes=1),
    )

    perf_names = [row['product_name'] for row in shift_performance(shift)['top_products']]
    assert perf_names == ['Sold Product']
