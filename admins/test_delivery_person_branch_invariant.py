from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone


pytestmark = pytest.mark.django_db


@override_settings(
    DEPLOYMENT_MODE='cloud',
    BRANCH_ID='cloud',
    SYNC_ENABLED=False,
)
def test_admin_order_rejects_foreign_delivery_person_and_accepts_own_branch(
    monkeypatch,
):
    from admins.services import order_service
    AdminOrderService = order_service.AdminOrderService
    from base.models import (
        Category, DeliveryPerson, Order, Product, Shift, User,
    )

    owner = User.objects.create(
        email='owner@example.test', role='ADMIN', status='ACTIVE',
        password='!', branch_id='cloud',
    )
    cashier = User.objects.create(
        email='cashier@example.test', role='CASHIER', status='ACTIVE',
        password='!', branch_id='cloud',
    )
    Shift.objects.create(
        user=cashier,
        start_time=timezone.now(),
        status='ACTIVE',
        branch_id='branch-a',
    )
    category = Category.objects.create(name='Food', branch_id='cloud')
    product = Product.objects.create(
        name='Meal', price=Decimal('50000'), category=category,
        branch_id='cloud',
    )
    foreign = DeliveryPerson(
        first_name='Foreign', phone_number='998900000001',
        branch_id='cloud',
    )
    # Simulate a legacy cloud-owned row from before cloud branch-scoped creates
    # became fail-closed. Normal application code can no longer mint this row.
    foreign.save(_syncing=True)
    own = DeliveryPerson.objects.create(
        first_name='Local', phone_number='998900000002',
        branch_id='branch-a',
    )
    items = [{'product_id': product.id, 'quantity': 1}]
    monkeypatch.setattr(
        order_service, '_apply_order_stock_transition',
        lambda *args, **kwargs: None,
    )

    result, status = AdminOrderService.create_order(
        owner.id, items, cashier_id=cashier.id,
        order_type='DELIVERY', delivery_person_id=foreign.id,
    )

    assert status == 400, result
    assert 'different branch' in result['message'].lower()
    assert not Order.objects.exists()

    result, status = AdminOrderService.create_order(
        owner.id, items, cashier_id=cashier.id,
        order_type='DELIVERY', delivery_person_id=own.id,
    )

    assert status == 201, result
    order = Order.objects.get(pk=result['data']['order_id'])
    assert order.branch_id == 'branch-a'
    assert order.delivery_person_id == own.id
    assert order.items.get().branch_id == 'branch-a'
