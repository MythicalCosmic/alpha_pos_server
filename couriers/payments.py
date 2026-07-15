"""Courier payment recording — the one place a courier collection is written
and the one place ``payment.paid`` / ``payment.refunded`` are emitted.

Record-only by design (the system launches cash-only, no live gateway): the
courier confirms how the customer paid at the door and the payment lands
``PAID`` immediately — recording cash, card-on-terminal or QR the same way. We
update the POS Order's rolled-up paid fields (so the cloud shift/AI reports see
it) but deliberately do NOT write a synced ``base.OrderPayment`` row — those
carry no such hazard: the old claim that the OrderPayment sync write-denylist
"would land blank on the tills" is FALSE -- SyncMixin._strip_sync_denied(creating=True)
keeps denied fields that are NOT NULL with no default, and ``amount``/``method`` both
qualify, so a CREATE syncs intact. The real reason is that courier cash is collected at
the door and never enters a till drawer, so it must not appear in
``cashbox.drawer.expected_payment_totals``. Reports attribute a courier sale from
``CourierPayment`` via ``base.services.tender`` (PROVIDER_TO_METHOD), never as 100% cash.

``apply_webhook`` is the seam for a future online gateway: it confirms/reverses
a payment server-side (never from the client) and fires the same WS events. It
is inert until ``COURIER_PAYMENT_WEBHOOK_SECRET`` is configured.
"""
import hashlib
import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from base.models import Order, OrderPayment
from base.services.accounting_cursor import lock_branch_accounting
from couriers import presenters, realtime, services
from couriers.models import CourierPayment

logger = logging.getLogger('couriers.payments')

# MONEY INVARIANT: this is the one intentionally shiftless settlement path.
# CourierPayment is provider evidence for a DELIVERY collected at the door; it
# never creates OrderPayment, touches a till drawer, or impersonates the normal
# cashier pay/refund path (which owns the active-shift guard).
_PAID_WORDS = {'paid', 'success', 'succeeded', 'completed', 'confirmed'}
_REFUND_WORDS = {'refund', 'refunded', 'reversed', 'chargeback'}
_FAIL_WORDS = {'fail', 'failed', 'canceled', 'cancelled', 'declined'}
_CONCRETE_METHODS = {
    value for value, _label in Order.PaymentMethod.choices
    if value != Order.PaymentMethod.MIXED
}


# --------------------------------------------------------------------------- #
# order paid-state (rolled-up onto base.Order, recomputed from PAID rows)
# --------------------------------------------------------------------------- #
def _payment_evidence(order):
    """Return valid till + courier settlement evidence for a locked order.

    ``OrderPayment.CASH`` stores tendered cash and may include change, so only
    the residual bill after non-cash till rows is counted. Courier rows are
    collected amounts and therefore count exactly. Invalid historical rows
    block header mutation instead of being guessed into a tender bucket.
    """
    due = Decimal(str(order.total_amount or 0))
    till_rows = list(
        OrderPayment.objects.filter(order=order, is_deleted=False)
        .order_by('created_at', 'pk')
    )
    methods = set()
    timestamps = []
    noncash = Decimal('0')
    cash_tendered = Decimal('0')
    for row in till_rows:
        amount = Decimal(str(row.amount))
        if row.branch_id != order.branch_id:
            return None, 'order has till payment evidence from another branch'
        if row.method not in _CONCRETE_METHODS:
            return None, f'order has invalid till payment method {row.method!r}'
        if amount < 0:
            return None, 'order has a negative till payment amount'
        if amount == 0:
            continue
        timestamps.append(row.created_at)
        if row.method == Order.PaymentMethod.CASH:
            cash_tendered += amount
        else:
            noncash += amount
            methods.add(row.method)

    if noncash > due:
        return None, 'existing till non-cash payments exceed order total'
    # A raw CASH row can exceed the residual because it records the amount
    # tendered before change. Never count that change as a second settlement.
    cash_applied = min(cash_tendered, max(due - noncash, Decimal('0')))
    if cash_applied > 0:
        methods.add(Order.PaymentMethod.CASH)
    total = noncash + cash_applied

    courier_rows = list(
        CourierPayment.objects.filter(
            order=order, status=CourierPayment.Status.PAID,
        ).order_by('paid_at', 'created_at', 'pk')
    )
    for row in courier_rows:
        method = CourierPayment.PROVIDER_TO_METHOD.get(row.provider)
        if row.branch_id != order.branch_id:
            return None, 'order has courier payment evidence from another branch'
        if method not in _CONCRETE_METHODS:
            return None, f'order has invalid courier provider {row.provider!r}'
        amount = Decimal(str(row.amount))
        if amount <= 0:
            return None, 'order has a non-positive courier payment amount'
        total += amount
        methods.add(method)
        timestamps.append(row.paid_at or row.created_at)

    return {
        'due': due,
        'total': total,
        'methods': methods,
        'paid_at': max((stamp for stamp in timestamps if stamp), default=None),
        'has_rows': bool(till_rows or courier_rows),
    }, None


