from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET, require_http_methods
from base.helpers.request import get_client_ip, get_user_agent
from base.helpers.response import json_response
from base.helpers.cookie import set_session_cookie, clear_session_cookie
from base.security.rate_limit import rate_limit, rate_limit_by
from base.security.permissions import admin_required
from admins.services.auth_service import AdminAuthService
from admins.requests.auth_requests import (
    login_request,
    change_password_request,
    revoke_session_request,
)


def _login_email(request):
    # Best-effort: don't 500 the throttle if the body is malformed.
    try:
        import json
        body = json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        return None
    email = body.get('email') if isinstance(body, dict) else None
    return (email or '').strip().lower()[:128] or None


@csrf_exempt
@rate_limit('admin_login', 5, 60)
@rate_limit_by('admin_login_user', 5, 60, _login_email)
@require_POST
def login(request):
    data, error = login_request(request)
    if error:
        return json_response(error)

    result, status = AdminAuthService.login(
        email=data['email'],
        password=data['password'],
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )

    response = JsonResponse(result, status=status)

    token = result.get('data', {}).get('token')
    if result.get('success') and token:
        set_session_cookie(response, token)

    return response


@csrf_exempt
@rate_limit('admin_logout', 10, 60)
@admin_required
@require_POST
def logout(request):
    result, status = AdminAuthService.logout(request.session_key)
    response = JsonResponse(result, status=status)

    if result.get('success'):
        clear_session_cookie(response)

    return response


@csrf_exempt
@rate_limit('admin_logout_all', 5, 60)
@admin_required
@require_POST
def logout_all(request):
    result, status = AdminAuthService.logout_all(request.session_key)
    response = JsonResponse(result, status=status)

    if result.get('success'):
        clear_session_cookie(response)

    return response


@csrf_exempt
@admin_required
@require_GET
def me(request):
    result, status = AdminAuthService.me(request.session_key)
    return JsonResponse(result, status=status)


@csrf_exempt
@rate_limit('admin_change_password', 3, 60)
@admin_required
@require_POST
def change_password(request):
    data, error = change_password_request(request)
    if error:
        return json_response(error)

    result, status = AdminAuthService.change_password(
        session_key=request.session_key,
        current_password=data['current_password'],
        new_password=data['new_password'],
    )

    return JsonResponse(result, status=status)


@csrf_exempt
@admin_required
@require_http_methods(["GET", "DELETE"])
def sessions(request):
    if request.method == "GET":
        result, status = AdminAuthService.get_active_sessions(request.session_key)
        return JsonResponse(result, status=status)

    data, error = revoke_session_request(request)
    if error:
        return json_response(error)

    result, status = AdminAuthService.revoke_session(request.session_key, data['session_id'])
    return JsonResponse(result, status=status)
