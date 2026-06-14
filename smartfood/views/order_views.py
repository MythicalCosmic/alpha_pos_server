"""Customer orders: create + list (active|history), detail, cancel.

GET /orders lists; POST /orders creates. Create additionally requires an
on-duty cashier (require_open_with_cashier), so the two methods can't share a
single decorator stack — they are dispatched as separate inner handlers.
"""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.urls import path

from base.helpers.request import parse_json_body
from base.helpers.response import json_response
from smartfood.security import customer_required
from smartfood.gating import require_open_with_cashier
from smartfood.services.order_service import BotOrderService


@require_POST
@require_open_with_cashier
def _create_order(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = BotOrderService.create(
        request.customer,
        data.get('items'),
        order_type=data.get('order_type', 'DELIVERY'),
        address_id=data.get('address_id'),
        phone=data.get('phone', ''),
        note=data.get('note', ''),
        tip=data.get('tip', 0),
        points_used=data.get('points_used', 0),
        payment_method=data.get('payment_method', 'CASH'),
        lang=request.customer.language,
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "POST"])
@customer_required
def orders(request):
    if request.method == "POST":
        return _create_order(request)
    result, status = BotOrderService.list_for(request.customer, request.GET.get('status'))
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@customer_required
def order_detail(request, order_id):
    result, status = BotOrderService.get_for(request.customer, order_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@customer_required
def cancel_order(request, order_id):
    result, status = BotOrderService.cancel(request.customer, order_id)
    return JsonResponse(result, status=status)


urlpatterns = [
    path('orders', orders, name='orders'),
    path('orders/<int:order_id>', order_detail, name='order-detail'),
    path('orders/<int:order_id>/cancel', cancel_order, name='order-cancel'),
]
