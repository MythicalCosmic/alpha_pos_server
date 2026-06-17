"""Unified phone-keyed client: a Telegram login that shares the same phone as an
in-store walk-in converges onto one base.Customer, and that client's in-store
base.Orders surface in the bot order history."""
import pytest
from decimal import Decimal

from smartfood.models import Customer as SfCustomer
from smartfood.services.order_service import BotOrderService
from smartfood.services.auth_service import _link_base_customer
from base.models import (
    Customer as BaseCustomer, Order, OrderItem, Product, Category, User,
)

pytestmark = pytest.mark.django_db


def test_phone_link_converges_and_surfaces_instore_orders():
    u = User.objects.create(email='bot@local', first_name='B', last_name='C',
                            role='USER', status='ACTIVE', password='!')
    cat = Category.objects.create(name='Food')
    p = Product.objects.create(name='Plov', price=Decimal('40000'), category=cat)
    base_client = BaseCustomer.objects.create(name='Ali', phone_number='998901112233')
    o = Order.objects.create(user=u, customer=base_client, order_type='HALL',
                             status='COMPLETED', is_paid=True,
                             total_amount=Decimal('40000'), branch_id='branch1')
    OrderItem.objects.create(order=o, product=p, quantity=1, price=p.price)

    # Same person opens the bot (telegram_id) and shares the same number.
    sf = SfCustomer.objects.create(telegram_id=55501, first_name='Ali',
                                   phone_number='+998 90 111 22 33')
    _link_base_customer(sf)

    base_client.refresh_from_db()
    assert base_client.telegram_id == 55501                       # converged
    assert BaseCustomer.objects.filter(phone_number__contains='111').count() == 1

    res, st = BotOrderService.list_for(sf, status='history')
    assert st == 200
    instore = res['data'].get('in_store', [])
    assert len(instore) == 1
    assert instore[0]['source'] == 'in_store'
    assert instore[0]['totals']['total'] == 40000
    assert instore[0]['items'][0]['name'] == 'Plov'


def test_active_tab_excludes_instore():
    sf = SfCustomer.objects.create(telegram_id=42, phone_number='998900000000')
    res, st = BotOrderService.list_for(sf, status='active')
    assert st == 200 and 'in_store' not in res['data']


def test_no_link_no_instore():
    sf = SfCustomer.objects.create(telegram_id=999, phone_number='998905554433')
    res, st = BotOrderService.list_for(sf, status='history')
    assert st == 200 and res['data']['in_store'] == []
