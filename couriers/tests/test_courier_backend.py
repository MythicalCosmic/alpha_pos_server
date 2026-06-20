"""Tests for the courier backend additions: payments (record-only), the
settlement/reconciliation ledger, persisted notifications, GPS-trail distance,
share-location, logout and the payment webhook seam.

Realtime sends are no-ops here (no channel layer) — the funnel swallows a
missing layer, so these assert DB/state, which is what matters.
"""
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from base.models import User, Order, Session
from base.repositories.session import SessionRepository
from base.security.hashing import hash_password
from couriers import services, payments, geo
from couriers.models import (
    Courier, DeliveryAssignment, CourierPayment, CourierSettlement,
    CourierNotification, LocationTrailPoint,
)

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _courier(code='CR-1', phone='+99890000000'):
    user = User.objects.create(email=f'{code}@t.local', first_name='A', last_name='B',
                               role='CASHIER', status='ACTIVE',
                               password=hash_password('x'))
    return Courier.objects.create(user=user, code=code, phone=phone, branch_id='cloud')


def _order(courier, step='ON_WAY', total='100000', fee=5000, **extra):
    order = Order.objects.create(user=courier.user, order_type='DELIVERY',
                                 status='PREPARING', branch_id='cloud',
                                 total_amount=Decimal(total))
    a = DeliveryAssignment.objects.create(order=order, courier=courier, step=step,
                                          fee=fee, assigned_at=timezone.now(), **extra)
    return order, a


def _token_for(courier, days=1):
    token = 'tok-' + courier.code
    SessionRepository.create(
        user_id=courier.user, ip_address='1.1.1.1', user_agent='t',
        payload=SessionRepository.hash_token(token),
        expires_at=timezone.now() + timedelta(days=days),
    )
    return token


def _post(client, url, body=None, token=None):
    kw = {'content_type': 'application/json'}
    if token:
        kw['HTTP_AUTHORIZATION'] = f'Token {token}'
    return client.post(url, data=json.dumps(body or {}), **kw)


# --------------------------------------------------------------------------- #
# payments
# --------------------------------------------------------------------------- #
def test_cash_payment_marks_order_paid_and_records():
    c = _courier()
    order, _ = _order(c)
    payment, err = payments.create_payment(c, order, 'CASH')
    assert err is None
    assert payment.status == CourierPayment.Status.PAID
    assert payment.amount == 100000 and payment.paid_at is not None
    order.refresh_from_db()
    assert order.is_paid and order.payment_method == 'CASH' and order.paid_at


def test_refund_reverses_order_paid_state():
    c = _courier()
    order, _ = _order(c)
    payment, _ = payments.create_payment(c, order, 'CASH')
    refunded, err = payments.refund_payment(payment)
    assert err is None and refunded.status == CourierPayment.Status.REFUNDED
    order.refresh_from_db()
    assert not order.is_paid and order.payment_method is None and order.paid_at is None
    # second refund is idempotent (no error, stays refunded)
    again, err = payments.refund_payment(refunded)
    assert err is None and again.status == CourierPayment.Status.REFUNDED


def test_partial_then_completing_payment_sets_mixed():
    c = _courier()
    order, _ = _order(c, total='100000')
    payments.create_payment(c, order, 'CASH', amount=40000)
    order.refresh_from_db()
    assert not order.is_paid                      # 40k < 100k due
    payments.create_payment(c, order, 'QR', amount=60000)
    order.refresh_from_db()
    assert order.is_paid and order.payment_method == 'MIXED'


def test_invalid_provider_and_amount_rejected():
    c = _courier()
    order, _ = _order(c)
    assert payments.create_payment(c, order, 'BTC')[1]
    assert payments.create_payment(c, order, 'CASH', amount=-5)[1]
    assert payments.create_payment(c, order, 'CASH', amount='abc')[1]


def test_webhook_confirms_pending_payment():
    c = _courier()
    order, _ = _order(c)
    pending = CourierPayment.objects.create(
        order=order, courier=c, provider='QR', amount=100000,
        status=CourierPayment.Status.PENDING, external_id='ext-1', branch_id='cloud')
    payment, err = payments.apply_webhook(external_id='ext-1', status='paid')
    assert err is None and payment.id == pending.id
    payment.refresh_from_db()
    assert payment.status == CourierPayment.Status.PAID
    order.refresh_from_db()
    assert order.is_paid and order.payment_method == 'PAYME'


