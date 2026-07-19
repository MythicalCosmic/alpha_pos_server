"""Courier request authentication.

The mobile app sends ``Authorization: Token <key>`` (it stores the key in
secure-store). The rest of this server resolves a session from
``Authorization: Bearer <key>`` or a ``session_key`` cookie. This module bridges
the two: it accepts ``Token``, ``Bearer`` or the cookie, resolves the session
(SHA-256 hashed in ``Session.payload``) to a staff ``base.User``, then to that
user's ``Courier`` profile.

``@courier_required`` mirrors ``base.security.permissions.admin_required`` but
gates on "has a Courier profile" instead of a role, and sets:
  request.user          -> base.User
  request.courier       -> couriers.Courier
  request.session_key   -> raw token
"""
from functools import wraps

from django.http import JsonResponse

from base.repositories.session import SessionRepository


def get_courier_token(request):
    """Extract the raw session token from the request. Accepts the courier
    app's ``Token`` scheme, the server-wide ``Bearer`` scheme, or the cookie."""
    cookie = request.COOKIES.get('session_key')
    if cookie:
        return cookie
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    for prefix in ('Token ', 'Bearer '):
        if auth.startswith(prefix):
            return auth[len(prefix):].strip()
    return None


def resolve_courier(request):
    """Return (user, courier, token) for a valid session, else (None, None, None)."""
    token = get_courier_token(request)
    if not token:
        return None, None, None
    session = SessionRepository.get_by_session_key(token)
    if not session or not session.user_id or session.user_id.is_deleted:
        return None, None, None
    if session.is_expired():
        SessionRepository.invalidate_cache(token)
        return None, None, None
    user = session.user_id
    if getattr(user, 'status', 'ACTIVE') != 'ACTIVE':
        return None, None, None
    courier = getattr(user, 'courier', None)
    return user, courier, token


def logout_session(token):
    """Invalidate the access token and its refresh family. Idempotent."""
    from couriers.tokens import revoke_access_token
    revoke_access_token(token)


def courier_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user, courier, token = resolve_courier(request)
        if user is None:
            return JsonResponse(
                {'success': False, 'message': 'Authentication required'}, status=401,
            )
        if courier is None:
            return JsonResponse(
                {'success': False, 'message': 'No courier profile for this account'},
                status=403,
            )
        request.user = user
        request.courier = courier
        request.session_key = token
        return view_func(request, *args, **kwargs)
    return wrapper