def _capacity_error(order, amount):
    """Reject a new collection if concrete/legacy settlement leaves no room."""
    evidence, error = _payment_evidence(order)
    if error:
        return error
    amount = Decimal(str(amount))
    due = evidence['due']
    if due <= 0:
        return 'order has no positive amount due'
    # A paid legacy till order can pre-date OrderPayment rows. Do not collect it
    # again merely because its old line-level evidence is absent.
    if order.is_paid and not evidence['has_rows']:
        return 'order is already paid'
    remaining = due - evidence['total']
    if amount > remaining:
        return f'payment exceeds remaining order amount ({max(remaining, Decimal("0"))})'
    return None


def _recompute_order_paid(order, *, accounting_recorded_at=None):
    """Re-derive the paid header from all concrete till/courier evidence."""
    evidence, error = _payment_evidence(order)
    if error:
        logger.error('Cannot recompute paid header for order %s: %s', order.pk, error)
        return
    total_paid = evidence['total']
    total_due = evidence['due']
    fields = []
    if total_paid > 0 and total_paid >= total_due:
        methods = evidence['methods']
        method = next(iter(methods)) if len(methods) == 1 else 'MIXED'
        if not order.is_paid:
            order.is_paid = True
            fields.append('is_paid')
            if (
                order.accounting_recorded_at is None
                and accounting_recorded_at is not None
            ):
                order.accounting_recorded_at = accounting_recorded_at
                fields.append('accounting_recorded_at')
        if order.payment_method != method:
            order.payment_method = method
            fields.append('payment_method')
        inferred_paid_at = evidence['paid_at'] or timezone.now()
        if order.paid_at != inferred_paid_at:
            order.paid_at = inferred_paid_at
            fields.append('paid_at')
    elif order.is_paid:
        # A settled header is immutable sale evidence. Missing/removed child
        # evidence is a reconciliation defect or a later refund event, never
        # permission to rewrite paid_at into the present or clear history.
        logger.error(
            'Refusing to erase paid header for order %s: evidence=%s due=%s',
            order.pk, total_paid, total_due,
        )
    else:
        # Unsettled drafts may still carry stale rolled-up fields from an
        # interrupted partial-payment attempt; clearing those is not erasing a
        # completed sale because is_paid has never become true.
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
        amount = order.total_amount
    try:
        decimal_amount = Decimal(str(amount).strip())
    except (ArithmeticError, TypeError, ValueError):
        return None, 'amount must be an integer (so\'m)'
    if (not decimal_amount.is_finite()
            or decimal_amount != decimal_amount.to_integral_value()):
        return None, 'amount must be an integer (so\'m)'
    amount = int(decimal_amount)
    if amount <= 0:
        return None, 'amount must be positive'
    return amount, None


def _identity_error(payment, *, order, courier, provider, amount):
    """Validate an external_id replay without mutating the original event."""
    if payment.order_id != order.pk:
        return 'external_id already belongs to another order'
    if payment.provider != provider:
        return 'external_id already belongs to a different provider'
    if int(payment.amount) != int(amount):
        return 'external_id already belongs to a different amount'
    courier_id = getattr(courier, 'pk', None)
    if payment.courier_id != courier_id:
        return 'external_id already belongs to a different courier'
    return None


def _manual_external_id(*, order, courier, provider, amount):
    """Stable key for old clients that omit an explicit idempotency key.

    A caller intentionally recording two otherwise identical split events can
    supply distinct external IDs. A normal mobile retry resolves to the same
    durable event, including after that event has been refunded.
    """
    identity = '|'.join((
        str(order.uuid), str(getattr(courier, 'pk', '') or ''),
        provider, str(int(amount)),
    ))
    return 'manual:' + hashlib.sha256(identity.encode('utf-8')).hexdigest()


def _refund_event_id(payment):
    return services.courier_payment_event_key(payment)


def _existing_refund(payment):
    from base.models import OrderRefund
    return OrderRefund.objects.filter(
        source=OrderRefund.Source.COURIER_PAYMENT,
        source_id=_refund_event_id(payment),
        is_deleted=False,
    ).first()


