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

from base.helpers.request import parse_json_body
from base.repositories.session import SessionRepository
from base.security.hashing import verify_password, verify_password_dummy

from couriers.auth import courier_required
from couriers.models import Courier, DeliveryAssignment, PushToken
from couriers import services, presenters

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
    cash_collected = 0
    minutes_total = 0
    by_hour = {f'{h:02d}': 0 for h in range(8, 24)}
    for a in done:
        deliveries += 1
        earnings += int(a.fee or 0)
        if not a.order.is_paid or a.order.payment_method == 'CASH':
            cash_collected += presenters.so_m(a.order.total_amount)
        if a.delivered_at and a.order.created_at:
            minutes_total += int((a.delivered_at - a.order.created_at).total_seconds() // 60)
        if a.delivered_at:
            h = timezone.localtime(a.delivered_at).strftime('%H')
            by_hour[h] = by_hour.get(h, 0) + 1
    avg_minutes = int(minutes_total / deliveries) if deliveries else 0
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
        'distanceKm': 0,   # TODO: sum from location trail if you persist one
        'byHour': [{'h': str(int(h)), 'n': n} for h, n in sorted(by_hour.items())],
    })


@require_GET
@courier_required
def balance(request):
    """Cash held / payout. The real numbers come from the payments ledger
    (companion BACKEND_INTEGRATION.md §5). Until that's wired, derive cash-held
    from undelivered-but-collected orders and return the documented shape."""
    held = []
    held_total = 0
    active = (DeliveryAssignment.objects
              .filter(courier=request.courier)
              .exclude(step__in=(DeliveryAssignment.Step.DELIVERED,
                                 DeliveryAssignment.Step.DECLINED))
              .select_related('order'))
    for a in active:
        if a.order.is_paid and a.order.payment_method == 'CASH':
            amt = presenters.so_m(a.order.total_amount)
            held_total += amt
            held.append({'order': a.order_id, 'amount': amt})
    return JsonResponse({
        'balance': 0,            # TODO payments doc §5: net payable to the courier
        'heldTotal': held_total,
        'held': held,
        'ledger': [],            # TODO payments doc §5: settlement ledger rows
    })


@require_GET
@courier_required
def notifications(request):
    """Recent courier notifications. Derived from this courier's assignments;
    swap for a persisted Notification table if you add one."""
    out = []
    recent = (DeliveryAssignment.objects
              .filter(courier=request.courier)
              .select_related('order')
              .order_by('-updated_at')[:30])
    for a in recent:
        if a.step == DeliveryAssignment.Step.READY:
            out.append({'id': f'n{a.id}', 'icon': 'checkcircle', 'tone': 'success',
                        'title': f'Order #{a.order_id} is ready',
                        'body': 'Ready for pickup at the counter.',
                        'at': presenters.hhmm(a.ready_at), 'unread': True, 'order': a.order_id})
        elif a.step == DeliveryAssignment.Step.ASSIGNED:
            out.append({'id': f'n{a.id}', 'icon': 'scooter', 'tone': 'primary',
                        'title': f'New order #{a.order_id}',
                        'body': 'Assigned — kitchen is preparing.',
                        'at': presenters.hhmm(a.assigned_at), 'unread': True, 'order': a.order_id})
    return JsonResponse(out, safe=False)


@require_GET
@courier_required
def shift_reconciliation(request):
    """SNAKE_CASE on the wire (spec §3 / payments doc §5). Cash totals are the
    real numbers; QR/fees/bonuses/tips come from the payments ledger — stubbed
    until that's wired, but the shape is exact."""
    start, _ = _today_range()
    done = (DeliveryAssignment.objects
            .filter(courier=request.courier, step=DeliveryAssignment.Step.DELIVERED,
                    delivered_at__gte=start)
            .select_related('order'))
    collected_cash = 0
    delivery_fees = 0
    cash_orders = 0
    for a in done:
        delivery_fees += int(a.fee or 0)
        if a.order.payment_method == 'CASH':
            collected_cash += presenters.so_m(a.order.total_amount)
            cash_orders += 1
    started = request.courier.shift_started_at
    return JsonResponse({
        'collected_cash': collected_cash,
        'qr_collected': 0,        # TODO payments doc §5
        'delivery_fees': delivery_fees,
        'bonuses': 0,             # TODO
        'tips': 0,                # TODO
        'cash_orders': cash_orders,
        'qr_orders': 0,           # TODO
        'shift_start': presenters.hhmm(started),
        'handover_code': f'ALP-{request.courier.id:04d}',
        'net_payout': delivery_fees,   # TODO payments doc §5
        'cash_in_hand': collected_cash,
    })


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
def shift_settle(request):
    """Clears cash-in-hand at handover. Real settlement -> payments doc §5."""
    # TODO payments doc §5: write the settlement ledger row + reset held cash.
    return JsonResponse({'ok': True})


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
