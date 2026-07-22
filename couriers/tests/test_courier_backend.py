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

from base.models import (
    CashRegister, ExternalOrderPayment, Order, OrderPayment, OrderRefund,
    Session, User,
)
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
def _courier(code='CR-1', phone='+998900000000'):
    user = User.objects.create(email=f'{code}@t.local', first_name='A', last_name='B',
                               role='COURIER', status='ACTIVE',
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


def test_cash_payment_creates_external_evidence_without_touching_drawer():
    c = _courier()
    order, _ = _order(c)

    payment, err = payments.create_payment(
        c, order, 'CASH', external_id='cash-external-evidence',
    )

    assert err is None
    evidence = ExternalOrderPayment.objects.get(
        source=ExternalOrderPayment.Source.COURIER,
        source_id=payment.external_id,
    )
    assert evidence.order_id == order.pk
    assert evidence.branch_id == order.branch_id
    assert evidence.method == Order.PaymentMethod.CASH
    assert evidence.amount == Decimal('100000')
    assert evidence.occurred_at == payment.paid_at
    assert evidence.affects_drawer is False
    assert not OrderPayment.objects.filter(order=order).exists()
    assert CashRegister.objects.get(
        branch_id=order.branch_id,
    ).current_balance == Decimal('0')


def test_exact_retry_repairs_missing_external_evidence_without_duplicate_collection():
    c = _courier()
    order, _ = _order(c)
    paid_at = timezone.now() - timedelta(minutes=5)
    payment = CourierPayment.objects.create(
        order=order,
        courier=c,
        provider=CourierPayment.Provider.QR,
        amount=100000,
        status=CourierPayment.Status.PAID,
        external_id='repair-missing-external-evidence',
        branch_id=order.branch_id,
        paid_at=paid_at,
    )

    replay, err = payments.create_payment(
        c, order, 'QR', external_id='repair-missing-external-evidence',
    )

    assert err is None and replay.pk == payment.pk
    assert CourierPayment.objects.filter(external_id=payment.external_id).count() == 1
    assert ExternalOrderPayment.objects.filter(
        source=ExternalOrderPayment.Source.COURIER,
        source_id=payment.external_id,
    ).count() == 1


def test_synced_partial_external_evidence_blocks_overcollection():
    c = _courier()
    order, _ = _order(c, total='100000')
    ExternalOrderPayment.objects.create(
        order=order,
        source=ExternalOrderPayment.Source.COURIER,
        source_id='other-edition-partial',
        method=Order.PaymentMethod.PAYME,
        amount=Decimal('40000'),
        occurred_at=timezone.now(),
        branch_id=order.branch_id,
    )

    rejected, err = payments.create_payment(
        c, order, 'CASH', amount=70000,
        external_id='would-overcollect-cross-edition',
    )

    assert rejected is None
    assert 'remaining order amount (60000' in err
    assert not CourierPayment.objects.filter(
        external_id='would-overcollect-cross-edition',
    ).exists()

    accepted, err = payments.create_payment(
        c, order, 'CASH', amount=60000,
        external_id='complete-cross-edition',
    )
    assert err is None and accepted is not None
    order.refresh_from_db()
    assert order.is_paid is True
    assert order.payment_method == Order.PaymentMethod.MIXED


def test_external_evidence_conflict_rolls_back_new_collection():
    c = _courier()
    first_order, _ = _order(c)
    second_order, _ = _order(c)
    occurred_at = timezone.now()
    ExternalOrderPayment.objects.create(
        order=first_order,
        source=ExternalOrderPayment.Source.COURIER,
        source_id='conflicting-external-evidence',
        method=Order.PaymentMethod.PAYME,
        amount=Decimal('100000'),
        occurred_at=occurred_at,
        branch_id=first_order.branch_id,
    )

    payment, err = payments.create_payment(
        c, second_order, 'QR', external_id='conflicting-external-evidence',
    )

    assert payment is None
    assert 'conflicts on order_id' in err
    assert not CourierPayment.objects.filter(
        external_id='conflicting-external-evidence',
    ).exists()
    second_order.refresh_from_db()
    assert second_order.is_paid is False
    assert ExternalOrderPayment.objects.filter(
        source_id='conflicting-external-evidence',
    ).count() == 1


def test_refund_appends_ledger_and_preserves_paid_evidence():
    c = _courier()
    order, _ = _order(c)
    payment, _ = payments.create_payment(c, order, 'CASH')
    order.refresh_from_db()
    original_paid_at = order.paid_at
    refunded, err = payments.refund_payment(payment)
    assert err is None and refunded.status == CourierPayment.Status.REFUNDED
    order.refresh_from_db()
    payment.refresh_from_db()
    assert order.is_paid and order.payment_method == 'CASH'
    assert order.paid_at == original_paid_at
    assert payment.status == CourierPayment.Status.PAID
    assert payment.refunded_at is None
    event = OrderRefund.objects.get(order=order)
    assert event.source == OrderRefund.Source.COURIER_PAYMENT
    assert event.amount == Decimal('100000')
    assert event.cash_amount == Decimal('100000')
    assert event.drawer_cash_amount == Decimal('0')
    evidence = ExternalOrderPayment.objects.get(source_id=payment.external_id)
    evidence_pk = evidence.pk
    # A replay returns legacy wire status but does not append/debit twice.
    again, err = payments.refund_payment(refunded)
    assert err is None and again.status == CourierPayment.Status.REFUNDED
    assert OrderRefund.objects.filter(order=order).count() == 1
    assert ExternalOrderPayment.objects.get(
        source_id=payment.external_id,
    ).pk == evidence_pk


def test_partial_then_completing_payment_sets_mixed():
    c = _courier()
    order, _ = _order(c, total='100000')
    payments.create_payment(c, order, 'CASH', amount=40000)
    order.refresh_from_db()
    assert not order.is_paid                      # 40k < 100k due
    payments.create_payment(c, order, 'QR', amount=60000)
    order.refresh_from_db()
    assert order.is_paid and order.payment_method == 'MIXED'


def test_split_provider_refunds_are_multi_event_and_never_touch_drawer():
    from base.models import CashRegister
    from base.services.tender import net_breakdown

    c = _courier()
    order, _ = _order(c, total='100000')
    cash, err = payments.create_payment(
        c, order, 'CASH', amount=40000, external_id='split-cash-refund',
    )
    assert err is None
    qr, err = payments.create_payment(
        c, order, 'QR', amount=60000, external_id='split-qr-refund',
    )
    assert err is None

    first, err = payments.refund_payment(cash)
    assert err is None and first.status == CourierPayment.Status.REFUNDED
    second, err = payments.refund_payment(qr)
    assert err is None and second.status == CourierPayment.Status.REFUNDED

    events = OrderRefund.objects.filter(order=order).order_by('refunded_at')
    assert events.count() == 2
    assert sum((event.amount for event in events), Decimal('0')) == Decimal('100000')
    assert sum((event.drawer_cash_amount for event in events), Decimal('0')) == 0
    # The register row is also the branch accounting serialization lock.  A
    # courier event may create that row, but it must never move drawer cash.
    register = CashRegister.objects.get(branch_id=order.branch_id)
    assert register.current_balance == Decimal('0')
    order.refresh_from_db()
    assert order.is_paid and order.payment_method == Order.PaymentMethod.MIXED
    assert CourierPayment.objects.filter(
        order=order, status=CourierPayment.Status.PAID,
    ).count() == 2
    split, _detail = net_breakdown(Order.objects.filter(pk=order.pk), events)
    assert sum(split.values(), Decimal('0')) == 0


def test_blank_external_id_retry_reuses_one_durable_event():
    c = _courier()
    order, _ = _order(c)

    first, err = payments.create_payment(c, order, 'CASH')
    assert err is None
    replay, err = payments.create_payment(c, order, 'CASH')

    assert err is None
    assert replay.pk == first.pk
    assert first.external_id.startswith('manual:')
    assert CourierPayment.objects.filter(order=order).count() == 1


def test_blank_external_id_retry_does_not_resurrect_refunded_collection():
    c = _courier()
    order, _ = _order(c)
    payment, _ = payments.create_payment(c, order, 'CASH')
    payment, err = payments.refund_payment(payment)
    assert err is None

    replay, err = payments.create_payment(c, order, 'CASH')

    assert err is None
    assert replay.pk == payment.pk
    assert replay.status == CourierPayment.Status.REFUNDED
    assert CourierPayment.objects.filter(order=order).count() == 1
    order.refresh_from_db()
    assert order.is_paid is True
    assert OrderRefund.objects.filter(order=order).count() == 1


def test_courier_payment_aggregate_cannot_exceed_order_total():
    c = _courier()
    order, _ = _order(c, total='100000')
    first, err = payments.create_payment(c, order, 'CASH', amount=60000)
    assert err is None

    rejected, err = payments.create_payment(c, order, 'QR', amount=40001)

    assert rejected is None
    assert 'exceeds remaining order amount' in err
    assert CourierPayment.objects.filter(order=order).count() == 1
    first.refresh_from_db()
    assert first.status == CourierPayment.Status.PAID
    order.refresh_from_db()
    assert order.is_paid is False


def test_distinct_ids_allow_intentional_equal_split_without_overpayment():
    c = _courier()
    order, _ = _order(c, total='100000')

    first, err = payments.create_payment(
        c, order, 'CASH', amount=50000, external_id='split-part-1',
    )
    assert err is None
    second, err = payments.create_payment(
        c, order, 'CASH', amount=50000, external_id='split-part-2',
    )

    assert err is None
    assert first.pk != second.pk
    order.refresh_from_db()
    assert order.is_paid is True
    assert order.payment_method == 'CASH'


def test_till_and_courier_evidence_recompute_and_report_as_one_split():
    from base.services.tender import order_tender_split

    c = _courier()
    order, _ = _order(c, total='100000')
    OrderPayment.objects.create(
        order=order, method=Order.PaymentMethod.CASH, amount=40000,
        branch_id=order.branch_id,
    )

    payment, err = payments.create_payment(c, order, 'QR', amount=60000)

    assert err is None
    assert payment.status == CourierPayment.Status.PAID
    order.refresh_from_db()
    assert order.is_paid is True
    assert order.payment_method == Order.PaymentMethod.MIXED
    split, _detail = order_tender_split(order)
    assert split['cash'] == Decimal('40000')
    assert split['payme'] == Decimal('60000')
    assert sum(split.values()) == order.total_amount


def test_drawer_cash_excludes_all_courier_collection_even_when_till_tender_is_large():
    from base.services.tender import order_tender_sources

    c = _courier()
    order, _ = _order(c, total='100000')
    OrderPayment.objects.create(
        order=order,
        method=Order.PaymentMethod.CASH,
        amount=100000,  # raw tender may include money/change beyond its bill leg
        branch_id=order.branch_id,
    )
    CourierPayment.objects.create(
        order=order,
        courier=c,
        provider=CourierPayment.Provider.CASH,
        amount=50000,
        status=CourierPayment.Status.PAID,
        paid_at=timezone.now(),
        external_id='historical-mixed-cash-sources',
        branch_id=order.branch_id,
    )

    split, _detail, drawer_cash = order_tender_sources(order)

    assert split['cash'] == Decimal('100000')
    assert drawer_cash == Decimal('50000')


def test_refund_keeps_order_paid_when_separate_till_payment_covers_it():
    c = _courier()
    order, _ = _order(c, total='100000')
    OrderPayment.objects.create(
        order=order, method=Order.PaymentMethod.HUMO, amount=100000,
        branch_id=order.branch_id,
    )
    # Historical mixed state from before aggregate validation was introduced.
    courier = CourierPayment.objects.create(
        order=order, courier=c, provider=CourierPayment.Provider.CASH,
        amount=100000, status=CourierPayment.Status.PAID,
        paid_at=timezone.now(), external_id='historical-double-settlement',
        branch_id=order.branch_id,
    )
    Order.objects.filter(pk=order.pk).update(
        is_paid=True, payment_method=Order.PaymentMethod.MIXED,
        paid_at=timezone.now(),
    )

    refunded, err = payments.refund_payment(courier)

    assert err is None
    assert refunded.status == CourierPayment.Status.REFUNDED
    order.refresh_from_db()
    assert order.is_paid is True
    # Both positive settlement events remain frozen; the refund ledger supplies
    # the dated negative courier leg to reports.
    assert order.payment_method == Order.PaymentMethod.MIXED
    courier.refresh_from_db()
    assert courier.status == CourierPayment.Status.PAID
    assert OrderRefund.objects.get(order=order).drawer_cash_amount == 0


def test_webhook_cannot_activate_pending_payment_beyond_remaining_amount():
    c = _courier()
    order, _ = _order(c, total='100000')
    paid, err = payments.create_payment(c, order, 'CASH', amount=70000)
    assert err is None and paid.status == CourierPayment.Status.PAID
    pending = CourierPayment.objects.create(
        order=order, courier=c, provider='QR', amount=30001,
        status=CourierPayment.Status.PENDING, external_id='too-large-pending',
        branch_id=order.branch_id,
    )

    activated, err = payments.apply_webhook(
        external_id=pending.external_id, status='paid',
    )

    assert activated is None
    assert 'exceeds remaining order amount' in err
    pending.refresh_from_db()
    assert pending.status == CourierPayment.Status.PENDING


def test_invalid_provider_and_amount_rejected():
    c = _courier()
    order, _ = _order(c)
    assert payments.create_payment(c, order, 'BTC')[1]
    assert payments.create_payment(c, order, 'CASH', amount=-5)[1]
    assert payments.create_payment(c, order, 'CASH', amount='abc')[1]
    assert payments.create_payment(c, order, 'CASH', amount=1.5)[1]


def test_shiftless_courier_settlement_cannot_bypass_till_for_nondelivery_order():
    c = _courier()
    order, _ = _order(c)
    Order.objects.filter(pk=order.pk).update(order_type=Order.OrderType.HALL)
    order.refresh_from_db()

    payment, err = payments.create_payment(c, order, 'CASH')

    assert payment is None
    assert 'only for delivery orders' in err
    assert not CourierPayment.objects.filter(order=order).exists()


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


def test_webhook_external_id_replay_creates_only_one_money_event():
    c = _courier()
    order, _ = _order(c)

    first, err = payments.apply_webhook(
        external_id='gateway-event-1', status='paid', order_id=order.id,
        provider='QR', amount=100000,
    )
    assert err is None
    replay, err = payments.apply_webhook(
        external_id='gateway-event-1', status='paid', order_id=order.id,
        provider='QR', amount=100000,
    )

    assert err is None
    assert replay.pk == first.pk
    assert CourierPayment.objects.filter(external_id='gateway-event-1').count() == 1


def test_paid_webhook_replay_repairs_legacy_header_with_accounting_cursor():
    from base.models import CashRegister

    c = _courier()
    order, _ = _order(c)
    paid_at = timezone.now() - timedelta(days=2)
    CourierPayment.objects.create(
        order=order,
        courier=c,
        provider='QR',
        amount=order.total_amount,
        status=CourierPayment.Status.PAID,
        external_id='gateway-legacy-paid-replay',
        branch_id=order.branch_id,
        paid_at=paid_at,
    )
    receipt_floor = timezone.now()

    replay, err = payments.apply_webhook(
        external_id='gateway-legacy-paid-replay', status='paid',
    )

    assert err is None
    assert replay.status == CourierPayment.Status.PAID
    order.refresh_from_db()
    assert order.is_paid is True
    assert order.paid_at == paid_at
    assert order.accounting_recorded_at >= receipt_floor
    assert order.accounting_recorded_at > order.paid_at
    assert CashRegister.objects.filter(branch_id=order.branch_id).exists()


def test_gateway_external_id_cannot_be_reused_for_another_order():
    c = _courier()
    first_order, _ = _order(c)
    second_order, _ = _order(c)
    payment, err = payments.create_payment(
        c, first_order, 'QR', external_id='gateway-event-shared',
    )
    assert err is None

    duplicate, err = payments.create_payment(
        c, second_order, 'QR', external_id='gateway-event-shared',
    )

    assert duplicate is None
    assert 'another order' in err
    assert CourierPayment.objects.filter(external_id='gateway-event-shared').count() == 1
    second_order.refresh_from_db()
    assert second_order.is_paid is False


def test_refunded_external_event_cannot_be_resurrected_by_late_paid_replay():
    c = _courier()
    order, _ = _order(c)
    payment, err = payments.create_payment(
        c, order, 'QR', external_id='gateway-refunded-event',
    )
    assert err is None
    payment, err = payments.refund_payment(payment)
    assert err is None
    refunded_at = payment.refunded_at

    replay, err = payments.apply_webhook(
        external_id='gateway-refunded-event', status='paid',
        order_id=order.id, provider='QR', amount=100000,
    )
    assert err is None
    assert replay.status == CourierPayment.Status.REFUNDED
    assert replay.refunded_at == refunded_at

    direct_replay, err = payments.create_payment(
        c, order, 'QR', external_id='gateway-refunded-event',
    )
    assert err is None
    assert direct_replay.status == CourierPayment.Status.REFUNDED
    assert direct_replay.refunded_at == refunded_at
    order.refresh_from_db()
    payment.refresh_from_db()
    assert order.is_paid is True
    assert order.paid_at is not None
    assert payment.status == CourierPayment.Status.PAID
    assert payment.refunded_at is None
    assert OrderRefund.objects.filter(order=order).count() == 1


def test_external_event_replay_rejects_changed_money_fields():
    c = _courier()
    order, _ = _order(c)
    payment, err = payments.create_payment(
        c, order, 'QR', amount=100000, external_id='gateway-fixed-identity',
    )
    assert err is None

    replay, err = payments.create_payment(
        c, order, 'QR', amount=90000, external_id='gateway-fixed-identity',
    )

    assert replay is None
    assert 'different amount' in err
    payment.refresh_from_db()
    assert payment.amount == 100000


def test_terminal_webhook_states_ignore_late_conflicting_callbacks():
    c = _courier()
    paid_order, _ = _order(c)
    paid = CourierPayment.objects.create(
        order=paid_order, courier=c, provider='QR', amount=100000,
        status=CourierPayment.Status.PENDING, external_id='gateway-paid-first',
        branch_id='cloud',
    )
    paid, err = payments.apply_webhook(
        external_id=paid.external_id, status='paid',
    )
    assert err is None
    replay, err = payments.apply_webhook(
        external_id=paid.external_id, status='failed',
    )
    assert err is None
    assert replay.status == CourierPayment.Status.PAID
    paid_order.refresh_from_db()
    assert paid_order.is_paid is True

    failed_order, _ = _order(c)
    failed = CourierPayment.objects.create(
        order=failed_order, courier=c, provider='QR', amount=100000,
        status=CourierPayment.Status.PENDING, external_id='gateway-failed-first',
        branch_id='cloud',
    )
    failed, err = payments.apply_webhook(
        external_id=failed.external_id, status='failed',
    )
    assert err is None
    replay, err = payments.apply_webhook(
        external_id=failed.external_id, status='paid',
    )
    assert err is None
    assert replay.status == CourierPayment.Status.FAILED
    failed_order.refresh_from_db()
    assert failed_order.is_paid is False


# --------------------------------------------------------------------------- #
# reconciliation + settlement ledger
# --------------------------------------------------------------------------- #
def test_delivered_locks_order_then_assignment_then_courier(monkeypatch):
    c = _courier()
    order, assignment = _order(c, step='ON_WAY')
    calls = []
    assignment_lock_options = []

    real_assignment_lock = DeliveryAssignment.objects.select_for_update
    real_order_lock = Order.objects.select_for_update
    real_courier_lock = services.lock_courier_accounting

    def tracked_assignment_lock(*args, **kwargs):
        calls.append('assignment')
        assignment_lock_options.append(kwargs)
        return real_assignment_lock(*args, **kwargs)

    def tracked_order_lock(*args, **kwargs):
        calls.append('order')
        return real_order_lock(*args, **kwargs)

    def tracked_courier_lock(courier_or_id):
        calls.append('courier')
        return real_courier_lock(courier_or_id)

    monkeypatch.setattr(
        DeliveryAssignment.objects, 'select_for_update',
        tracked_assignment_lock,
    )
    monkeypatch.setattr(
        Order.objects, 'select_for_update', tracked_order_lock,
    )
    monkeypatch.setattr(
        services, 'lock_courier_accounting', tracked_courier_lock,
    )

    delivered, err = services.advance_status(assignment, 'DELIVERED')

    assert err is None and delivered.step == 'DELIVERED'
    assert calls == ['order', 'assignment', 'courier']
    assert assignment_lock_options == [{'of': ('self',)}]
    order.refresh_from_db()
    assert order.status == 'COMPLETED'


def test_mark_ready_scopes_lock_away_from_nullable_courier_join(monkeypatch):
    c = _courier()
    order, assignment = _order(c, step='ASSIGNED')
    lock_options = []
    real_assignment_lock = DeliveryAssignment.objects.select_for_update

    def tracked_assignment_lock(*args, **kwargs):
        lock_options.append(kwargs)
        return real_assignment_lock(*args, **kwargs)

    monkeypatch.setattr(
        DeliveryAssignment.objects, 'select_for_update',
        tracked_assignment_lock,
    )

    services.mark_ready(order)

    assignment.refresh_from_db()
    assert assignment.step == DeliveryAssignment.Step.READY
    assert lock_options == [{'of': ('self',)}]


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


def test_refund_key_falls_back_to_legacy_primary_key():
    payment = CourierPayment(pk=731, external_id='')
    assert services.courier_payment_event_key(payment) == 'legacy-payment:731'
    assert payments._refund_event_id(payment) == 'legacy-payment:731'


def test_refund_and_pending_paid_webhook_take_courier_owner_lock(monkeypatch):
    c = _courier()
    order, _ = _order(c)
    paid, err = payments.create_payment(
        c, order, 'CASH', external_id='lock-refund-event',
    )
    assert err is None

    calls = []
    real_lock = services.lock_courier_accounting

    def tracked_lock(courier_or_id):
        calls.append(getattr(courier_or_id, 'pk', courier_or_id))
        return real_lock(courier_or_id)

    monkeypatch.setattr(services, 'lock_courier_accounting', tracked_lock)
    refunded, err = payments.refund_payment(paid)
    assert err is None and refunded is not None

    pending_order, _ = _order(c)
    pending = CourierPayment.objects.create(
        order=pending_order,
        courier=c,
        provider=CourierPayment.Provider.QR,
        amount=100000,
        status=CourierPayment.Status.PENDING,
        external_id='lock-paid-webhook',
        branch_id='cloud',
    )
    confirmed, err = payments.apply_webhook(
        external_id=pending.external_id, status='paid',
    )
    assert err is None and confirmed.status == CourierPayment.Status.PAID
    assert calls == [c.pk, c.pk]


def test_order_cancel_refunds_only_unreversed_courier_tender():
    c = _courier()
    order, _ = _order(c, total='100000')
    OrderPayment.objects.create(
        order=order, method=Order.PaymentMethod.UZCARD, amount=30000,
        branch_id='cloud',
    )
    cash, err = payments.create_payment(
        c, order, 'CASH', amount=40000, external_id='cancel-mixed-cash',
    )
    assert err is None
    _qr, err = payments.create_payment(
        c, order, 'QR', amount=30000, external_id='cancel-mixed-qr',
    )
    assert err is None
    _refunded, err = payments.refund_payment(cash)
    assert err is None

    cancellation = OrderRefund.objects.create(
        order=order,
        amount=Decimal('60000'),
        cash_amount=Decimal('0'),
        drawer_cash_amount=Decimal('0'),
        card_amount=Decimal('30000'),
        payme_amount=Decimal('30000'),
        unknown_amount=Decimal('0'),
        refunded_at=timezone.now(),
        source=OrderRefund.Source.ORDER_CANCEL,
        source_id=str(order.uuid),
        branch_id='cloud',
    )

    entries = services.courier_refund_entries(c)
    attributed = {entry['source_id']: entry for entry in entries}
    assert attributed[cash.external_id]['amount'] == Decimal('40000')
    assert attributed[cancellation.source_id]['amount'] == Decimal('30000')
    assert attributed[cancellation.source_id]['card_amount'] == 0
    assert attributed[cancellation.source_id]['payme_amount'] == 30000
    snap = services.reconciliation_snapshot(c)
    assert snap['cash_collected'] == 0
    assert snap['qr_collected'] == 0


def test_reconciliation_windows_are_half_open_at_exact_cutoff():
    c = _courier()
    order, _ = _order(c)
    payment, err = payments.create_payment(
        c, order, 'CASH', external_id='boundary-payment',
    )
    assert err is None
    cutoff = timezone.now()
    CourierPayment.objects.filter(pk=payment.pk).update(paid_at=cutoff)

    before = services.reconciliation_snapshot(
        c, start=cutoff - timedelta(seconds=1), end=cutoff,
    )
    after = services.reconciliation_snapshot(
        c, start=cutoff, end=cutoff + timedelta(seconds=1),
    )
    assert before['cash_collected'] == 0
    assert after['cash_collected'] == 100000


def test_legacy_refunded_status_keeps_gross_and_refund_in_same_window():
    c = _courier()
    order, _ = _order(c)
    payment, err = payments.create_payment(
        c, order, 'CASH', external_id='legacy-status-same-window',
    )
    assert err is None
    _refunded, err = payments.refund_payment(payment)
    assert err is None
    CourierPayment.objects.filter(pk=payment.pk).update(
        status=CourierPayment.Status.REFUNDED,
    )

    snap = services.reconciliation_snapshot(c)
    assert snap['cash_collected'] == 0
    assert snap['cash_in_hand'] == 0


def test_late_economic_refund_rolls_into_post_settlement_cursor_window():
    c = _courier()
    order, _ = _order(c)
    payment, err = payments.create_payment(
        c, order, 'CASH', external_id='late-refund-after-handover',
    )
    assert err is None
    settlement = services.settle(c)

    _refunded, err = payments.refund_payment(payment)
    assert err is None
    refund = OrderRefund.objects.get(
        source=OrderRefund.Source.COURIER_PAYMENT,
        source_id=payment.external_id,
    )
    # Simulate a delayed provider event whose economic time predates the closed
    # handover; its local receipt cursor remains in the new window.
    OrderRefund.objects.filter(pk=refund.pk).update(
        refunded_at=settlement.period_end - timedelta(days=1),
    )
    CourierPayment.objects.filter(pk=payment.pk).update(
        status=CourierPayment.Status.REFUNDED,
    )
    refund.refresh_from_db()
    assert refund.accounting_recorded_at >= settlement.period_end

    snap = services.reconciliation_snapshot(c)
    assert snap['cash_collected'] == -100000
    assert snap['cash_in_hand'] == -100000


def test_settlement_locks_order_branch_for_legacy_blank_payment_branch(
    monkeypatch,
):
    c = _courier()
    order, _ = _order(c)
    payment, err = payments.create_payment(
        c, order, 'CASH', external_id='legacy-blank-branch',
    )
    assert err is None
    CourierPayment.objects.filter(pk=payment.pk).update(branch_id='')

    locked = []
    real_lock = services.lock_branch_accounting

    def tracked_lock(branch_id):
        locked.append(branch_id)
        return real_lock(branch_id)

    monkeypatch.setattr(services, 'lock_branch_accounting', tracked_lock)
    services.settle(c)
    assert locked == ['cloud']


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
    other = _courier('CR-2', phone='+998900000001')
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
