"""Courier request authentication.

The mobile app sends ``Authorization: Token <key>`` (it stores the key in
secure-store). The rest of this server resolves a session from
``Authorization: Bearer <key>`` or a ``session_key`` cookie. This module bridges
the two: it accepts ``Token``, ``Bearer`` or the cookie, resolves the session
(SHA-256 hashed in ``Session.payload``) to a staff ``base.User``, then to that
user's ``Courier`` profile.

``@courier_required`` mirrors ``base.security.permissions.admin_required`` but
requires both the dedicated COURIER role and its Courier profile, and sets:
  request.user          -> base.User
  request.courier       -> couriers.Courier
  request.session_key   -> raw token
"""
from functools import wraps

from django.http import JsonResponse

from base.helpers.request import SessionCredentialConflict
from base.models import User
from base.repositories.session import SessionRepository
from base.security.auth import session_credential_conflict_response


def get_courier_token(request):
    """Extract the raw session token from the request. Accepts the courier
    app's ``Token`` scheme, the server-wide ``Bearer`` scheme, or the cookie."""
    cookie = request.COOKIES.get('session_key')
    cookie = cookie if isinstance(cookie, str) and cookie else None
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    header = None
    if isinstance(auth, str):
        scheme, separator, value = auth.partition(' ')
        if separator and scheme.lower() in ('token', 'bearer'):
            header = value.strip() or None
    if cookie and header and cookie != header:
        raise SessionCredentialConflict()
    return cookie or header


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
    # The profile alone is not an authentication audience.  A legacy role
    # drift to CASHIER/MANAGER must never let that staff bearer enter the
    # courier mobile API (and the reverse is blocked by the core POS gates).
    if user.role != User.RoleChoices.COURIER:
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
        try:
            user, courier, token = resolve_courier(request)
        except SessionCredentialConflict:
            return session_credential_conflict_response()
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
