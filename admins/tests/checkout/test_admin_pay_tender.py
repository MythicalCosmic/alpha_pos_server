"""Admin mark-as-paid tender-line tests.

Previously it set only is_paid/payment_method/paid_at, so a cloud-paid sale had no
tender lines. MIXED stays OUTPUT-only: a split is recorded by sending `payments`.
"""
import json
import secrets
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from admins.services.order_service import AdminOrderService
from base.repositories.session import SessionRepository

pytestmark = pytest.mark.django_db
D = Decimal


def _cashier():
    from base.models import User
    return User.objects.create(email=f'{secrets.token_hex(4)}@x.local', first_name='C',
                               last_name='X', role='CASHIER', status='ACTIVE', password='!')


def _unpaid_order(cashier, total):
    from base.models import Order, Shift
    Shift.objects.get_or_create(
        user=cashier,
        status='ACTIVE',
        defaults={'start_time': timezone.now() - timedelta(hours=1)},
    )
    return Order.objects.create(user=cashier, cashier=cashier, order_type='HALL',
                                status='COMPLETED', is_paid=False,
                                total_amount=D(total), subtotal=D(total),
                                display_id=Order.objects.count() + 1)


def _lines(order):
    from base.models import OrderPayment
    return sorted((p.method, p.amount) for p in
                  OrderPayment.objects.filter(order=order, is_deleted=False))


