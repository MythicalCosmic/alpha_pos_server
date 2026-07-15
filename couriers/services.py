"""Courier delivery domain logic — the one place order/courier lifecycle is
mutated and the one place events are emitted (so REST handlers, the kitchen
signal and webhooks all funnel through here).

Lifecycle (courier projection):  ASSIGNED -> READY -> PICKED_UP -> ON_WAY -> DELIVERED
  * ASSIGNED  : assigned, kitchen still preparing.
  * READY     : kitchen marked the order READY (server-driven, not the courier).
  * PICKED_UP/ON_WAY : courier-driven; location sharing is ON here.
  * DELIVERED : terminal; also closes base.Order (status=COMPLETED).
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from base.services.accounting_cursor import lock_branch_accounting
from couriers.models import (
    Courier, DeliveryAssignment, LocationPing, LocationTrailPoint,
    CourierPayment, CourierSettlement, CourierNotification,
)
from couriers import realtime, push, presenters, geo

logger = logging.getLogger('couriers.services')

ACCEPT_WINDOW_SECONDS = 20      # IncomingOrderSheet hold-to-accept countdown
TRAIL_RETENTION_DAYS = 7        # GPS breadcrumbs older than this are pruned


def lock_courier_accounting(courier_or_id):
    """Lock the courier row that serializes ledger events and cutoffs."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('courier accounting lock requires an atomic transaction')
    courier_id = getattr(courier_or_id, 'pk', courier_or_id)
    return Courier.objects.select_for_update().get(pk=courier_id)


def _today_start():
    now = timezone.localtime()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


# --------------------------------------------------------------------------- #
# event funnel
# --------------------------------------------------------------------------- #
def _emit(order, event, data, *, courier_id=None, to_cashiers=True, push_title=None,
          push_body=None, courier_for_push=None):
    """Emit a courier event over WS (to the courier and/or the branch cashiers)
    and optionally a background push to the courier."""
    realtime.push_courier_event(
        event,
        courier_id=courier_id,
        branch_id=order.branch_id if to_cashiers else None,
        data=data,
    )
    if push_title and courier_for_push is not None:
        push.push_to_courier(courier_for_push, push_title, push_body or '',
                             data={'order_id': order.id})


