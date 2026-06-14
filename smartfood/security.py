"""Customer auth for the Telegram Mini App.

- verify_init_data: validate Telegram WebApp `initData` via the documented
  HMAC-SHA256 scheme (constant-time), reject stale launch data.
- customer_required: authenticate a Customer by Bearer/cookie token (the token
  is matched by its SHA-256 digest, mirroring base.login_required).
Operator endpoints reuse base.security.permissions.manager_required.
"""
import hashlib
import hmac
import json
import time
from functools import wraps
from urllib.parse import parse_qsl

from django.conf import settings
from django.http import JsonResponse

from base.helpers.request import get_session_key
from smartfood.repositories import CustomerSessionRepository

AUTH_TTL_DEFAULT = 24 * 3600        # issued bearer SESSION lifetime
INITDATA_MAX_AGE_DEFAULT = 3600     # initData freshness/replay window (NOT the session lifetime)


def _auth_ttl():
    raw = getattr(settings, 'SMARTFOOD_AUTH_TTL', None)
    if raw is None:
        return AUTH_TTL_DEFAULT
    try:
        return int(raw)
    except (TypeError, ValueError):
        return AUTH_TTL_DEFAULT


def _initdata_max_age():
    raw = getattr(settings, 'SMARTFOOD_INITDATA_MAX_AGE', None)
    if raw is None:
        return INITDATA_MAX_AGE_DEFAULT
    try:
        return int(raw)
    except (TypeError, ValueError):
        return INITDATA_MAX_AGE_DEFAULT


def verify_init_data(init_data, bot_token=None, max_age=None):
    """Return the parsed Telegram `user` dict if `init_data` is authentic, else None.

    Algorithm (Telegram WebApp): build a data_check_string of all fields except
    `hash`, sorted by key, joined by '\\n'; secret = HMAC_SHA256('WebAppData',
    bot_token); valid iff HMAC_SHA256(secret, data_check_string) == hash.
    """
    bot_token = bot_token if bot_token is not None else (getattr(settings, 'CUSTOMER_BOT_TOKEN', '') or '')
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    received_hash = pairs.pop('hash', None)
    if not received_hash:
        return None
    data_check_string = '\n'.join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b'WebAppData', bot_token.encode('utf-8'), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check_string.encode('utf-8'), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        return None
    # Replay guard: a legitimate Telegram initData always carries auth_date, so
    # reject when it's missing/zero (do NOT fail open) and reject stale launch data
    # within a SHORT freshness window (distinct from the issued session lifetime).
    ttl = _initdata_max_age() if max_age is None else max_age
    try:
        auth_date = int(pairs.get('auth_date', '0'))
    except (TypeError, ValueError):
        auth_date = 0
    if auth_date <= 0:
        return None
    if ttl and (time.time() - auth_date) > ttl:
        return None
    user_raw = pairs.get('user')
    if not user_raw:
        return None
    try:
        user = json.loads(user_raw)
    except (ValueError, TypeError):
        return None
    return user if isinstance(user, dict) and user.get('id') is not None else None


def customer_required(view_func):
    """Authenticate request.customer via a Bearer/cookie token."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        token = get_session_key(request)
        if not token:
            return JsonResponse({"success": False, "message": "Authentication required"}, status=401)
        session = CustomerSessionRepository.get_by_token(token)
        if not session or session.is_expired():
            return JsonResponse({"success": False, "message": "Invalid or expired session"}, status=401)
        customer = session.customer
        if customer is None or customer.is_blocked:
            return JsonResponse({"success": False, "message": "Account blocked"}, status=403)
        request.customer = customer
        request.customer_session = session
        return view_func(request, *args, **kwargs)
    return wrapper
