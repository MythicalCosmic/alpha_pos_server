"""Courier lifecycle tests (spec §10). Run with the repo's pytest:

    pytest couriers/tests/test_courier_flow.py

Channel-layer sends are no-ops here (no Redis / InMemory layer needed) because
the realtime funnel swallows a missing layer — these assert the DB/state
transitions, which is what §10 calls for.
"""
from decimal import Decimal

import pytest
from django.utils import timezone
from datetime import timedelta

from base.models import User, Order
from base.security.hashing import hash_password
from couriers.models import Courier, DeliveryAssignment
from couriers import services

pytestmark = pytest.mark.django_db


def _courier(code='CR-1', phone='+998900000000'):
    user = User.objects.create(email=f'{code}@t.local', first_name='A', last_name='B',
                               role='CASHIER', status='ACTIVE',
                               password=hash_password('x'))
    return Courier.objects.create(user=user, code=code, phone=phone, branch_id='cloud')


def _order(courier, step='ASSIGNED', **extra):
    order = Order.objects.create(user=courier.user, order_type='DELIVERY',
                                 status='PREPARING', branch_id='cloud',
                                 total_amount=Decimal('100000'))
    a = DeliveryAssignment.objects.create(order=order, courier=courier, step=step,
                                           assigned_at=timezone.now(), **extra)
    return order, a


def test_status_is_forward_only():
    c = _courier()
    _, a = _order(c, step='PICKED_UP')
    # backward (PICKED_UP -> READY) is rejected
    updated, err = services.advance_status(a, 'READY')
    assert updated is None and err
    # forward (PICKED_UP -> ON_WAY) is allowed
    updated, err = services.advance_status(a, 'ON_WAY')
    assert err is None and updated.step == 'ON_WAY'


def test_courier_cannot_set_ready():
    c = _courier()
    _, a = _order(c, step='ASSIGNED')
    updated, err = services.advance_status(a, 'READY')
    assert updated is None and 'Courier cannot set' in err


def test_delivered_closes_order():
    c = _courier()
    order, a = _order(c, step='ON_WAY')
    updated, err = services.advance_status(a, 'DELIVERED')
    assert err is None and updated.step == 'DELIVERED'
    order.refresh_from_db()
    assert order.status == 'COMPLETED'
    assert updated.delivered_at is not None


def test_accept_window_expiry():
    c = _courier()
    _, a = _order(c, step='ASSIGNED', expires_at=timezone.now() - timedelta(seconds=1))
    ok, err = services.accept(a)
    assert ok is False and 'expired' in err.lower()


def test_mark_ready_only_from_assigned():
    c = _courier()
    order, a = _order(c, step='ASSIGNED')
    services.mark_ready(order)
    a.refresh_from_db()
    assert a.step == 'READY' and a.ready_at is not None
    # idempotent: a second call doesn't bounce it back or error
    services.mark_ready(order)
    a.refresh_from_db()
    assert a.step == 'READY'