def notify(courier, *, icon='bell', tone='primary', title='', body='', order=None):
    """Persist a courier-app notification (the bell feed). Best-effort — a feed
    write must never break the order/payment flow it rides along with."""
    if courier is None or not title:
        return None
    try:
        return CourierNotification.objects.create(
            courier=courier, icon=(icon or 'bell')[:24], tone=(tone or 'primary')[:12],
            title=title[:160], body=(body or '')[:400], order=order,
        )
    except Exception:  # noqa: BLE001
        logger.debug('courier notify failed', exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# assignment (cashier/admin -> courier)
# --------------------------------------------------------------------------- #
@transaction.atomic
def pick_available_courier():
    """Auto-assign policy: the first ONLINE courier with no in-flight delivery
    (ASSIGNED/READY/PICKED_UP). Deliberately simple — swap for nearest /
    round-robin later. Returns a Courier or None when all are busy/offline."""
    busy = DeliveryAssignment.objects.filter(
        step__in=(DeliveryAssignment.Step.ASSIGNED,
                  DeliveryAssignment.Step.READY,
                  DeliveryAssignment.Step.PICKED_UP),
    ).values_list('courier_id', flat=True)
    return (Courier.objects.filter(online=True).exclude(id__in=list(busy))
            .order_by('id').first())


def assign(order, courier, *, fee=0, addr_text='', addr_landmark='', addr_lat=None,
           addr_lng=None, distance_km=None):
    """Assign a delivery order to a courier; (re)opens the hold-to-accept window
    and fires order.assigned + push. Idempotent on the order (OneToOne)."""
    now = timezone.now()
    assignment, _ = DeliveryAssignment.objects.update_or_create(
        order=order,
        defaults={
            'courier': courier,
            'step': DeliveryAssignment.Step.ASSIGNED,
            'fee': int(fee or 0),
            'assigned_at': now,
            'accepted_at': None,
            'declined_reason': '',
            'expires_at': now + timedelta(seconds=ACCEPT_WINDOW_SECONDS),
            'addr_text': addr_text or '',
            'addr_landmark': addr_landmark or '',
            'addr_lat': addr_lat,
            'addr_lng': addr_lng,
            'distance_km': distance_km,
        },
    )
    addr = presenters._address(order, assignment)
    _emit(order, 'order.assigned', {
        'order_id': order.id,
        'total': presenters.so_m(order.total_amount),
        'fee': int(assignment.fee),
        'payment': 'PAID' if order.is_paid else 'UNPAID',
        'customer': {'name': presenters._customer(order)['name']},
        'address': {'text': addr['text'], 'distance_km': addr['distanceKm']},
        'expires_in': ACCEPT_WINDOW_SECONDS,
    }, courier_id=courier.id, to_cashiers=False,
        push_title=f'New order #{order.id} assigned',
        push_body='Kitchen is preparing — head over.', courier_for_push=courier)
    # let the desktop reflect the assignment too
    realtime.send_to_cashiers(order.branch_id, 'order.status', {
        'order_id': order.id, 'courier_id': courier.code, 'step': assignment.step,
    })
    notify(courier, icon='scooter', tone='primary',
           title=f'New order #{order.id}',
           body='Assigned — kitchen is preparing.', order=order)
    return assignment


def accept(assignment):
    """Courier accepts within the window. Step stays ASSIGNED until the kitchen
    is READY; we just record acceptance and tell the desktop."""
    if assignment.expires_at and timezone.now() > assignment.expires_at:
        return False, 'Accept window expired'
    if assignment.step not in (DeliveryAssignment.Step.ASSIGNED,
                               DeliveryAssignment.Step.READY):
        return False, 'Order is not awaiting acceptance'
    assignment.accepted_at = timezone.now()
    assignment.save(update_fields=['accepted_at', 'updated_at'])
    realtime.send_to_cashiers(assignment.order.branch_id, 'order.status', {
        'order_id': assignment.order_id,
        'courier_id': assignment.courier.code if assignment.courier else None,
        'step': assignment.step,
    })
    return True, None


def decline(assignment, reason=''):
    """Courier declines — free the order for reassignment."""
    assignment.step = DeliveryAssignment.Step.DECLINED
    assignment.declined_reason = (reason or '')[:200]
    assignment.save(update_fields=['step', 'declined_reason', 'updated_at'])
    realtime.send_to_cashiers(assignment.order.branch_id, 'order.status', {
        'order_id': assignment.order_id,
        'courier_id': assignment.courier.code if assignment.courier else None,
        'step': 'DECLINED',
    })
    return True, None


# --------------------------------------------------------------------------- #
# kitchen READY -> courier (server-driven, via signal)
# --------------------------------------------------------------------------- #
def mark_ready(order):
    """Kitchen marked the order READY: flip the courier step and notify. Safe to
    call repeatedly — only the first ASSIGNED->READY transition emits."""
    assignment = getattr(order, 'courier_delivery', None)
    if not assignment or assignment.step != DeliveryAssignment.Step.ASSIGNED:
        return
    assignment.step = DeliveryAssignment.Step.READY
    assignment.ready_at = timezone.now()
    assignment.save(update_fields=['step', 'ready_at', 'updated_at'])
    courier = assignment.courier
    _emit(order, 'order.ready', {'order_id': order.id},
          courier_id=courier.id if courier else None,
          push_title=f'Order #{order.id} is ready',
          push_body='Ready for pickup at the counter.',
          courier_for_push=courier)
    notify(courier, icon='checkcircle', tone='success',
           title=f'Order #{order.id} is ready',
           body='Ready for pickup at the counter.', order=order)


# --------------------------------------------------------------------------- #
# courier-driven status transitions
# --------------------------------------------------------------------------- #
_STEP_TS = {
    DeliveryAssignment.Step.PICKED_UP: 'picked_at',
    DeliveryAssignment.Step.DELIVERED: 'delivered_at',
}


@transaction.atomic
def advance_status(assignment, target):
    """Courier advances the order (PICKED_UP/ON_WAY/DELIVERED). Forward-only and
    owner-scoped (the caller already checked ownership). READY is kitchen-only."""
    if target not in DeliveryAssignment.COURIER_SETTABLE:
        return None, f'Courier cannot set step {target}'
    assignment = (
        DeliveryAssignment.objects.select_for_update()
        .select_related('order', 'courier')
        .get(pk=assignment.pk)
    )
    if not assignment.can_advance_to(target):
        return None, f'Illegal transition {assignment.step} -> {target}'

    order = assignment.order
    if target == DeliveryAssignment.Step.DELIVERED:
        # Payment/refund writers lock Order -> branch -> Courier. Locking the
        # courier first here and then updating Order formed the reverse half of
        # a real deadlock when payment confirmation and DELIVERED overlapped.
        # Keep one compatible order: Assignment -> Order -> Courier.
        from base.models import Order
        order = Order.objects.select_for_update().get(pk=assignment.order_id)

    assignment.step = target
    fields = ['step', 'updated_at']
    ts_field = _STEP_TS.get(target)
    if ts_field:
        if target == DeliveryAssignment.Step.DELIVERED and assignment.courier_id:
            assignment.courier = lock_courier_accounting(assignment.courier_id)
        setattr(assignment, ts_field, timezone.now())
        fields.append(ts_field)
    assignment.save(update_fields=fields)

    if target == DeliveryAssignment.Step.DELIVERED:
        # Close the POS order so it syncs back to the till as completed.
        if order.status != 'COMPLETED':
            order.status = 'COMPLETED'
            order.save(update_fields=['status', 'updated_at'])
        realtime.send_to_cashiers(order.branch_id, 'order.delivered', {
            'order_id': order.id,
            'courier_id': assignment.courier.code if assignment.courier else None,
            'at': assignment.delivered_at.isoformat(),
        })

    data = {'order_id': order.id, 'step': target}
    realtime.push_courier_event(
        'order.status',
        courier_id=assignment.courier_id,
        branch_id=order.branch_id,
        data={**data, 'courier_id': assignment.courier.code if assignment.courier else None},
    )
    return assignment, None


# --------------------------------------------------------------------------- #
# location (REST fallback when the socket is down — §5)
# --------------------------------------------------------------------------- #
def update_location(courier, lat, lng):
    LocationPing.objects.update_or_create(
        courier=courier, defaults={'lat': lat, 'lng': lng},
    )
    # Append a trail breadcrumb only while on-shift + sharing, so distanceKm is
    # scoped to active shifts and we don't store a trail for an idle/private app.
    if courier.online and courier.share_loc:
        LocationTrailPoint.objects.create(courier=courier, lat=lat, lng=lng)
    if not courier.share_loc:
        return
    assignment = courier.current_delivery()
    if not assignment:
        return
    realtime.send_to_cashiers(assignment.order.branch_id, 'courier.location', {
        'courier_id': courier.code, 'order_id': assignment.order_id,
        'lat': lat, 'lng': lng, 'at': timezone.now().isoformat(),
    })


def set_online(courier, online):
    courier.online = bool(online)
    if online and not courier.shift_started_at:
        courier.shift_started_at = timezone.now()
    if not online:
        courier.shift_started_at = None
    courier.save(update_fields=['online', 'shift_started_at', 'updated_at'])
    # Opportunistic prune at end of shift keeps the trail table bounded without a
    # cron (the standalone `prune_courier_trail` command exists for ops too).
    if not online:
        prune_trail(courier)
    return courier


def prune_trail(courier=None, *, days=TRAIL_RETENTION_DAYS):
    """Delete GPS breadcrumbs older than `days`. Scoped to one courier when given,
    else fleet-wide. Returns the number of rows removed."""
    cutoff = timezone.now() - timedelta(days=days)
    qs = LocationTrailPoint.objects.filter(at__lt=cutoff)
    if courier is not None:
        qs = qs.filter(courier=courier)
    deleted, _ = qs.delete()
    return deleted


def set_share_location(courier, share):
    courier.share_loc = bool(share)
    courier.save(update_fields=['share_loc', 'updated_at'])
    return courier


# --------------------------------------------------------------------------- #
# distance (GPS trail -> km)
# --------------------------------------------------------------------------- #
def shift_distance_km(courier):
    """Kilometres travelled this shift, summed from the GPS trail. 0.0 when not
    on shift or with too few fixes."""
    start = courier.shift_started_at or _today_start()
    pts = (LocationTrailPoint.objects
           .filter(courier=courier, at__gte=start)
           .order_by('at').only('lat', 'lng'))
    return round(geo.trail_distance_km(pts), 1)


# --------------------------------------------------------------------------- #
# money: reconciliation + settlement (the courier's cash/payout ledger)
# --------------------------------------------------------------------------- #
def courier_payment_event_key(payment):
    """Return the immutable refund key used by every courier payment writer."""
    return str(payment.external_id or f'legacy-payment:{payment.pk}')


def courier_refund_events(courier, *, start=None, end=None):
    """Raw immutable refunds that reverse this courier's payment evidence.

    A normal cancellation may reverse the still-unrefunded remainder of courier
    tender in one ``ORDER_CANCEL`` event. Include those rows as well as direct
    provider reversals; consumers that need amounts must use
    :func:`courier_refund_entries`, which attributes only the courier portion of
    a mixed till/courier cancellation.
    """
    from base.models import OrderRefund
    courier_payments = list(
        CourierPayment.objects.filter(courier=courier)
        .only('pk', 'external_id', 'order_id')
    )
    payment_keys = [
        courier_payment_event_key(payment) for payment in courier_payments
    ]
    order_ids = {payment.order_id for payment in courier_payments}
    relevant = Q(
        source=OrderRefund.Source.COURIER_PAYMENT,
        source_id__in=payment_keys,
    )
    if order_ids:
        relevant |= Q(
            source=OrderRefund.Source.ORDER_CANCEL,
            order_id__in=order_ids,
        )
    qs = OrderRefund.objects.filter(is_deleted=False).filter(relevant)
    if start is not None:
        qs = qs.filter(accounting_recorded_at__gte=start)
    if end is not None:
        qs = qs.filter(accounting_recorded_at__lt=end)
    return qs


def courier_refund_entries(courier, *, start=None, end=None):
    """Return courier-attributed refund amounts over ``[start, end)``.

    ``ORDER_CANCEL`` freezes the whole order's remaining tender. For a delivery
    split between a till and a courier, subtracting that whole row from the
    courier would overstate the reversal. Derive each courier remainder from its
    immutable payment, subtract prior provider refunds, then cap it by the
    cancellation's frozen tender bucket.
    """
    from base.models import OrderRefund

    courier_payments = list(
        CourierPayment.objects.filter(
            courier=courier,
            status__in=(
                CourierPayment.Status.PAID,
                CourierPayment.Status.REFUNDED,
            ),
            paid_at__isnull=False,
        ).only('pk', 'external_id', 'order_id', 'provider', 'amount')
    )
    payment_by_key = {
        courier_payment_event_key(payment): payment
        for payment in courier_payments
    }
    payments_by_order = {}
    for payment in courier_payments:
        payments_by_order.setdefault(payment.order_id, []).append(payment)

    provider_refunds = {
        refund.source_id: refund
        for refund in OrderRefund.objects.filter(
            is_deleted=False,
            source=OrderRefund.Source.COURIER_PAYMENT,
            source_id__in=list(payment_by_key),
        ).only('source_id', 'amount', 'refunded_at', 'accounting_recorded_at')
    }
    rows = list(
        courier_refund_events(courier, start=start, end=end)
        .order_by('refunded_at', 'pk')
    )
    entries = []
    for refund in rows:
        cash = card = payme = Decimal('0')
        label = 'Order cancellation'
        if refund.source == OrderRefund.Source.COURIER_PAYMENT:
            payment = payment_by_key.get(refund.source_id)
            if payment is None:
                continue
            label = payment.get_provider_display()
            if payment.provider == CourierPayment.Provider.CASH:
                cash = Decimal(refund.amount or 0)
            elif payment.provider == CourierPayment.Provider.CARD:
                card = Decimal(refund.amount or 0)
            else:
                payme = Decimal(refund.amount or 0)
        else:
            outstanding = {'cash': Decimal('0'), 'card': Decimal('0'),
                           'payme': Decimal('0')}
            for payment in payments_by_order.get(refund.order_id, ()):
                remaining = Decimal(payment.amount or 0)
                prior = provider_refunds.get(courier_payment_event_key(payment))
                if (
                    prior is not None
                    and prior.accounting_recorded_at
                    <= refund.accounting_recorded_at
                ):
                    remaining = max(
                        remaining - Decimal(prior.amount or 0), Decimal('0'),
                    )
                bucket = {
                    CourierPayment.Provider.CASH: 'cash',
                    CourierPayment.Provider.CARD: 'card',
                    CourierPayment.Provider.QR: 'payme',
                }.get(payment.provider)
                if bucket:
                    outstanding[bucket] += remaining
            # drawer_cash_amount is the cash that came through the POS till;
            # only the remainder could have been held by this courier.
            cancellation_courier_cash = max(
                Decimal(refund.cash_amount or 0)
                - Decimal(refund.drawer_cash_amount or 0),
                Decimal('0'),
            )
            cash = min(cancellation_courier_cash, outstanding['cash'])
            card = min(Decimal(refund.card_amount or 0), outstanding['card'])
            payme = min(Decimal(refund.payme_amount or 0), outstanding['payme'])

        attributed = cash + card + payme
        if attributed <= 0:
            continue
        entries.append({
            'refund': refund,
            'source_id': refund.source_id,
            'order_id': refund.order_id,
            'refunded_at': refund.refunded_at,
            'cash_amount': cash,
            'card_amount': card,
            'payme_amount': payme,
            'amount': attributed,
            'label': label,
        })
    return entries


def unsettled_start(courier):
    """Start of the courier's current (unsettled) accounting window.

    The end of the last settlement if there is one. Otherwise a STABLE anchor:
    the earliest unsettled money event (first PAID payment / first delivery),
    falling back to midnight. Deliberately NOT based on ``shift_started_at`` —
    that flag is nulled on going offline, which would otherwise move the window
    (and the cash-in-hand the rider sees) without any money changing hands, and
    would drop pre-midnight cash from a shift that spans midnight."""
    last = courier.settlements.order_by('-period_end').first()
    if last and last.period_end:
        return last.period_end
    first_pay = (CourierPayment.objects
                 .filter(
                     courier=courier,
                     status__in=(
                         CourierPayment.Status.PAID,
                         CourierPayment.Status.REFUNDED,
                     ),
                     paid_at__isnull=False,
                 )
                 .order_by('paid_at').values_list('paid_at', flat=True).first())
    first_del = (DeliveryAssignment.objects
                 .filter(courier=courier, step=DeliveryAssignment.Step.DELIVERED,
                         delivered_at__isnull=False)
                 .order_by('delivered_at').values_list('delivered_at', flat=True).first())
    candidates = [t for t in (first_pay, first_del) if t]
    return min(candidates) if candidates else _today_start()


def reconciliation_snapshot(courier, *, start=None, end=None, bonuses=0, tips=0):
    """Aggregate the courier's money over [start, end) — defaults to the
    unsettled window up to now. Returns raw integer so'm (presenters shape the
    wire). Cash is collected cash; qr_collected folds card + QR (non-cash).
    bonuses/tips have no live source, so they default to 0 and are only set at
    settle time when an operator records them.

    Fees are windowed by delivered_at and payments by paid_at. In the cash-first
    door-confirmed flow these happen within seconds of each other, so a single
    delivery's fee and cash land in the same window; the only edge where they
    split is a settlement landing in the brief gap between the two — accepted as
    a known limitation for the record-only launch."""
    start = start or unsettled_start(courier)
    end = end or timezone.now()

    done = (DeliveryAssignment.objects.filter(
        courier=courier, step=DeliveryAssignment.Step.DELIVERED,
        delivered_at__gte=start, delivered_at__lt=end))
    deliveries = 0
    delivery_fees = 0
    for a in done:
        deliveries += 1
        delivery_fees += int(a.fee or 0)

    pays = (CourierPayment.objects.filter(
        courier=courier,
        status__in=(
            CourierPayment.Status.PAID,
            CourierPayment.Status.REFUNDED,
        ),
        paid_at__isnull=False,
        paid_at__gte=start, paid_at__lt=end))
    cash_collected = 0
    qr_collected = 0
    cash_orders = set()
    qr_orders = set()
    held = []
    for p in pays:
        amt = int(p.amount)
        if p.provider == CourierPayment.Provider.CASH:
            cash_collected += amt
            cash_orders.add(p.order_id)
            held.append({'order': p.order_id, 'amount': amt})
        else:
            qr_collected += amt
            qr_orders.add(p.order_id)

    for refund in courier_refund_entries(courier, start=start, end=end):
        cash_refunded = int(refund['cash_amount'])
        noncash_refunded = int(
            refund['card_amount'] + refund['payme_amount']
        )
        cash_collected -= cash_refunded
        qr_collected -= noncash_refunded
        if cash_refunded:
            held.append({
                'order': refund['order_id'], 'amount': -cash_refunded,
            })

    bonuses = int(bonuses or 0)
    tips = int(tips or 0)
    net_payout = delivery_fees + bonuses + tips
    return {
        'deliveries': deliveries,
        'cash_collected': cash_collected,
        'qr_collected': qr_collected,
        'delivery_fees': delivery_fees,
        'bonuses': bonuses,
        'tips': tips,
        'cash_orders': len(cash_orders),
        'qr_orders': len(qr_orders),
        'net_payout': net_payout,
        'cash_in_hand': cash_collected,
        'held': held,
        'period_start': start,
        'period_end': end,
    }


@transaction.atomic
def settle(courier, *, bonuses=0, tips=0, note=''):
    """Close the unsettled window: freeze a CourierSettlement snapshot and reset
    'unsettled' to now (everything up to period_end is settled). Returns the row.

    Branch accounting rows are locked first, in deterministic order, then the
    courier. Payment/refund writers use the same branch -> courier order. This
    gives late refund cursors and the cutoff one serial order without coupling
    the shared core refund service to this optional app."""
    branch_ids = sorted({
        str(payment_branch or order_branch or '').strip()
        for payment_branch, order_branch in CourierPayment.objects.filter(
            courier=courier,
        ).values_list('branch_id', 'order__branch_id')
        if str(payment_branch or order_branch or '').strip()
    })
    for branch_id in branch_ids:
        lock_branch_accounting(branch_id)
    courier = lock_courier_accounting(courier)
    end = timezone.now()
    start = unsettled_start(courier)
    snap = reconciliation_snapshot(courier, start=start, end=end,
                                   bonuses=bonuses, tips=tips)
    return CourierSettlement.objects.create(
        courier=courier,
        period_start=start, period_end=end,
        deliveries=snap['deliveries'],
        cash_collected=snap['cash_collected'],
        qr_collected=snap['qr_collected'],
        delivery_fees=snap['delivery_fees'],
        bonuses=snap['bonuses'], tips=snap['tips'],
        net_payout=snap['net_payout'],
        handover_code=f'ALP-{courier.id:04d}',
        note=(note or '')[:200],
    )
