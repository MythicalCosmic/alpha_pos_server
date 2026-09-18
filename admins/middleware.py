"""Server-enforced read-only access for store-review accounts.

App Store / Google Play reviewers sign in to the real system (owner decision,
2026-09-18). Their user carries the ``app.review_readonly`` permission flag:
they can open every screen, but any change — approving or paying an expense,
editing anything — is refused here, before any view runs.
"""
from django.http import JsonResponse

from base.helpers.request import resolve_session_credential
from base.repositories import SessionRepository

READ_ONLY_FLAG = 'app.review_readonly'
SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})
ALLOWED_PATHS = frozenset({
    '/api/admins/auth-login',
    '/api/admins/auth-logout',
    '/api/admins/auth-refresh',
    '/api/admins/devices',
})


class ReviewReadOnlyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method not in SAFE_METHODS and request.path.startswith('/api/') \
                and request.path.rstrip('/') not in ALLOWED_PATHS and self._is_reviewer(request):
            return JsonResponse({
                'success': False,
                'code': 'REVIEW_READ_ONLY',
                'message': 'This review account can view data but cannot change anything.',
            }, status=403)
        return self.get_response(request)

    @staticmethod
    def _is_reviewer(request):
        try:
            session_key, _ = resolve_session_credential(request)
        except Exception:  # noqa: BLE001 — conflicting credentials are rejected by the view decorators
            return False
        if not session_key:
            return False
        session = SessionRepository.get_by_session_key(session_key)
        user = getattr(session, 'user_id', None) if session else None
        return bool(user and READ_ONLY_FLAG in (user.permissions or []))