# --------------------------------------------------------------------------- #
# reconciliation + settlement ledger
# --------------------------------------------------------------------------- #
def test_reconciliation_counts_fees_and_cash():
    c = _courier()
    order, a = _order(c, step='ON_WAY', fee=7000)
    services.advance_status(a, 'DELIVERED')        # sets delivered_at, closes order
    payments.create_payment(c, order, 'CASH')
    snap = services.reconciliation_snapshot(c)
    assert snap['deliveries'] == 1
    assert snap['delivery_fees'] == 7000
    assert snap['cash_collected'] == 100000
    assert snap['net_payout'] == 7000             # fees + bonuses + tips
    assert snap['cash_in_hand'] == 100000


def test_settle_freezes_snapshot_and_resets_window():
    c = _courier()
    order, a = _order(c, step='ON_WAY', fee=7000)
    services.advance_status(a, 'DELIVERED')
    payments.create_payment(c, order, 'CASH')

    settlement = services.settle(c, bonuses=1000, tips=500)
    assert isinstance(settlement, CourierSettlement)
    assert settlement.cash_collected == 100000 and settlement.delivery_fees == 7000
    assert settlement.net_payout == 7000 + 1000 + 500

    # everything up to the settlement is now settled -> next window is empty
    snap = services.reconciliation_snapshot(c)
    assert snap['cash_collected'] == 0 and snap['delivery_fees'] == 0


def test_qr_and_card_fold_into_noncash():
    c = _courier()
    o1, _ = _order(c)
    o2, _ = _order(c)
    payments.create_payment(c, o1, 'QR')
    payments.create_payment(c, o2, 'CARD')
    snap = services.reconciliation_snapshot(c)
    assert snap['cash_collected'] == 0
    assert snap['qr_collected'] == 200000 and snap['qr_orders'] == 2


# --------------------------------------------------------------------------- #
# notifications
# --------------------------------------------------------------------------- #
def test_assign_and_ready_persist_notifications():
    c = _courier()
    order = Order.objects.create(user=c.user, order_type='DELIVERY',
                                 status='PREPARING', branch_id='cloud',
                                 total_amount=Decimal('50000'))
    services.assign(order, c, fee=5000)
    assert CourierNotification.objects.filter(
        courier=c, order=order, title__icontains='New order').exists()

    services.mark_ready(order)
    assert CourierNotification.objects.filter(
        courier=c, order=order, title__icontains='ready').exists()


def test_notifications_read_marks_and_counts():
    c = _courier()
    token = _token_for(c)
    cl = Client()
    services.notify(c, title='one')
    services.notify(c, title='two')
    assert CourierNotification.objects.filter(courier=c, read_at__isnull=True).count() == 2

    resp = _post(cl, '/courier/notifications/read/', {}, token)
    assert resp.status_code == 200 and resp.json()['unread'] == 0
    assert CourierNotification.objects.filter(courier=c, read_at__isnull=True).count() == 0


# --------------------------------------------------------------------------- #
# distance (GPS trail)
# --------------------------------------------------------------------------- #
def test_haversine_one_degree_lat_is_about_111km():
    d = geo.haversine_km(0.0, 0.0, 1.0, 0.0)
    assert 110.0 < d < 112.0


def test_trail_distance_sums_small_segments_and_skips_jumps():
    class P:
        def __init__(self, lat, lng):
            self.lat, self.lng = lat, lng
    # ~111 m per 0.001 deg lat; three small hops then one huge GPS jump (skipped)
    pts = [P(41.000, 69.0), P(41.001, 69.0), P(41.002, 69.0),
           P(50.000, 69.0)]
    km = geo.trail_distance_km(pts)
    assert 0.2 < km < 0.3            # ~0.222 km, jump excluded


def test_shift_distance_uses_trail():
    c = _courier()
    c.online = True
    c.shift_started_at = timezone.now() - timedelta(hours=1)
    c.save()
    services.update_location(c, 41.000, 69.000)
    services.update_location(c, 41.001, 69.000)
    assert LocationTrailPoint.objects.filter(courier=c).count() == 2
    assert services.shift_distance_km(c) > 0


# --------------------------------------------------------------------------- #
# share-location, logout (view layer)
# --------------------------------------------------------------------------- #
def test_share_location_toggle():
    c = _courier()
    token = _token_for(c)
    cl = Client()
    resp = _post(cl, '/courier/shift/share-location/', {'share': False}, token)
    assert resp.status_code == 200 and resp.json()['shareLocation'] is False
    c.refresh_from_db()
    assert c.share_loc is False


