from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _freeze_at_open_business_time(monkeypatch):
    """Keep current-period assertions outside the deliberate 03:00-07:00 gap."""
    from base.services.business_day import business_date, day_window

    start, _ = day_window(business_date())
    frozen_now = start + timedelta(hours=5)
    monkeypatch.setattr(timezone, 'now', lambda: frozen_now)
    return frozen_now


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_cloud_requires_branch_when_multiple_registers(admin_user):
    from base.models import CashRegister
    from admins.services.inkassa_service import AdminInkassaService

    CashRegister.objects.create(branch_id='branch-a', current_balance=100)
    CashRegister.objects.create(branch_id='branch-b', current_balance=200)

    result, status = AdminInkassaService.get_balance()
    assert status == 422
    assert 'branch_id' in result['errors']

    result, status = AdminInkassaService.get_balance('branch-b')
    assert status == 200
    assert result['data']['branch_id'] == 'branch-b'
    assert Decimal(result['data']['balance']) == Decimal('200')
    assert Decimal(
        result['data']['available_uncollected_cash']
    ) == Decimal('200')
    assert Decimal(
        result['data']['reported_uncollected_cash_cursor']
    ) == Decimal('200')
    assert result['data']['balance_scope'] == {
        'kind': 'BRANCH_UNCOLLECTED_CASH_CURSOR',
        'branch_id': 'branch-b',
        'is_shift_drawer': False,
        'label': 'Uncollected branch cash (not a shift drawer)',
    }


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_inkassa_changes_only_selected_branch(admin_user):
    from base.models import CashRegister
    from base.services.treasury_service import TreasuryService
    from admins.services.inkassa_service import AdminInkassaService

    admin_user.branch_id = 'cloud'
    admin_user.save(update_fields=['branch_id'])
    first = CashRegister.objects.create(branch_id='branch-a', current_balance=100)
    second = CashRegister.objects.create(branch_id='branch-b', current_balance=200)
    TreasuryService.post_shift_settlement(
        9301, {'CASH': '30'}, performed_by=admin_user, branch_id='branch-a',
    )

    result, status = AdminInkassaService.perform(
        admin_user,
        {'cash': '30'},
        branch_id='branch-a',
        batch_key='branch-a-cash',
    )
    assert status == 200, result
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.current_balance == Decimal('100')
    assert second.current_balance == Decimal('200')
    assert Decimal(result['data']['balance_after']) == Decimal('70')
    assert result['data']['branch_id'] == 'branch-a'


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_mixed_inkassa_batch_owns_period_revenue_once(
    admin_user, cashier_user, regular_user, monkeypatch,
):
    from base.models import CashRegister, Inkassa, Order
    from base.services.treasury_service import TreasuryService
    from admins.services.inkassa_service import AdminInkassaService

    frozen_now = _freeze_at_open_business_time(monkeypatch)
    admin_user.branch_id = 'cloud'
    admin_user.save(update_fields=['branch_id'])
    order = Order.objects.create(
        user=regular_user,
        cashier=cashier_user,
        branch_id='branch-a',
        status='COMPLETED',
        is_paid=True,
        payment_method='CASH',
        paid_at=timezone.now(),
        subtotal='100.00',
        total_amount='100.00',
    )
    # Inkassa owns the half-open accounting period ending at ``now``. Keep the
    # sale just inside that boundary rather than exactly on it.
    Order.objects.filter(pk=order.pk).update(
        accounting_recorded_at=frozen_now - timedelta(microseconds=1),
    )
    # Paid Order.save already created the branch register as its accounting
    # lock. Seed revenue on that same drawer rather than creating a duplicate.
    register = CashRegister.objects.get(branch_id='branch-a')
    register.current_balance = Decimal('100.00')
    register.save(update_fields=['current_balance'])
    TreasuryService.post_shift_settlement(
        9302,
        {'CASH': '10.00', 'UZCARD': '20.00'},
        performed_by=admin_user,
        branch_id='branch-a',
    )

    result, status = AdminInkassaService.perform(
        admin_user,
        {'cash': '10.00', 'uzcard': '20.00'},
        branch_id='branch-a',
        batch_key='branch-a-mixed-period',
    )
    assert status == 200, result
    ids = [row['id'] for row in result['data']['inkassas']]
    rows = list(Inkassa.objects.filter(pk__in=ids).order_by('pk'))
    assert len(rows) == 2
    assert sum((row.total_revenue for row in rows), Decimal('0')) == Decimal('100')
    assert sum(row.total_orders for row in rows) == 1
    assert rows[0].total_revenue == Decimal('100')
    assert rows[1].total_revenue == Decimal('0')


