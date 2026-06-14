"""Customer auth: Telegram initData login, logout, and profile (me)."""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods
from django.urls import path

from base.helpers.request import parse_json_body, get_user_agent, get_client_ip
from base.helpers.response import json_response, ServiceResponse
from smartfood.security import customer_required
from smartfood.serializers import customer_dict
from smartfood.services.auth_service import CustomerAuthService


@csrf_exempt
@require_POST
def auth(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = CustomerAuthService.login_with_init_data(
        data.get('init_data', ''), get_user_agent(request), get_client_ip(request))
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@customer_required
def logout(request):
    result, status = CustomerAuthService.logout(request.customer_session)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "PATCH"])
@customer_required
def me(request):
    if request.method == "GET":
        return json_response(ServiceResponse.success(customer_dict(request.customer)))
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = CustomerAuthService.update_profile(
        request.customer,
        name=data.get('name'),
        phone=data.get('phone'),
        language=data.get('language'),
    )
    return JsonResponse(result, status=status)


urlpatterns = [
    path('auth', auth, name='auth'),
    path('auth/logout', logout, name='auth-logout'),
    path('me', me, name='me'),
]
