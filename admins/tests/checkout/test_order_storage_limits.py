"""Reject unrepresentable orders before changing sales, lines, or stock."""
from decimal import Decimal
import importlib

import pytest
from django.utils import timezone

from base.models import (
    ChefQueueCounter, Customer, DisplayIdCounter, Order, OrderItem,
    SequenceCounter, Shift,
)

pytestmark = pytest.mark.django_db
SURFACES = [
    ('admins', 'AdminOrderService', 'add_item_to_order', 'update_order_item'),
]


@pytest.fixture(params=SURFACES, ids=lambda value: value[0])
def surface(request, regular_user, cashier_user, monkeypatch):
    package, service_name, add_name, update_name = request.param
    module = importlib.import_module(f'{package}.services.order_service')
    service = getattr(module, service_name)
    actor = cashier_user
    if package == 'waiters':
        actor = regular_user
        actor.role = 'WAITER'
        actor.save(update_fields=['role'])
    else:
        Shift.objects.create(user=actor, status='ACTIVE', start_time=timezone.now(), branch_id='branch1')
    kwargs = ({'waiter_user_id': actor.id} if package == 'waiters' else
              {'cashier_id': actor.id, 'user_id': actor.id, 'user_role': 'CASHIER'} if package == 'customers' else {})
    return package, module, service, actor, add_name, update_name, kwargs


def _state():
    return [list(model.objects.order_by('pk').values()) for model in (
        Order, OrderItem, Customer, DisplayIdCounter, ChefQueueCounter, SequenceCounter,
    )]


def test_create_rejects_total_overflow_without_side_effects(surface, product, monkeypatch):
    package, module, service, actor, *_ = surface
    product.price = Decimal('60000000')
    product.save(update_fields=['price'])
    def forbidden(*args, **kwargs):
        pytest.fail('Rejected order reached stock writes')
    monkeypatch.setattr(module, '_apply_order_stock_transition', forbidden)
    before = _state()
    kwargs = {'user_id': actor.id, 'items': [{'product_id': product.id, 'quantity': 2}]}
    if package != 'waiters':
        kwargs['cashier_id'] = actor.id
    if package == 'admins':
        kwargs.update(customer_name='Synthetic limit test', phone_number='+998901234567')
    result, status = service.create_order(**kwargs)
    assert status == 422, result
    assert not result['success']
    assert _state() == before


@pytest.mark.parametrize('operation, price, initial_quantity, quantity', [
    ('add', '60000000', 1, 1),
    ('update', '60000000', 1, 2),
    ('add', '0.01', 2**31 - 1, 1),
    ('update', '0.01', 1, 2**31),
    ('update', '10', 1, True),
])
def test_edit_rejects_storage_overflow_without_side_effects(surface, order_factory, product, monkeypatch, operation, price, initial_quantity, quantity):
    package, module, service, actor, add_name, update_name, kwargs = surface
    order = order_factory(user=actor, cashier=actor)
    item = order.items.get()
    product.price = item.price = Decimal(price)
    product.save(update_fields=['price'])
    item.quantity = initial_quantity
    item.save(update_fields=['price', 'quantity'])
    order.subtotal = order.total_amount = item.price * item.quantity
    order.save(update_fields=['subtotal', 'total_amount'])
    def forbidden(*args, **kwargs):
        pytest.fail('Rejected item change reached stock writes')
    monkeypatch.setattr(module, '_adjust_order_stock', forbidden)
    before = _state()
    method = getattr(service, add_name if operation == 'add' else update_name)
    result, status = method(order.id, product.id if operation == 'add' else item.id, quantity, **kwargs)
    assert status == 422, result
    assert not result['success']
    assert _state() == before


def test_edit_allows_exact_money_limit(surface, order_factory, product):
    package, module, service, actor, add_name, update_name, kwargs = surface
    order = order_factory(user=actor, cashier=actor)
    item = order.items.get()
    item.price = Decimal('33333333.33')
    item.save(update_fields=['price'])
    order.subtotal = order.total_amount = item.price
    order.save(update_fields=['subtotal', 'total_amount'])
    result, status = getattr(service, update_name)(order.id, item.id, 3, **kwargs)
    assert status == 200, result
    order.refresh_from_db()
    assert order.total_amount == Decimal('99999999.99')


@pytest.mark.parametrize('instant', [False, True])
def test_new_line_checks_live_total_even_with_stale_header(surface, order_factory, product, instant):
    from base.models import Product
    package, module, service, actor, add_name, update_name, kwargs = surface
    order = order_factory(user=actor, cashier=actor)
    item = order.items.get()
    item.price = Decimal('60000000')
    item.save(update_fields=['price'])
    # Deliberately leave the old 10.00 header; capacity comes from live lines.
    extra = Product.objects.create(
        name='Second synthetic product', category=product.category,
        price=Decimal('60000000'), is_instant=instant,
    )
    before = _state()
    result, status = getattr(service, add_name)(order.id, extra.id, 1, **kwargs)
    assert status == 422, result
    assert _state() == before
