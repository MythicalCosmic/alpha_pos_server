from django.conf import settings
from django.http import HttpResponseNotFound
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


_CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'none'",
    "base-uri 'none'",
    "connect-src 'self'",
    "font-src 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "img-src 'self'",
    "manifest-src 'none'",
    "media-src 'none'",
    "object-src 'none'",
    "script-src 'self'",
    "style-src 'self'",
    "worker-src 'none'",
))


@never_cache
@require_GET
def legacy_dashboard(request):
    """Serve a data-free compatibility shell over the canonical admin APIs."""
    if not getattr(settings, 'LEGACY_COMPAT_DASHBOARD_ENABLED', False):
        return HttpResponseNotFound()

    response = render(request, 'admins/legacy_dashboard.html')
    response['Cache-Control'] = (
        'private, no-store, no-cache, max-age=0, must-revalidate'
    )
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'
    response['Content-Security-Policy'] = _CONTENT_SECURITY_POLICY
    response['Cross-Origin-Resource-Policy'] = 'same-origin'
    response['Permissions-Policy'] = (
        'camera=(), geolocation=(), microphone=(), payment=(), usb=()'
    )
    return response
