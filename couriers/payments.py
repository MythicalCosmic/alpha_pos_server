"""Courier payment recording — the one place a courier collection is written
and the one place ``payment.paid`` / ``payment.refunded`` are emitted.

Record-only by design (the system launches cash-only, no live gateway): the
courier confirms how the customer paid at the door and the payment lands
``PAID`` immediately — recording cash, card-on-terminal or QR the same way. We
update the POS Order's rolled-up paid fields (so the cloud shift/AI reports see
it) but deliberately do NOT write a synced ``base.OrderPayment`` row — those
carry a sync write-denylist that would land blank on the tills.

``apply_webhook`` is the seam for a future online gateway: it confirms/reverses
a payment server-side (never from the client) and fires the same WS events. It
is inert until ``COURIER_PAYMENT_WEBHOOK_SECRET`` is configured.
"""
import logging

from django.db import transaction
from django.utils import timezone

from base.models import Order
from couriers import presenters, realtime, services
from couriers.models import CourierPayment

logger = logging.getLogger('couriers.payments')

_PAID_WORDS = {'paid', 'success', 'succeeded', 'completed', 'confirmed'}
_REFUND_WORDS = {'refund', 'refunded', 'reversed', 'chargeback'}
_FAIL_WORDS = {'fail', 'failed', 'canceled', 'cancelled', 'declined'}


# --------------------------------------------------------------------------- #
# order paid-state (rolled-up onto base.Order, recomputed from PAID rows)
# --------------------------------------------------------------------------- #
def _recompute_order_paid(order):
    """Re-derive Order.is_paid / payment_method / paid_at from this order's PAID
    courier payments. Idempotent — a refund that drops the total below due flips
    the order back to unpaid."""
    rows = list(CourierPayment.objects.filter(
        order=order, status=CourierPayment.Status.PAID))
    total_paid = sum(int(r.amount) for r in rows)
    total_due = presenters.so_m(order.total_amount)
    fields = []
    if total_paid > 0 and total_paid >= total_due:
        methods = {CourierPayment.PROVIDER_TO_METHOD.get(r.provider, 'CASH') for r in rows}
        method = next(iter(methods)) if len(methods) == 1 else 'MIXED'
        if not order.is_paid:
            order.is_paid = True
            fields.append('is_paid')
        if order.payment_method != method:
            order.payment_method = method
            fields.append('payment_method')
        if not order.paid_at:
            order.paid_at = timezone.now()
            fields.append('paid_at')
    else:
        if order.is_paid:
            order.is_paid = False
            fields.append('is_paid')
        if order.payment_method:
            order.payment_method = None
            fields.append('payment_method')
        if order.paid_at:
            order.paid_at = None
            fields.append('paid_at')
    if fields:
        fields.append('updated_at')
        order.save(update_fields=fields)


def _emit(payment, event, order):
    realtime.push_courier_event(
        event,
        courier_id=payment.courier_id,
        branch_id=order.branch_id or None,
        data={
            'order_id': order.id,
            'payment_id': payment.id,
            'provider': payment.provider,
            'amount': int(payment.amount),
            'status': payment.status,
            'is_paid': order.is_paid,
            'at': timezone.now().isoformat(),
        },
    )


def _coerce_amount(amount, order):
    if amount is None or amount == '':
        return presenters.so_m(order.total_amount), None
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return None, 'amount must be an integer (so\'m)'
    if amount <= 0:
        return None, 'amount must be positive'
    return amount, None


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def create_payment(courier, order, provider, *, amount=None, note='', external_id='', link=''):
    """Record a courier-collected payment as PAID and fire ``payment.paid``.
    Returns (payment, None) or (None, error_message)."""
    provider = (provider or '').strip().upper()
    if provider not in CourierPayment.Provider.values:
        return None, 'invalid provider (expected CASH, CARD or QR)'
    amount, err = _coerce_amount(amount, order)
    if err:
        return None, err

    now = timezone.now()
    with transaction.atomic():
        # Lock the order row so concurrent create/refund recomputes serialize
        # (the rolled-up is_paid/payment_method can't be left stale).
        order = Order.objects.select_for_update().get(pk=order.pk)
        payment = CourierPayment.objects.create(
            order=order, courier=courier, provider=provider, amount=amount,
            status=CourierPayment.Status.PAID, paid_at=now,
            branch_id=order.branch_id or '', note=(note or '')[:200],
            external_id=(external_id or '')[:128], link=(link or '')[:512],
        )
        _recompute_order_paid(order)

    _emit(payment, 'payment.paid', order)
    label = dict(CourierPayment.Provider.choices).get(provider, provider)
    services.notify(
        courier, icon='cash' if payment.is_cash else 'creditcard', tone='success',
        title=f'Payment received · order #{order.id}',
        body=f'{presenters.so_m(payment.amount):,} so\'m via {label}.', order=order,
    )
    return payment, None


def refund_payment(payment):
    """Reverse a PAID payment and fire ``payment.refunded``. Idempotent."""
    if payment.status == CourierPayment.Status.REFUNDED:
        return payment, None
    if payment.status != CourierPayment.Status.PAID:
        return None, 'only a paid payment can be refunded'
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=payment.order_id)
        payment.status = CourierPayment.Status.REFUNDED
        payment.refunded_at = timezone.now()
        payment.save(update_fields=['status', 'refunded_at'])
        _recompute_order_paid(order)
    _emit(payment, 'payment.refunded', order)
    return payment, None


def apply_webhook(*, external_id='', payment_id=None, status='', order_id=None,
                  provider='QR', amount=None):
    """Server-side gateway confirmation/reversal (never trusts the client).

    Resolves the payment by id or external_id, drives the status transition and
    fires the matching WS event. For a ``paid`` callback with no existing row but
    an ``order_id``, it creates the PAID record (the online-payment seam).
    Returns (payment, None) or (None, error_message)."""
    status = (status or '').strip().lower()
    payment = None
    if payment_id:
        payment = (CourierPayment.objects.select_related('order', 'courier')
                   .filter(pk=payment_id).first())
    elif external_id:
        payment = (CourierPayment.objects.select_related('order', 'courier')
                   .filter(external_id=external_id).first())

    if status in _PAID_WORDS:
        if payment:
            if payment.status == CourierPayment.Status.PAID:
                return payment, None
            with transaction.atomic():
                order = Order.objects.select_for_update().get(pk=payment.order_id)
                payment.status = CourierPayment.Status.PAID
                payment.paid_at = timezone.now()
                payment.save(update_fields=['status', 'paid_at'])
                _recompute_order_paid(order)
            _emit(payment, 'payment.paid', order)
            return payment, None
        if order_id:
            order = Order.objects.filter(pk=order_id).first()
            if not order:
                return None, 'unknown order'
            courier = getattr(getattr(order, 'courier_delivery', None), 'courier', None)
            return create_payment(courier, order, provider, amount=amount,
                                  external_id=external_id, note='gateway')
        return None, 'unknown payment'

    if status in _REFUND_WORDS:
        if not payment:
            return None, 'unknown payment'
        return refund_payment(payment)

    if status in _FAIL_WORDS:
        if payment and payment.status == CourierPayment.Status.PENDING:
            payment.status = CourierPayment.Status.FAILED
            payment.save(update_fields=['status'])
        return payment, None

    return None, f'unsupported status {status!r}'