def test_sales_stats_use_paid_at_and_net_product_revenue(
    regular_user, cashier_user, monkeypatch,
):
    from base.models import Category, Order, OrderItem, Product
    from base.services.business_day import today_window
    from admins.services.inkassa_service import AdminInkassaService

    _freeze_at_open_business_time(monkeypatch)
    start, _ = today_window()
    inside = start + timedelta(hours=1)
    outside = start - timedelta(hours=1)
    category = Category.objects.create(name='Food', slug='food')
    product = Product.objects.create(name='Burger', category=category, price=100)

    created_today = Order.objects.create(
        user=regular_user, cashier=cashier_user, branch_id='branch-a',
        status='COMPLETED', is_paid=True, payment_method='CASH',
        subtotal=100, discount_amount=20, total_amount=80,
    )
    Order.objects.filter(pk=created_today.pk).update(
        created_at=inside, paid_at=outside,
    )
    OrderItem.objects.create(
        order=created_today, product=product, quantity=1, price=100,
    )

    paid_today_but_created_before = Order.objects.create(
        user=regular_user, cashier=cashier_user, branch_id='branch-a',
        status='COMPLETED', is_paid=True, payment_method='CASH',
        subtotal=200, total_amount=200,
    )
    Order.objects.filter(pk=paid_today_but_created_before.pk).update(
        created_at=outside, paid_at=inside,
    )
    OrderItem.objects.create(
        order=paid_today_but_created_before, product=product, quantity=2, price=100,
    )

    result, status = AdminInkassaService.get_stats(branch_id='branch-a')
    assert status == 200, result
    stats = result['data']['stats']
    assert Decimal(stats['today']['total_revenue']) == Decimal('200')
    assert stats['today']['order_count'] == 1
    assert len(stats['top_products']) == 1
    assert Decimal(stats['top_products'][0]['total_revenue']) == Decimal('200')


def test_sales_stats_book_refund_at_refunded_at(
    regular_user, cashier_user, monkeypatch,
):
    from base.models import Category, Order, OrderItem, OrderRefund, Product, Shift
    from base.services.business_day import today_window
    from admins.services.inkassa_service import AdminInkassaService

    _freeze_at_open_business_time(monkeypatch)
    start, _ = today_window()
    sale_time = start - timedelta(hours=1)
    refund_time = start + timedelta(hours=1)
    category = Category.objects.create(name='Refund food', slug='refund-food')
    product = Product.objects.create(
        name='Refund burger', category=category, price=80,
    )
    shift = Shift.objects.create(
        user=cashier_user,
        status=Shift.Status.ACTIVE,
        start_time=start,
        branch_id='branch-a',
    )
    order = Order.objects.create(
        user=regular_user,
        cashier=cashier_user,
        branch_id='branch-a',
        status='CANCELED',
        is_paid=True,
        payment_method='CASH',
        paid_at=sale_time,
        subtotal=80,
        total_amount=80,
    )
    OrderItem.objects.create(
        order=order, product=product, quantity=1, price=80,
    )
    OrderRefund.objects.create(
        order=order,
        shift=shift,
        cashier=cashier_user,
        amount='80.00',
        cash_amount='80.00',
        drawer_cash_amount='80.00',
        refunded_at=refund_time,
        source=OrderRefund.Source.ORDER_CANCEL,
        source_id=str(order.uuid),
        branch_id='branch-a',
    )

    result, status = AdminInkassaService.get_stats(branch_id='branch-a')
    assert status == 200, result
    stats = result['data']['stats']
    assert Decimal(stats['today']['total_revenue']) == Decimal('-80.00')
    assert stats['today']['order_count'] == 0
    assert stats['today']['refund_count'] == 1
    assert Decimal(stats['today']['refund_total']) == Decimal('80.00')
    assert stats['cashier_performance'] == [{
        'cashier_id': cashier_user.id,
        'cashier_name': (
            f'{cashier_user.first_name} {cashier_user.last_name}'
        ).strip(),
        'total_revenue': '-80',
        'order_count': 0,
        'refund_count': 1,
    }]
    assert len(stats['top_products']) == 1
    assert stats['top_products'][0]['total_quantity'] == -1
    assert Decimal(stats['top_products'][0]['total_revenue']) == Decimal('-80.00')
