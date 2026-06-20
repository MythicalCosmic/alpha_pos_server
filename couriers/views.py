"""Courier REST API (function views + JsonResponse — the repo convention; no DRF).

Paths are the exact ones the mobile app calls (spec §3). Read feeds are
camelCase; the reconciliation payload is snake_case. Auth is the session token
the app sends as ``Authorization: Token <key>`` (see couriers.auth).
"""
import secrets
from datetime import timedelta

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from django.conf import settings
from django.db.models import Sum

from base.helpers.request import parse_json_body
from base.models import Order
from base.repositories.session import SessionRepository
from base.security.hashing import verify_password, verify_password_dummy

from couriers.auth import courier_required, logout_session
from couriers.models import (
    Courier, DeliveryAssignment, PushToken, CourierPayment, CourierNotification,
)
from couriers import services, presenters, payments

SESSION_TTL_DAYS = 7


def _ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()[:45]
    return request.META.get('REMOTE_ADDR', '0.0.0.0')[:45]


def _ua(request):
    return request.META.get('HTTP_USER_AGENT', '')[:256]


def _today_range():
    now = timezone.localtime()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
@csrf_exempt
@require_POST
def courier_login(request):
    """POST /auth/courier/login/  {phone,password} OR {qr} -> {token}."""
    data, error = parse_json_body(request)
    if error:
        return JsonResponse(error[0], status=error[1])

    phone = (data.get('phone') or '').strip()
    password = data.get('password') or ''
    qr = (data.get('qr') or '').strip()

    if qr and not phone:
        # QR payload format: "phone:password" (the app encodes the rider's creds
        # into the login QR). Adjust to your QR scheme if different.
        if ':' in qr:
            phone, password = qr.split(':', 1)
        else:
            return JsonResponse({'success': False, 'message': 'Invalid login QR'}, status=400)

    if not phone or not password:
        return JsonResponse({'success': False, 'message': 'phone and password required'},
                            status=400)

    courier = (Courier.objects.select_related('user')
               .filter(phone=phone, user__is_deleted=False).first())
    if not courier:
        verify_password_dummy(password)   # constant-time: don't leak which phones exist
        return JsonResponse({'success': False, 'message': 'Invalid credentials'}, status=401)
    if not verify_password(password, courier.user.password):
        return JsonResponse({'success': False, 'message': 'Invalid credentials'}, status=401)
    if getattr(courier.user, 'status', 'ACTIVE') != 'ACTIVE':
        return JsonResponse({'success': False, 'message': 'Account is suspended'}, status=403)

    token = secrets.token_hex(32)
    SessionRepository.create(
        user_id=courier.user,
        ip_address=_ip(request),
        user_agent=_ua(request),
        payload=SessionRepository.hash_token(token),
        expires_at=timezone.now() + timedelta(days=SESSION_TTL_DAYS),
    )
    return JsonResponse({'token': token})


@csrf_exempt
@require_POST
@courier_required
def courier_logout(request):
    """POST /auth/courier/logout/ -> revoke the current session token."""
    logout_session(getattr(request, 'session_key', None))
    return JsonResponse({'ok': True})


# --------------------------------------------------------------------------- #
# profile + feeds (camelCase)
# --------------------------------------------------------------------------- #
@require_GET
@courier_required
def me(request):
    return JsonResponse(presenters.courier_dict(request.courier))


@require_GET
@courier_required
def orders_active(request):
    qs = (DeliveryAssignment.objects
          .filter(courier=request.courier)
          .exclude(step__in=(DeliveryAssignment.Step.DELIVERED,
                             DeliveryAssignment.Step.DECLINED))
          .select_related('order')
          .order_by('-assigned_at'))
    return JsonResponse([presenters.active_order_dict(a.order, a) for a in qs], safe=False)


@require_GET
@courier_required
def orders_completed(request):
    qs = (DeliveryAssignment.objects
          .filter(courier=request.courier, step=DeliveryAssignment.Step.DELIVERED)
          .select_related('order')
          .order_by('-delivered_at')[:100])
    return JsonResponse([presenters.completed_order_dict(a.order, a) for a in qs], safe=False)