def _auth(user):
    from base.models import Session

    token = secrets.token_hex(32)
    Session.objects.create(
        user_id=user,
        ip_address='127.0.0.1',
        user_agent='',
        payload=SessionRepository.hash_token(token),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return {'HTTP_AUTHORIZATION': f'Bearer {token}'}


def _post_payment(client, order, auth, payload, *, key=None):
    headers = dict(auth)
    if key is not None:
        headers['HTTP_IDEMPOTENCY_KEY'] = key
    return client.post(
        f'/api/admins/orders/{order.id}/pay',
        data=json.dumps(payload),
        content_type='application/json',
        **headers,
    )


def test_single_tender_writes_one_line():
    c = _cashier()
    o = _unpaid_order(c, 60000)
    _, st = AdminOrderService.mark_as_paid(o.id, payment_method='UZCARD')
    assert st == 200
    o.refresh_from_db()
    assert o.is_paid and o.payment_method == 'UZCARD'
    assert _lines(o) == [('UZCARD', D('60000.00'))]


def test_configured_electronic_provider_is_valid_checkout_tender():
    from base.models import PaymentMethodConfig
    from cashbox.services.drawer import expected_payment_totals

    PaymentMethodConfig.objects.update_or_create(
        code='CLICK',
        defaults={
            'label': 'Click',
            'is_active': True,
            'treasury_destination': 'BANK',
        },
    )
    cashier = _cashier()
    order = _unpaid_order(cashier, 60000)

    body, status = AdminOrderService.mark_as_paid(
        order.id,
        payment_method='CLICK',
    )

    assert status == 200, body
    assert _lines(order) == [('CLICK', D('60000.00'))]
    shift = order.cashier.shifts.get(status='ACTIVE')
    assert expected_payment_totals(shift)['CLICK'] == D('60000.00')


def test_split_payments_write_lines_and_roll_up_to_mixed():
    c = _cashier()
    o = _unpaid_order(c, 53000)
    _, st = AdminOrderService.mark_as_paid(o.id, payments=[
        {'method': 'HUMO', 'amount': '35000'}, {'method': 'UZCARD', 'amount': '18000'}])
    assert st == 200
    o.refresh_from_db()
    assert o.payment_method == 'MIXED'
    assert _lines(o) == [('HUMO', D('35000')), ('UZCARD', D('18000'))]
    rows = list(o.payments.filter(is_deleted=False).order_by('line_index'))
    assert o.payment_action_id is not None
    assert {row.payment_action_id for row in rows} == {o.payment_action_id}
    assert [row.line_index for row in rows] == [0, 1]


def test_bare_mixed_is_rejected():
    c = _cashier()
    o = _unpaid_order(c, 10000)
    body, st = AdminOrderService.mark_as_paid(o.id, payment_method='MIXED')
    assert st == 422, body


def test_noncash_overpayment_rejected():
    c = _cashier()
    o = _unpaid_order(c, 10000)
    _, st = AdminOrderService.mark_as_paid(o.id, payments=[{'method': 'HUMO', 'amount': '20000'}])
    assert st == 422


def test_short_payment_rejected():
    c = _cashier()
    o = _unpaid_order(c, 10000)
    _, st = AdminOrderService.mark_as_paid(o.id, payments=[{'method': 'UZCARD', 'amount': '5000'}])
    assert st == 422


def test_unpay_preserves_tender_evidence_and_appends_refund():
    from base.models import OrderRefund

    c = _cashier()
    o = _unpaid_order(c, 40000)
    AdminOrderService.mark_as_paid(o.id, payment_method='UZCARD')
    assert _lines(o) == [('UZCARD', D('40000.00'))]
    _, st = AdminOrderService.mark_as_paid(o.id, payment_method='UZCARD')  # already paid
    assert st == 400
    _, st = AdminOrderService.mark_as_unpaid(o.id)
    assert st == 200
    o.refresh_from_db()
    assert o.is_paid is True
    assert o.status == 'CANCELED'
    assert _lines(o) == [('UZCARD', D('40000.00'))]
    assert OrderRefund.objects.filter(order=o, amount=D('40000.00')).exists()


def test_settlement_equals_revenue_for_admin_paid_orders():
    """The FE's actual criterion: an admin-paid sale must be visible to per-tender
    shift settlement, so sum(expected tenders) == revenue (before cashbox expenses)."""
    from base.models import Order, Shift
    from cashbox.services.drawer import expected_payment_totals
    from django.db.models import Sum

    c = _cashier()
    now = timezone.now()
    s = Shift.objects.create(user=c, status='ACTIVE', start_time=now - timedelta(hours=3))

    o1 = _unpaid_order(c, 100000)
    o2 = _unpaid_order(c, 60000)
    o3 = _unpaid_order(c, 53000)
    AdminOrderService.mark_as_paid(o1.id, payment_method='PAYME')
    AdminOrderService.mark_as_paid(o2.id, payment_method='HUMO')
    AdminOrderService.mark_as_paid(o3.id, payments=[
        {'method': 'HUMO', 'amount': '35000'}, {'method': 'UZCARD', 'amount': '18000'}])

    revenue = Order.objects.filter(cashier=c, is_paid=True).exclude(
        status='CANCELED').aggregate(s=Sum('total_amount'))['s']
    t = expected_payment_totals(s)
    assert sum(t.values()) == revenue == D('213000')
    assert t['CASH'] == D('0')
    assert t['PAYME'] == D('100000')
    assert t['HUMO'] == D('95000')
    assert t['UZCARD'] == D('18000')


@pytest.mark.parametrize('raw_body', [b'', b'{'])
def test_admin_pay_endpoint_rejects_missing_or_malformed_json(
    raw_body, admin_user,
):
    from base.models import OrderPayment

    order = _unpaid_order(admin_user, 10)
    response = Client().post(
        f'/api/admins/orders/{order.id}/pay',
        data=raw_body,
        content_type='application/json',
        **_auth(admin_user),
    )

    assert response.status_code == 400
    order.refresh_from_db()
    assert order.is_paid is False
    assert not OrderPayment.objects.filter(order=order).exists()


@pytest.mark.parametrize(
    'payload',
    [
        {},
        {
            'payment_method': 'HUMO',
            'payments': [{'method': 'UZCARD', 'amount': 10}],
        },
        {'payment_method': 'HUMO', 'payments': []},
        {'payments': None},
        {'payments': {}},
        {'payments': 'HUMO'},
        {'payments': []},
        {'payments': [None]},
        {'payments': ['HUMO']},
    ],
)
def test_admin_pay_rejects_missing_malformed_or_contradictory_shape(
    payload, admin_user,
):
    from base.models import OrderPayment

    order = _unpaid_order(admin_user, 10)
    response = _post_payment(Client(), order, _auth(admin_user), payload)

    assert response.status_code == 422, response.content
    order.refresh_from_db()
    assert order.is_paid is False
    assert order.payment_method is None
    assert not OrderPayment.objects.filter(order=order).exists()


def test_admin_pay_accepts_smart_pos_dual_shape_using_structured_tenders(
    admin_user,
):
    from base.models import OrderPayment

    order = _unpaid_order(admin_user, 10)
    response = _post_payment(
        Client(),
        order,
        _auth(admin_user),
        {
            'payments': [
                {'method': 'HUMO', 'amount': 6},
                {'method': 'UZCARD', 'amount': 4},
            ],
            'payment_method': 'HUMO',
            'discount_percent': 0,
        },
    )

    assert response.status_code == 200, response.content
    order.refresh_from_db()
    assert order.payment_method == 'MIXED'
    assert list(
        OrderPayment.objects.filter(order=order)
        .order_by('line_index')
        .values_list('method', 'amount')
    ) == [
        ('HUMO', D('6.00')),
        ('UZCARD', D('4.00')),
    ]


def test_admin_pay_zero_total_dual_shape_creates_no_tender(admin_user):
    from base.models import OrderPayment

    order = _unpaid_order(admin_user, 10)
    response = _post_payment(
        Client(),
        order,
        _auth(admin_user),
        {
            'payments': [],
            'payment_method': 'CASH',
            'discount_percent': 100,
        },
    )

    assert response.status_code == 200, response.content
    order.refresh_from_db()
    assert order.total_amount == D('0.00')
    assert order.payment_method is None
    assert order.payment_action_id is not None
    assert response.json()['data']['payments'] == []
    assert not OrderPayment.objects.filter(order=order).exists()


@pytest.mark.parametrize(
    'amount',
    [
        'NaN', 'Infinity', '-Infinity', -1, 0, '0.001',
        '99999999.991', '100000000',
    ],
)
def test_admin_pay_rejects_nonfinite_nonpositive_or_unstorable_amount(
    amount,
):
    from base.models import OrderPayment

    cashier = _cashier()
    order = _unpaid_order(cashier, 10)
    result, status = AdminOrderService.mark_as_paid(
        order.id,
        payments=[{'method': 'HUMO', 'amount': amount}],
    )

    assert status == 422, result
    order.refresh_from_db()
    assert order.is_paid is False
    assert not OrderPayment.objects.filter(order=order).exists()


@pytest.mark.parametrize(
    'discount',
    ['NaN', 'Infinity', '-Infinity', -1, 101, '1.001', None, ''],
)
def test_admin_pay_rejects_invalid_discount(discount):
    cashier = _cashier()
    order = _unpaid_order(cashier, 10)
    result, status = AdminOrderService.mark_as_paid(
        order.id,
        payment_method='HUMO',
        discount_percent=discount,
    )

    assert status == 422, result
    order.refresh_from_db()
    assert order.is_paid is False


def test_admin_pay_returns_canonical_checkout_evidence(admin_user):
    from base.models import OrderPayment

    order = _unpaid_order(admin_user, 10)
    shift = order.cashier.shifts.get(status='ACTIVE')
    response = _post_payment(
        Client(),
        order,
        _auth(admin_user),
        {'payment_method': '  humo  '},
        key=f'canonical-{uuid4()}',
    )

    assert response.status_code == 200, response.content
    data = response.json()['data']
    order.refresh_from_db()
    payment = OrderPayment.objects.get(order=order)
    assert data == {
        'is_paid': True,
        'order_id': order.id,
        'order_uuid': str(order.uuid),
        'payment_action_id': str(order.payment_action_id),
        'shift_id': shift.id,
        'shift_uuid': str(shift.uuid),
        'paid_at': order.paid_at.isoformat(),
        'payment_method': 'HUMO',
        'discount_percent': '0.00',
        'discount_amount': '0.00',
        'total_amount': '10.00',
        'payments': [
            {'line_index': 0, 'method': 'HUMO', 'amount': '10.00'},
        ],
    }
    assert payment.payment_action_id == order.payment_action_id


def test_headerless_admin_pay_exact_retry_is_safe_and_byte_stable(admin_user):
    from base.models import OrderPayment

    order = _unpaid_order(admin_user, 10)
    client = Client()
    auth = _auth(admin_user)
    payload = json.dumps({'payment_method': 'PAYME'})

    first = client.post(
        f'/api/admins/orders/{order.id}/pay',
        data=payload,
        content_type='application/json',
        **auth,
    )
    replay = client.post(
        f'/api/admins/orders/{order.id}/pay',
        data=payload,
        content_type='application/json',
        **auth,
    )

    assert first.status_code == replay.status_code == 200
    assert first.content == replay.content
    assert OrderPayment.objects.filter(order=order).count() == 1


@pytest.mark.parametrize('cache_loss', ['delete', 'inflight'])
def test_admin_pay_recovers_commit_response_cache_loss(
    cache_loss, admin_user,
):
    from base.models import IdempotencyKey, OrderPayment

    order = _unpaid_order(admin_user, 10)
    client = Client()
    auth = _auth(admin_user)
    key = f'admin-checkout-crash-{uuid4()}'
    payload = json.dumps({'payment_method': 'HUMO'})

    first = client.post(
        f'/api/admins/orders/{order.id}/pay',
        data=payload,
        content_type='application/json',
        HTTP_IDEMPOTENCY_KEY=key,
        **auth,
    )
    assert first.status_code == 200, first.content
    claim = IdempotencyKey.objects.get(key=key)
    assert claim.response_status == 200

    if cache_loss == 'delete':
        claim.delete()
    else:
        IdempotencyKey.objects.filter(pk=claim.pk).update(
            response_status=0,
            response_body={},
            created_at=timezone.now() - timedelta(seconds=6),
        )

    recovered = client.post(
        f'/api/admins/orders/{order.id}/pay',
        data=payload,
        content_type='application/json',
        HTTP_IDEMPOTENCY_KEY=key,
        **auth,
    )

    assert recovered.status_code == 200, recovered.content
    assert recovered.content == first.content
    order.refresh_from_db()
    assert OrderPayment.objects.filter(order=order).count() == 1
    assert OrderPayment.objects.get(order=order).payment_action_id == (
        order.payment_action_id
    )
    recovered_claim = IdempotencyKey.objects.get(key=key)
    assert recovered_claim.response_status == 200
    assert recovered_claim.response_body == first.json()


def test_admin_pay_same_action_replays_evidence_but_rejects_conflict():
    from base.models import OrderPayment

    cashier = _cashier()
    order = _unpaid_order(cashier, 10)
    action_id = uuid4()
    request = {
        'payment_method': 'HUMO',
        'payment_action_id': action_id,
    }

    first, first_status = AdminOrderService.mark_as_paid(order.id, **request)
    replay, replay_status = AdminOrderService.mark_as_paid(order.id, **request)
    conflict, conflict_status = AdminOrderService.mark_as_paid(
        order.id,
        payment_method='UZCARD',
        payment_action_id=action_id,
    )

    assert first_status == replay_status == 200
    assert first == replay
    assert conflict_status == 409, conflict
    assert OrderPayment.objects.filter(order=order).count() == 1


def test_admin_pay_zero_total_action_replays_without_tender_evidence():
    from base.models import OrderPayment

    cashier = _cashier()
    order = _unpaid_order(cashier, 10)
    action_id = uuid4()
    request = {
        'payment_method': 'CASH',
        'discount_percent': 100,
        'payment_action_id': action_id,
    }

    first, first_status = AdminOrderService.mark_as_paid(order.id, **request)
    replay, replay_status = AdminOrderService.mark_as_paid(order.id, **request)

    assert first_status == replay_status == 200
    assert first == replay
    assert first['data']['payment_action_id'] == str(action_id)
    assert first['data']['payment_method'] is None
    assert first['data']['payments'] == []
    order.refresh_from_db()
    assert order.total_amount == D('0.00')
    assert order.payment_action_id == action_id
    assert not OrderPayment.objects.filter(order=order).exists()


@override_settings(DEPLOYMENT_MODE='cloud')
def test_cloud_admin_pay_keeps_physical_cash_guard():
    from base.models import OrderPayment

    cashier = _cashier()
    order = _unpaid_order(cashier, 10)
    result, status = AdminOrderService.mark_as_paid(
        order.id,
        payment_method='CASH',
    )

    assert status == 400, result
    assert 'owning branch desktop' in result['message']
    order.refresh_from_db()
    assert order.is_paid is False
    assert not OrderPayment.objects.filter(order=order).exists()
