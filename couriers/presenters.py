"""Response builders. Field names are LOAD-BEARING — the mobile app validates
every payload with zod, so an unexpected key blanks the screen (spec §0).

Read feeds: camelCase. Reconciliation + all WS event `data`: snake_case.
Money: integer so'm. Times the app renders verbatim are short display strings
("19:35", "~6 min"), not ISO.
"""
from django.utils import timezone


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def hhmm(dt):
    """A local "19:35" display string (or '' when None)."""
    if not dt:
        return ''
    return timezone.localtime(dt).strftime('%H:%M')


def so_m(value):
    """Decimal/float money -> integer so'm (never floats on the wire)."""
    if value is None:
        return 0
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return 0


def eta_ready(order, assignment):
    """Short "~6 min" string until the kitchen is done. Uses ready_at if set,
    else a coarse estimate. Returns '' once ready/past."""
    if assignment and assignment.step != 'ASSIGNED':
        return ''
    ready_at = getattr(order, 'ready_at', None)
    if ready_at:
        mins = int((ready_at - timezone.now()).total_seconds() // 60)
        return f'~{mins} min' if mins > 0 else 'Ready'
    return '~10 min'


def _bot(order):
    """The smartfood BotOrder behind this POS order, if it came from the
    customer mini-app (gives customer + address). None for in-store orders."""
    return getattr(order, 'bot_order', None)


def _customer(order):
    bot = _bot(order)
    if bot and bot.customer_id:
        return {'name': bot.customer.name, 'phone': bot.phone_number or bot.customer.phone_number}
    return {'name': '', 'phone': getattr(order, 'phone_number', '') or ''}


def _address(order, assignment):
    bot = _bot(order)
    text = (assignment.addr_text if assignment else '') or ''
    landmark = (assignment.addr_landmark if assignment else '') or ''
    lat = assignment.addr_lat if assignment else None
    lng = assignment.addr_lng if assignment else None
    dist = assignment.distance_km if assignment else None
    if bot and getattr(bot, 'address_id', None):
        addr = bot.address
        text = text or (addr.line or bot.address_text)
        if lat is None and addr.lat is not None:
            lat, lng = float(addr.lat), float(addr.lng) if addr.lng is not None else None
    elif bot and bot.address_text:
        text = text or bot.address_text
    coords = {'lat': lat, 'lng': lng} if (lat is not None and lng is not None) else None
    return {'text': text, 'landmark': landmark, 'coords': coords, 'distanceKm': dist}


def _lines(order):
    out = []
    for it in order.items.select_related('product').all():
        out.append({
            'name': getattr(it.product, 'name', '') if it.product_id else (it.detail or ''),
            'qty': it.quantity,
            'price': so_m(it.price),
        })
    return out


# --------------------------------------------------------------------------- #
# camelCase read feeds
# --------------------------------------------------------------------------- #
def courier_dict(courier):
    first = courier.first_name or getattr(courier.user, 'first_name', '')
    last = courier.last_name or getattr(courier.user, 'last_name', '')
    initials = (first[:1] + last[:1]).upper()
    return {
        'first': first, 'last': last, 'initials': initials,
        'phone': courier.phone, 'vehicle': courier.vehicle, 'plate': courier.plate,
        'id': courier.code, 'branch': courier.branch_name or courier.branch_id,
        'rating': float(courier.rating), 'online': courier.online,
    }


def active_order_dict(order, assignment):
    return {
        'id': order.id,
        'step': assignment.step if assignment else 'ASSIGNED',
        'payment': 'PAID' if order.is_paid else 'UNPAID',
        'total': so_m(order.total_amount),
        'fee': int(assignment.fee) if assignment else 0,
        'placedAt': hhmm(order.created_at),
        'etaReady': eta_ready(order, assignment),
        'customer': _customer(order),
        'address': _address(order, assignment),
        'lines': _lines(order),
    }


def completed_order_dict(order, assignment):
    delivered = assignment.delivered_at if assignment else None
    minutes = None
    if delivered and order.created_at:
        minutes = int((delivered - order.created_at).total_seconds() // 60)
    addr = _address(order, assignment)
    return {
        'id': order.id,
        'total': so_m(order.total_amount),
        'fee': int(assignment.fee) if assignment else 0,
        'payment': 'PAID' if order.is_paid else 'UNPAID',
        'deliveredAt': hhmm(delivered),
        'minutes': minutes,
        'customer': {'name': _customer(order)['name']},
        'area': (addr['landmark'] or '').strip(),
    }
