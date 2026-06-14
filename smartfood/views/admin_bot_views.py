"""Operator bot console (manager auth, mounted at api/admins/smartfood/).

The back-office side of Smart Food: read/flip the BotConfig singleton, watch the
PENDING queue, see who's on an active shift, and dispatch/reject each incoming
bot order. request.user is the operator (passed to the dispatch service as the
actor for the audit trail). Thin views — all the work lives in the services.
"""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.urls import path

from base.helpers.request import parse_json_body
from base.helpers.response import json_response, ServiceResponse
from base.security.permissions import manager_required

from smartfood.services.config_service import BotConfigService
from smartfood.services.dispatch_service import DispatchService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@manager_required
def config(request):
    if request.method == "GET":
        result, status = BotConfigService.get()
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = BotConfigService.update(data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@manager_required
def config_enable(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = BotConfigService.set_enabled(bool(data.get('enabled')))
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@manager_required
def pending_orders(request):
    result, status = DispatchService.pending_queue()
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@manager_required
def active_cashiers(request):
    result, status = DispatchService.active_cashiers_list()
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@manager_required
def dispatch_order(request, bot_order_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    cashier_id = data.get('cashier_id')
    if not cashier_id:
        return json_response(ServiceResponse.validation_error(
            {'cashier_id': 'cashier_id is required'}))
    result, status = DispatchService.dispatch(
        bot_order_id, cashier_id, operator=request.user)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@manager_required
def reject_order(request, bot_order_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = DispatchService.reject(
        bot_order_id, data.get('reason', ''), operator=request.user)
    return JsonResponse(result, status=status)


# Paths are relative to the mount: api/admins/smartfood/<here>.
urlpatterns = [
    path('config', config, name='bot-config'),
    path('config/enable', config_enable, name='bot-config-enable'),
    path('orders/pending', pending_orders, name='bot-orders-pending'),
    path('cashiers/active', active_cashiers, name='bot-cashiers-active'),
    path('orders/<int:bot_order_id>/dispatch', dispatch_order, name='bot-order-dispatch'),
    path('orders/<int:bot_order_id>/reject', reject_order, name='bot-order-reject'),
]
