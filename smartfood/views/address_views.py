"""Customer addresses + Yandex geo proxy (Telegram Mini App).

Mounted under api/smartfood/ — every endpoint requires a customer session.
"""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.urls import path

from base.helpers.request import parse_json_body, safe_int
from base.helpers.response import json_response
from smartfood.security import customer_required
from smartfood.services.address_service import AddressService


@csrf_exempt
@require_http_methods(['GET', 'POST'])
@customer_required
def addresses(request):
    if request.method == 'GET':
        result, status = AddressService.list_for(request.customer)
        return JsonResponse(result, status=status)
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = AddressService.create(request.customer, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(['PUT', 'DELETE'])
@customer_required
def address_detail(request, address_id):
    if request.method == 'DELETE':
        result, status = AddressService.delete(request.customer, address_id)
        return JsonResponse(result, status=status)
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = AddressService.update(request.customer, address_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(['PUT'])
@customer_required
def address_set_default(request, address_id):
    result, status = AddressService.set_default(request.customer, address_id)
    return JsonResponse(result, status=status)


@require_GET
@customer_required
def geo_reverse(request):
    lang = request.GET.get('lang') or request.customer.language or 'uz'
    result, status = AddressService.geocode_reverse(
        request.GET.get('lat'), request.GET.get('lng'), lang=lang)
    return JsonResponse(result, status=status)


@require_GET
@customer_required
def geo_forward(request):
    lang = request.GET.get('lang') or request.customer.language or 'uz'
    limit = safe_int(request, 'limit', default=5, minimum=1, maximum=20)
    result, status = AddressService.geocode_forward(
        request.GET.get('q', ''), lang=lang, limit=limit)
    return JsonResponse(result, status=status)


urlpatterns = [
    path('addresses', addresses),
    path('addresses/<int:address_id>', address_detail),
    path('addresses/<int:address_id>/default', address_set_default),
    path('geo/reverse', geo_reverse),
    path('geo/forward', geo_forward),
]
