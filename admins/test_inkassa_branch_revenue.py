from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import override_settings


pytestmark = pytest.mark.django_db


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


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_inkassa_changes_only_selected_branch(admin_user):
    from base.models import CashRegister
    from admins.services.inkassa_service import AdminInkassaService

    first = CashRegister.objects.create(branch_id='branch-a', current_balance=100)
    second = CashRegister.objects.create(branch_id='branch-b', current_balance=200)

    result, status = AdminInkassaService.perform(
        admin_user, {'cash': '30'}, branch_id='branch-a',
    )
    assert status == 200, result
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.current_balance == Decimal('70')
    assert second.current_balance == Decimal('200')
    assert result['data']['branch_id'] == 'branch-a'


def test_sales_stats_use_created_at_and_net_product_revenue(
    regular_user, cashier_user,
):
    from base.models import Category, Order, OrderItem, Product
    from base.services.business_day import today_window
    from admins.services.inkassa_service import AdminInkassaService

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
    assert Decimal(stats['today']['total_revenue']) == Decimal('80')
    assert stats['today']['order_count'] == 1
    assert len(stats['top_products']) == 1
    assert Decimal(stats['top_products'][0]['total_revenue']) == Decimal('80')