@require_GET
@courier_required
def stats_today(request):
    start, end = _today_range()
    done = (DeliveryAssignment.objects
            .filter(courier=request.courier, step=DeliveryAssignment.Step.DELIVERED,
                    delivered_at__gte=start, delivered_at__lt=end)
            .select_related('order'))
    deliveries = 0
    earnings = 0
    minutes_total = 0
    by_hour = {f'{h:02d}': 0 for h in range(8, 24)}
    for a in done:
        deliveries += 1
        earnings += int(a.fee or 0)
        if a.delivered_at and a.order.created_at:
            minutes_total += int((a.delivered_at - a.order.created_at).total_seconds() // 60)
        if a.delivered_at:
            h = timezone.localtime(a.delivered_at).strftime('%H')
            by_hour[h] = by_hour.get(h, 0) + 1
    avg_minutes = int(minutes_total / deliveries) if deliveries else 0
    # Cash actually collected today = PAID CASH payment rows (consistent with the
    # balance/reconciliation ledger), not inferred from order paid-state.
    cash_collected = (CourierPayment.objects.filter(
        courier=request.courier, status=CourierPayment.Status.PAID,
        provider=CourierPayment.Provider.CASH,
        paid_at__gte=start, paid_at__lt=end)
        .aggregate(s=Sum('amount'))['s'] or 0)
    started = request.courier.shift_started_at
    active_hours = ''
    if started:
        secs = int((timezone.now() - started).total_seconds())
        active_hours = f'{secs // 3600}h {secs % 3600 // 60}m'
    return JsonResponse({
        'deliveries': deliveries,
        'earnings': earnings,
        'cashCollected': cash_collected,
        'avgMinutes': avg_minutes,
        'activeHours': active_hours,
        'distanceKm': services.shift_distance_km(request.courier),
        'byHour': [{'h': str(int(h)), 'n': n} for h, n in sorted(by_hour.items())],
    })


@require_GET
@courier_required
def balance(request):
    """Cash held + net payable, backed by the courier payment/settlement ledger.
    `balance` is the unsettled net payout; `heldTotal` the unsettled cash to hand
    over; `ledger` recent settlements + payments, newest first."""
    courier = request.courier
    snap = services.reconciliation_snapshot(courier)

    # (timestamp, row) pairs so we can sort chronologically before the `at`
    # field is flattened to a display string.
    rows = []
    for s in courier.settlements.all()[:20]:
        rows.append((s.at, presenters.ledger_row(
            at=s.at, kind='settlement', amount=s.net_payout,
            label=f'Shift settled · {s.deliveries} deliveries')))
    for p in (CourierPayment.objects.filter(courier=courier)
              .exclude(status=CourierPayment.Status.PENDING)
              .order_by('-created_at')[:20]):
        signed = -int(p.amount) if p.status == CourierPayment.Status.REFUNDED else int(p.amount)
        verb = 'Refund' if p.status == CourierPayment.Status.REFUNDED else 'Collected'
        when = p.refunded_at or p.paid_at or p.created_at
        rows.append((when, presenters.ledger_row(
            at=when, kind='payment', amount=signed, order=p.order_id,
            label=f'{verb} · {p.get_provider_display()} · order #{p.order_id}')))
    rows.sort(key=lambda r: r[0] or timezone.now(), reverse=True)
    ledger = [row for _, row in rows[:30]]

    return JsonResponse(presenters.balance_dict(snap, ledger))


@require_GET
@courier_required
def notifications(request):
    """The courier's recent persisted notifications (bell feed), newest first."""
    qs = (CourierNotification.objects
          .filter(courier=request.courier)
          .order_by('-created_at')[:50])
    return JsonResponse([presenters.notification_dict(n) for n in qs], safe=False)


@csrf_exempt
@require_POST
@courier_required
def notifications_read(request):
    """Mark notifications read. Body ``{"ids": [...]}`` marks those (tolerant of
    'n123' or 123); an empty/absent ids marks all unread read. Returns the
    remaining unread count."""
    data, _ = parse_json_body(request)
    ids = (data or {}).get('ids')
    qs = CourierNotification.objects.filter(courier=request.courier, read_at__isnull=True)
    if ids:
        parsed = []
        for raw in ids:
            try:
                parsed.append(int(str(raw).lstrip('n')))
            except (TypeError, ValueError):
                continue
        qs = qs.filter(id__in=parsed)
    qs.update(read_at=timezone.now())
    unread = CourierNotification.objects.filter(
        courier=request.courier, read_at__isnull=True).count()
    return JsonResponse({'ok': True, 'unread': unread})


@require_GET
@courier_required
def shift_reconciliation(request):
    """SNAKE_CASE on the wire (spec §3). The unsettled-window totals from the
    courier payment/fee ledger — cash, non-cash, fees, net payout, cash-in-hand."""
    snap = services.reconciliation_snapshot(request.courier)
    return JsonResponse(presenters.reconciliation_dict(snap, request.courier))


# --------------------------------------------------------------------------- #
# order actions
# --------------------------------------------------------------------------- #
def _owned_assignment(request, order_id):
    return (DeliveryAssignment.objects
            .select_related('order', 'courier')
            .filter(order_id=order_id, courier=request.courier).first())


@csrf_exempt
@require_POST
@courier_required
def order_accept(request, order_id):
    a = _owned_assignment(request, order_id)
    if not a:
        return JsonResponse({'success': False, 'message': 'Order not assigned to you'},
                            status=403)
    ok, err = services.accept(a)
    if not ok:
        return JsonResponse({'success': False, 'message': err}, status=409)
    return JsonResponse({'ok': True})


@csrf_exempt
@require_POST
@courier_required
def order_decline(request, order_id):
    a = _owned_assignment(request, order_id)
    if not a:
        return JsonResponse({'success': False, 'message': 'Order not assigned to you'},
                            status=403)
    data, _ = parse_json_body(request)
    services.decline(a, (data or {}).get('reason', ''))
    return JsonResponse({'ok': True})


@csrf_exempt
@require_POST
@courier_required
def order_status(request, order_id):
    """{ "step": "PICKED_UP" } -> the updated active order."""
    a = _owned_assignment(request, order_id)
    if not a:
        return JsonResponse({'success': False, 'message': 'Order not assigned to you'},
                            status=403)
    data, error = parse_json_body(request)
    if error:
        return JsonResponse(error[0], status=error[1])
    target = (data.get('step') or '').strip().upper()
    updated, err = services.advance_status(a, target)
    if err:
        return JsonResponse({'success': False, 'message': err}, status=409)
    return JsonResponse(presenters.active_order_dict(updated.order, updated))


# --------------------------------------------------------------------------- #
# location / shift / push
# --------------------------------------------------------------------------- #
@csrf_exempt
@require_POST
@courier_required
def location(request):
    data, error = parse_json_body(request)
    if error:
        return JsonResponse(error[0], status=error[1])
    try:
        lat, lng = float(data['lat']), float(data['lng'])
    except (KeyError, TypeError, ValueError):
        return JsonResponse({'success': False, 'message': 'lat and lng required'}, status=400)
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return JsonResponse({'success': False, 'message': 'lat/lng out of range'}, status=400)
    services.update_location(request.courier, lat, lng)
    return JsonResponse({'ok': True})


@csrf_exempt
@require_POST
@courier_required
def shift_online(request):
    data, error = parse_json_body(request)
    if error:
        return JsonResponse(error[0], status=error[1])
    courier = services.set_online(request.courier, bool(data.get('online')))
    return JsonResponse({'online': courier.online})


@csrf_exempt
@require_POST
@courier_required
def shift_share_location(request):
    """Toggle the 'share live location' setting. Body ``{"share": true}`` (also
    accepts ``shareLocation``/``online`` aliases)."""
    data, error = parse_json_body(request)
    if error:
        return JsonResponse(error[0], status=error[1])
    if 'share' in data:
        share = data.get('share')
    elif 'shareLocation' in data:
        share = data.get('shareLocation')
    else:
        share = data.get('enabled')
    courier = services.set_share_location(request.courier, bool(share))
    return JsonResponse({'shareLocation': courier.share_loc})


@csrf_exempt
@require_POST
@courier_required
def shift_settle(request):
    """Handover: freeze a settlement snapshot and reset the unsettled window.
    Optional body ``{bonuses, tips, note}`` records handover-time adjustments."""
    data, _ = parse_json_body(request)
    data = data or {}
    try:
        bonuses = int(data.get('bonuses') or 0)
        tips = int(data.get('tips') or 0)
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'message': 'bonuses/tips must be integers'},
                            status=400)
    settlement = services.settle(request.courier, bonuses=bonuses, tips=tips,
                                 note=(data.get('note') or ''))
    return JsonResponse(presenters.settlement_dict(settlement))