def test_logout_invalidates_session():
    c = _courier()
    token = _token_for(c)
    cl = Client()
    # works before logout
    assert cl.get('/courier/me/', HTTP_AUTHORIZATION=f'Token {token}').status_code == 200
    resp = _post(cl, '/auth/courier/logout/', {}, token)
    assert resp.status_code == 200
    assert not Session.objects.filter(
        payload=SessionRepository.hash_token(token)).exists()
    # token no longer authenticates
    assert cl.get('/courier/me/', HTTP_AUTHORIZATION=f'Token {token}').status_code == 401


# --------------------------------------------------------------------------- #
# payment endpoints (view layer)
# --------------------------------------------------------------------------- #
def test_payment_create_endpoint_requires_ownership():
    mine = _courier('CR-1')
    other = _courier('CR-2', phone='+99890000001')
    order, _ = _order(other)                # assigned to the OTHER courier
    token = _token_for(mine)
    cl = Client()
    resp = _post(cl, '/payments/create/',
                 {'order_id': order.id, 'provider': 'CASH'}, token)
    assert resp.status_code == 403


def test_payment_create_endpoint_happy_path():
    c = _courier()
    order, _ = _order(c)
    token = _token_for(c)
    cl = Client()
    resp = _post(cl, '/payments/create/',
                 {'order_id': order.id, 'provider': 'CASH'}, token)
    assert resp.status_code == 200
    body = resp.json()
    assert body['status'] == 'PAID' and body['payment_id']
    order.refresh_from_db()
    assert order.is_paid


def test_webhook_disabled_without_secret(settings):
    settings.COURIER_PAYMENT_WEBHOOK_SECRET = ''
    cl = Client()
    resp = _post(cl, '/payments/webhook/', {'status': 'paid'})
    assert resp.status_code == 503


def test_webhook_rejects_bad_secret(settings):
    settings.COURIER_PAYMENT_WEBHOOK_SECRET = 'topsecret'
    cl = Client()
    resp = cl.post('/payments/webhook/', data=json.dumps({'status': 'paid'}),
                   content_type='application/json',
                   HTTP_X_WEBHOOK_SECRET='wrong')
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# review-fix regressions
# --------------------------------------------------------------------------- #
def test_ws_session_user_rejects_expired_token():
    from couriers.consumers import _session_user
    c = _courier()
    good = 'good-tok'
    SessionRepository.create(
        user_id=c.user, ip_address='1.1.1.1', user_agent='t',
        payload=SessionRepository.hash_token(good),
        expires_at=timezone.now() + timedelta(days=1))
    assert _session_user(good) == c.user           # valid token resolves

    expired = 'expired-tok'
    SessionRepository.create(
        user_id=c.user, ip_address='1.1.1.1', user_agent='t',
        payload=SessionRepository.hash_token(expired),
        expires_at=timezone.now() - timedelta(seconds=1))
    assert _session_user(expired) is None          # expired token rejected (WS parity)


def test_unsettled_window_survives_offline_toggle():
    c = _courier()
    c.online = True
    c.shift_started_at = timezone.now()
    c.save()
    order, _ = _order(c)
    payments.create_payment(c, order, 'CASH')
    assert services.reconciliation_snapshot(c)['cash_collected'] == 100000
    # going offline nulls shift_started_at but must NOT move the money window
    services.set_online(c, False)
    c.refresh_from_db()
    assert services.reconciliation_snapshot(c)['cash_collected'] == 100000


def test_prune_trail_removes_old_points():
    c = _courier()
    old = LocationTrailPoint.objects.create(courier=c, lat=1.0, lng=1.0)
    LocationTrailPoint.objects.filter(pk=old.pk).update(
        at=timezone.now() - timedelta(days=30))           # backdate (bypass auto_now_add)
    LocationTrailPoint.objects.create(courier=c, lat=2.0, lng=2.0)   # recent
    deleted = services.prune_trail(c, days=7)
    assert deleted == 1
    assert LocationTrailPoint.objects.filter(courier=c).count() == 1


def test_stats_cash_reflects_payments_not_unpaid_orders():
    c = _courier()
    token = _token_for(c)
    order, a = _order(c, step='ON_WAY')
    services.advance_status(a, 'DELIVERED')               # delivered, but unpaid
    cl = Client()
    r1 = cl.get('/courier/stats/today/', HTTP_AUTHORIZATION=f'Token {token}')
    assert r1.json()['cashCollected'] == 0               # not the unpaid order total
    payments.create_payment(c, order, 'CASH')
    r2 = cl.get('/courier/stats/today/', HTTP_AUTHORIZATION=f'Token {token}')
    assert r2.json()['cashCollected'] == 100000
