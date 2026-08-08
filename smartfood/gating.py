"""Bot open/closed gating + active-cashier checks.

Customer endpoints return HTTP 200 with {"success": false, "closed": true,
"reason": ...} when closed, so the Mini App renders a 'closed' screen instead of
treating it as an error.
"""
from functools import wraps

from django.http import JsonResponse

from smartfood.models import BotConfig


def bot_open():
    """(is_open, reason). Closed when the operator has turned the bot OFF."""
    if not BotConfig.load().enabled:
        return False, 'bot_off'
    return True, ''


def active_cashiers():
    """On-duty cashiers: an ACTIVE shift whose user is an active CASHIER.

    Order/shift attribution in the POS is cashier_id + the shift's time window
    (no Shift FK on Order), so a dispatched order lands in that cashier's shift.
    """
    from base.models import Shift
    return (Shift.objects.filter(is_deleted=False, status='ACTIVE',
                                 user__role='CASHIER', user__status='ACTIVE')
            .select_related('user'))


def has_active_cashier():
    from base.services.presence import resolve_active_cashier
    return resolve_active_cashier() is not None


def _closed(reason):
    return JsonResponse({"success": False, "closed": True, "reason": reason}, status=200)


def require_open(view_func):
    """Block browsing/ordering when the bot is OFF."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        ok, reason = bot_open()
        if not ok:
            return _closed(reason)
        return view_func(request, *args, **kwargs)
    return wrapper


def require_open_with_cashier(view_func):
    """Order creation: bot ON and an on-shift cashier on a live POS."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        ok, reason = bot_open()
        if not ok:
            return _closed(reason)
        from base.services.presence import resolve_active_cashier
        resolved = resolve_active_cashier()
        if not resolved:
            return _closed('no_cashier')
        request.smartfood_dispatch_target = resolved
        return view_func(request, *args, **kwargs)
    return wrapper