@csrf_exempt
@require_POST
@courier_required
def push_token(request):
    data, error = parse_json_body(request)
    if error:
        return JsonResponse(error[0], status=error[1])
    token = (data.get('token') or '').strip()
    if not token:
        return JsonResponse({'success': False, 'message': 'token required'}, status=400)
    PushToken.objects.update_or_create(
        token=token,
        defaults={'courier': request.courier, 'platform': data.get('platform', '')[:8]},
    )
    return JsonResponse({'ok': True})


# --------------------------------------------------------------------------- #
# payments (record-only; see couriers.payments)
# --------------------------------------------------------------------------- #
@csrf_exempt
@require_POST
@courier_required
def payment_create(request):
    """POST /payments/create/  {order_id, provider, amount?} -> {payment_id, status, link}.
    Records a courier-collected payment as PAID and fires ``payment.paid``."""
    data, error = parse_json_body(request)
    if error:
        return JsonResponse(error[0], status=error[1])
    try:
        order = Order.objects.filter(pk=int(data.get('order_id'))).first()
    except (TypeError, ValueError):
        order = None
    if not order:
        return JsonResponse({'success': False, 'message': 'order not found'}, status=404)
    if not _owned_assignment(request, order.id):
        return JsonResponse({'success': False, 'message': 'Order not assigned to you'},
                            status=403)
    payment, err = payments.create_payment(
        request.courier, order, data.get('provider'),
        amount=data.get('amount'), note=data.get('note', ''),
        external_id=data.get('external_id', ''))
    if err:
        return JsonResponse({'success': False, 'message': err}, status=400)
    return JsonResponse({'payment_id': payment.id, 'status': payment.status,
                         'link': payment.link})


