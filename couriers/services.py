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

from django.db import transaction
from django.utils import timezone

from couriers.models import Courier, DeliveryAssignment, LocationPing
from couriers import realtime, push, presenters

logger = logging.getLogger('couriers.services')

ACCEPT_WINDOW_SECONDS = 20      # IncomingOrderSheet hold-to-accept countdown


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


# --------------------------------------------------------------------------- #
# assignment (cashier/admin -> courier)
# --------------------------------------------------------------------------- #
@transaction.atomic
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
    if not assignment.can_advance_to(target):
        return None, f'Illegal transition {assignment.step} -> {target}'

    assignment.step = target
    fields = ['step', 'updated_at']
    ts_field = _STEP_TS.get(target)
    if ts_field:
        setattr(assignment, ts_field, timezone.now())
        fields.append(ts_field)
    assignment.save(update_fields=fields)

    order = assignment.order
    if target == DeliveryAssignment.Step.DELIVERED:
        # Close the POS order so it syncs back to the till as completed.
        if order.status != 'COMPLETED':
            order.status = 'COMPLETED'
            order.save(update_fields=['status', 'updated_at'])
        realtime.send_to_cashiers(order.branch_id, 'order.delivered', {
            'order_id': order.id,
            'courier_id': assignment.courier.code if assignment.courier else None,
            'at': timezone.now().isoformat(),
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
    return courier