def _present_refunded(payment, refund):
    """Expose legacy wire status without mutating provider sale evidence."""
    payment.status = CourierPayment.Status.REFUNDED
    payment.refunded_at = refund.refunded_at
    payment._order_refund = refund
    return payment


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def create_payment(courier, order, provider, *, amount=None, note='', external_id='', link=''):
    """Record shiftless provider evidence for a courier DELIVERY.

    This is not a shortcut around cashier settlement: it writes only
    ``CourierPayment`` and never mutates a drawer or ``OrderPayment``. Returns
    ``(payment, None)`` or ``(None, error_message)``.
    """
    provider = (provider or '').strip().upper()
    if provider not in CourierPayment.Provider.values:
        return None, 'invalid provider (expected CASH, CARD or QR)'
    use_locked_order_total = amount is None or amount == ''
    amount, err = _coerce_amount(amount, order)
    if err:
        return None, err

    supplied_external_id = (external_id or '').strip()[:128]
    with transaction.atomic():
        # Lock the order row so concurrent create/refund recomputes serialize
        # (the rolled-up is_paid/payment_method can't be left stale).
        order = Order.objects.select_for_update().get(pk=order.pk)
        if order.order_type != Order.OrderType.DELIVERY:
            return None, 'courier payments are valid only for delivery orders'
        if courier is not None:
            assignment_courier_id = getattr(
                getattr(order, 'courier_delivery', None), 'courier_id', None,
            )
            if assignment_courier_id != courier.pk:
                return None, 'order is not assigned to this courier'
        if not order.branch_id:
            return None, 'delivery order has no branch ownership'
        # Global lock order is branch register -> courier. Inkassa and courier
        # settlement take the same owner rows before their cutoffs, so this event
        # falls wholly before one boundary or wholly after it.
        lock_branch_accounting(order.branch_id)
        if courier is not None:
            courier = services.lock_courier_accounting(courier)
        now = timezone.now()
        if use_locked_order_total:
            amount, err = _coerce_amount(None, order)
            if err:
                return None, err
        external_id = supplied_external_id or _manual_external_id(
            order=order, courier=courier, provider=provider, amount=amount,
        )
        values = {
            'order': order,
            'courier': courier,
            'provider': provider,
            'amount': amount,
            'status': CourierPayment.Status.PAID,
            'paid_at': now,
            'branch_id': order.branch_id or '',
            'note': (note or '')[:200],
            'link': (link or '')[:512],
        }

        payment = (
            CourierPayment.objects.select_for_update()
            .filter(external_id=external_id)
            .first()
        )
        if payment is None and not supplied_external_id:
            # Upgrade a pre-release blank-key event in place. This makes the
            # first retry after deployment idempotent without deleting or
            # rewriting any historical money row.
            payment = (
                CourierPayment.objects.select_for_update()
                .filter(
                    external_id='', order=order, courier=courier,
                    provider=provider, amount=amount,
                )
                .order_by('created_at', 'pk')
                .first()
            )
            if payment is not None:
                payment.external_id = external_id
                payment.save(update_fields=['external_id'])

        if payment is not None:
            identity_error = _identity_error(
                payment, order=order, courier=courier,
                provider=provider, amount=amount,
            )
            if identity_error:
                return None, identity_error
            if payment.status == CourierPayment.Status.PAID:
                # Exact replay: repair the header if necessary but never emit a
                # second money event/notification.
                _recompute_order_paid(order, accounting_recorded_at=now)
                refund = _existing_refund(payment)
                if refund is not None:
                    return _present_refunded(payment, refund), None
                return payment, None
            if payment.status == CourierPayment.Status.REFUNDED:
                _recompute_order_paid(order, accounting_recorded_at=now)
                return payment, None
            if payment.status == CourierPayment.Status.FAILED:
                _recompute_order_paid(order, accounting_recorded_at=now)
                return None, 'external_id belongs to a failed payment'
            if payment.status != CourierPayment.Status.PENDING:
                _recompute_order_paid(order, accounting_recorded_at=now)
                return None, f'invalid existing payment status {payment.status!r}'
            capacity_error = _capacity_error(order, amount)
            if capacity_error:
                return None, capacity_error
            payment.status = CourierPayment.Status.PAID
            payment.paid_at = now
            payment.save(update_fields=['status', 'paid_at'])
        else:
            capacity_error = _capacity_error(order, amount)
            if capacity_error:
                return None, capacity_error
            # get_or_create retains race safety for a malicious/global explicit
            # key reused concurrently against two differently locked orders.
            payment, created = CourierPayment.objects.get_or_create(
                external_id=external_id, defaults=values,
            )
            if not created:
                payment = CourierPayment.objects.select_for_update().get(pk=payment.pk)
                identity_error = _identity_error(
                    payment, order=order, courier=courier,
                    provider=provider, amount=amount,
                )
                if identity_error:
                    return None, identity_error
                # Another request won the key race. Treat only an exact PAID or
                # REFUNDED replay as success; never revive a terminal event.
                if payment.status in {
                    CourierPayment.Status.PAID,
                    CourierPayment.Status.REFUNDED,
                }:
                    _recompute_order_paid(order, accounting_recorded_at=now)
                    refund = _existing_refund(payment)
                    if refund is not None:
                        return _present_refunded(payment, refund), None
                    return payment, None
                return None, f'external_id belongs to a {payment.status.lower()} payment'
        _recompute_order_paid(order, accounting_recorded_at=now)

    _emit(payment, 'payment.paid', order)
    label = dict(CourierPayment.Provider.choices).get(provider, provider)
    services.notify(
        courier, icon='cash' if payment.is_cash else 'creditcard', tone='success',
        title=f'Payment received · order #{order.id}',
        body=f'{presenters.so_m(payment.amount):,} so\'m via {label}.', order=order,
    )
    return payment, None


