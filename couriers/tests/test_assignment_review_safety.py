import json
import secrets
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone

from base.models import DeliveryPerson, Order, Session, Shift, User
from base.repositories.session import SessionRepository
from couriers import services
from couriers.models import Courier, CourierNotification, DeliveryAssignment


pytestmark = pytest.mark.django_db


def _actor(role, *, branch_id='cloud', active_branch=None):
    suffix = secrets.token_hex(4)
    user = User.objects.create(
        email=f'{role.lower()}-{suffix}@test.local', first_name=role,
        last_name='Actor', role=role, status='ACTIVE', password='!',
        branch_id=branch_id,
    )
    if active_branch:
        Shift.objects.create(
            user=user, status=Shift.Status.ACTIVE,
            start_time=timezone.now(), branch_id=active_branch,
        )
    token = secrets.token_hex(32)
    Session.objects.create(
        user_id=user, payload=SessionRepository.hash_token(token),
        ip_address='127.0.0.1',
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return user, token


def _courier(code, phone, branch):
    user = User.objects.create(
        email=f'{code.lower()}@rider.local', first_name=code,
        last_name='Rider', role='COURIER', status='ACTIVE', password='!',
    )
    return Courier.objects.create(
        user=user, code=code, phone=phone, branch_id=branch,
        first_name=code,
    )


def _order(actor, branch):
    return Order.objects.create(
        user=actor, cashier=actor, order_type=Order.OrderType.DELIVERY,
        status=Order.Status.PREPARING, branch_id=branch,
        total_amount=Decimal('100000'),
    )


def _auth(token):
    return {'HTTP_AUTHORIZATION': f'Bearer {token}'}


def test_manager_uses_active_shift_branch_while_global_admin_can_cross_branches():
    manager, token = _actor('MANAGER', active_branch='branch-a')
    own_order = _order(manager, 'branch-a')
    foreign_order = _order(manager, 'branch-b')
    own = _courier('CR-A', '+998901110001', 'branch-a')
    foreign = _courier('CR-B', '+998901110002', 'branch-b')
    client = Client()

    listed = client.get('/api/admins/couriers/', **_auth(token))
    assert {row['id'] for row in listed.json()['data']} == {own.code}
    denied = client.post(
        '/api/admins/couriers/assign',
        data=json.dumps({'order_id': foreign_order.pk, 'courier_id': foreign.pk}),
        content_type='application/json', **_auth(token),
    )
    assert denied.status_code == 404

    _admin, admin_token = _actor('ADMIN', branch_id='cloud')
    admin_list = client.get('/api/admins/couriers/', **_auth(admin_token))
    assert {row['id'] for row in admin_list.json()['data']} == {
        own.code, foreign.code,
    }
    assigned = client.post(
        '/api/admins/couriers/assign',
        data=json.dumps({'order_id': foreign_order.pk, 'courier_id': foreign.pk}),
        content_type='application/json', **_auth(admin_token),
    )
    assert assigned.status_code == 200
    assert not DeliveryAssignment.objects.filter(order=own_order).exists()


def test_auto_assignment_excludes_on_way_and_refuses_double_booking():
    actor, _token = _actor('MANAGER', branch_id='branch-a')
    busy = _courier('CR-BUSY', '+998901110003', 'branch-a')
    free = _courier('CR-FREE', '+998901110004', 'branch-a')
    Courier.objects.filter(pk__in=[busy.pk, free.pk]).update(online=True)
    DeliveryAssignment.objects.create(
        order=_order(actor, 'branch-a'), courier=busy,
        step=DeliveryAssignment.Step.ON_WAY, assigned_at=timezone.now(),
    )

    selected = services.pick_available_courier(branch_id='branch-a')
    assert selected.pk == free.pk
    services.assign(_order(actor, 'branch-a'), free)
    with pytest.raises(services.AssignmentConflict, match='active delivery'):
        services.assign(_order(actor, 'branch-a'), free)


def test_notification_insert_failure_does_not_poison_outer_atomic(monkeypatch):
    courier = _courier('CR-NOTIFY', '+998901110005', 'branch-a')

    def fail_notification(**kwargs):
        raise IntegrityError('simulated notification failure')

    monkeypatch.setattr(CourierNotification.objects, 'create', fail_notification)
    with transaction.atomic():
        assert services.notify(courier, title='Optional') is None
        Courier.objects.filter(pk=courier.pk).update(online=True)
    courier.refresh_from_db()
    assert courier.online is True


def test_phone_canonical_unique_and_legacy_replay_guard():
    actor, _token = _actor('MANAGER', branch_id='branch-a')
    courier = _courier('CR-PHONE', '90 123 45 67', 'branch-a')
    assert courier.phone == '998901234567'
    duplicate_user = User.objects.create(
        email='duplicate-rider@test.local', first_name='D', last_name='R',
        role='COURIER', status='ACTIVE', password='!',
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Courier.objects.create(
                user=duplicate_user, code='CR-PHONE-2',
                phone='+998 90 123 45 67', branch_id='branch-a',
            )

    order = _order(actor, 'branch-a')
    services.assign(order, courier)
    legacy = DeliveryPerson.objects.create(
        first_name='Legacy', phone_number='+998909999999',
    )
    order.delivery_person = legacy
    order.save(update_fields=['delivery_person', 'updated_at'])
    order.refresh_from_db()
    assert order.delivery_person_id is None
