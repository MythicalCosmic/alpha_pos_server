"""Owner mobile app: session refresh and push-device registration."""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from admins.services import owner_push
from admins.services.auth_service import AdminAuthService
from base.helpers.request import get_client_ip, get_user_agent, parse_json_body
from base.helpers.response import ServiceResponse, json_response
from base.repositories import SessionRepository
from base.security.permissions import admin_required, backoffice_required
from base.security.rate_limit import rate_limit

DEVICE_FIELDS = {'token', 'platform', 'app_version', 'locale', 'prefs'}


def _unknown_fields(data, allowed):
    unknown = sorted(set(data) - allowed)
    if unknown:
        return json_response(ServiceResponse.validation_error({field: ['Unknown field.'] for field in unknown}))
    return None


@csrf_exempt
@rate_limit('admin_refresh', 20, 60)
@backoffice_required
@require_POST
def auth_refresh(request):
    result, status = AdminAuthService.refresh(
        request.session_key, get_client_ip(request), get_user_agent(request),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@admin_required
@require_http_methods(['GET', 'POST', 'DELETE'])
def devices(request):
    if request.method == 'GET':
        result, status = owner_push.list_devices(user=request.user)
        return JsonResponse(result, status=status)
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    if request.method == 'DELETE':
        if denied := _unknown_fields(data, {'token'}):
            return denied
        result, status = owner_push.unregister_device(user=request.user, token=data.get('token'))
        return JsonResponse(result, status=status)
    if denied := _unknown_fields(data, DEVICE_FIELDS):
        return denied
    session = SessionRepository.get_by_session_key(request.session_key)
    result, status = owner_push.register_device(
        user=request.user, session=session,
        token=data.get('token'), platform=data.get('platform'),
        app_version=data.get('app_version', ''), locale=data.get('locale', 'uz'),
        prefs=data.get('prefs'),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@admin_required
@require_http_methods(['PATCH'])
def device_detail(request, device_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    if denied := _unknown_fields(data, {'prefs', 'locale'}):
        return denied
    result, status = owner_push.update_device(
        user=request.user, device_id=device_id, prefs=data.get('prefs'), locale=data.get('locale'),
    )
    return JsonResponse(result, status=status)