@csrf_exempt
@require_POST
@courier_required
def payment_refund(request, payment_id):
    """POST /payments/<id>/refund/ -> reverse the courier's own payment."""
    payment = (CourierPayment.objects.select_related('order', 'courier')
               .filter(pk=payment_id, courier=request.courier).first())
    if not payment:
        return JsonResponse({'success': False, 'message': 'payment not found'}, status=404)
    refunded, err = payments.refund_payment(payment)
    if err:
        return JsonResponse({'success': False, 'message': err}, status=409)
    return JsonResponse({'ok': True, 'status': refunded.status})


@csrf_exempt
@require_POST
def payment_webhook(request):
    """POST /payments/webhook/  — gateway-driven confirmation/reversal. Guarded
    by the shared secret in the ``X-Webhook-Secret`` header; inert (503) until
    ``COURIER_PAYMENT_WEBHOOK_SECRET`` is configured. The online-payment seam for
    when a real gateway is wired (the launch is record-only/cash)."""
    secret = getattr(settings, 'COURIER_PAYMENT_WEBHOOK_SECRET', '') or ''
    if not secret:
        return JsonResponse({'success': False, 'message': 'webhook disabled'}, status=503)
    provided = request.META.get('HTTP_X_WEBHOOK_SECRET', '')
    if not provided or not secrets.compare_digest(provided, secret):
        return JsonResponse({'success': False, 'message': 'forbidden'}, status=403)
    data, error = parse_json_body(request)
    if error:
        return JsonResponse(error[0], status=error[1])
    payment, err = payments.apply_webhook(
        external_id=(data.get('external_id') or ''),
        payment_id=data.get('payment_id'),
        status=(data.get('status') or ''),
        order_id=data.get('order_id'),
        provider=(data.get('provider') or 'QR'),
        amount=data.get('amount'))
    if err:
        return JsonResponse({'success': False, 'message': err}, status=400)
    return JsonResponse({'ok': True, 'payment_id': getattr(payment, 'id', None),
                         'status': getattr(payment, 'status', None)})