def refund_payment(payment):
    """Append a provider refund while preserving PAID sale evidence."""
    emitted = False
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=payment.order_id)
        payment = CourierPayment.objects.select_for_update().get(pk=payment.pk)
        if payment.status != CourierPayment.Status.PAID:
            _recompute_order_paid(order)
            return None, 'only a paid payment can be refunded'
        if not order.branch_id:
            return None, 'delivery order has no branch ownership'
        lock_branch_accounting(order.branch_id)
        if payment.courier_id:
            payment.courier = services.lock_courier_accounting(
                payment.courier_id,
            )
        from base.services.order_refund import (
            SettlementInvariantError, record_external_provider_refund,
        )
        method = CourierPayment.PROVIDER_TO_METHOD.get(payment.provider)
        try:
            refund, emitted = record_external_provider_refund(
                order,
                method=method,
                amount=payment.amount,
                source_id=_refund_event_id(payment),
                reason=f'Courier payment #{payment.pk} provider refund',
            )
        except SettlementInvariantError as exc:
            return None, str(exc)
        # Deliberately no _recompute_order_paid(): paid_at/method and the PAID
        # CourierPayment are the immutable positive event.
    payment = _present_refunded(payment, refund)
    if emitted:
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
    external_id = (external_id or '').strip()[:128]
    payment = None
    if payment_id:
        payment = (CourierPayment.objects.select_related('order', 'courier')
                   .filter(pk=payment_id).first())
    elif external_id:
        payment = (CourierPayment.objects.select_related('order', 'courier')
                   .filter(external_id=external_id).first())

    if status in _PAID_WORDS:
        if payment:
            with transaction.atomic():
                order = Order.objects.select_for_update().get(pk=payment.order_id)
                payment = CourierPayment.objects.select_for_update().get(pk=payment.pk)
                if payment.status == CourierPayment.Status.PAID:
                    _recompute_order_paid(order)
                    refund = _existing_refund(payment)
                    if refund is not None:
                        logger.warning(
                            'Ignored late paid callback for refunded payment %s',
                            payment.pk,
                        )
                        return _present_refunded(payment, refund), None
                    return payment, None
                if payment.status in {
                    CourierPayment.Status.REFUNDED,
                    CourierPayment.Status.FAILED,
                }:
                    # First terminal outcome wins for this event identity. Late
                    # or out-of-order callbacks are acknowledged as no-ops so a
                    # gateway does not retry forever, but money is never revived.
                    _recompute_order_paid(order)
                    logger.warning(
                        'Ignored late paid callback for terminal payment %s (%s)',
                        payment.pk, payment.status,
                    )
                    return payment, None
                if payment.status != CourierPayment.Status.PENDING:
                    _recompute_order_paid(order)
                    return None, f'invalid existing payment status {payment.status!r}'
                capacity_error = _capacity_error(order, payment.amount)
                if capacity_error:
                    return None, capacity_error
                if not order.branch_id:
                    return None, 'delivery order has no branch ownership'
                lock_branch_accounting(order.branch_id)
                if payment.courier_id:
                    payment.courier = services.lock_courier_accounting(
                        payment.courier_id,
                    )
                now = timezone.now()
                payment.status = CourierPayment.Status.PAID
                payment.paid_at = now
                payment.save(update_fields=['status', 'paid_at'])
                _recompute_order_paid(order, accounting_recorded_at=now)
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
        if payment:
            with transaction.atomic():
                order = Order.objects.select_for_update().get(pk=payment.order_id)
                payment = CourierPayment.objects.select_for_update().get(pk=payment.pk)
                if payment.status == CourierPayment.Status.PENDING:
                    payment.status = CourierPayment.Status.FAILED
                    payment.save(update_fields=['status'])
                elif payment.status in {
                    CourierPayment.Status.PAID,
                    CourierPayment.Status.REFUNDED,
                }:
                    logger.warning(
                        'Ignored late failed callback for terminal payment %s (%s)',
                        payment.pk, payment.status,
                    )
                _recompute_order_paid(order)
        return payment, None

    return None, f'unsupported status {status!r}'
