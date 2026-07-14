from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from base.helpers.request import parse_json_body, safe_page, safe_per_page, safe_int, coerce_quantity
from base.helpers.response import json_response
from base.security.permissions import admin_required, permission_required
from base.security.audit import audit
from base.security.idempotency import idempotent
from base.models import AuditLog
from admins.services.order_service import AdminOrderService
from admins.requests.order_requests import create_order_request, update_order_request


def _check_permission(request, perm):
    user_perms = request.user.permissions or []
    # ADMIN role has every permission (matches base.security.permissions
    # .permission_required) — without this an admin without '*' in their
    # permissions list got a 403 on create/edit.
    if '*' in user_perms or getattr(request.user, 'role', None) == 'ADMIN':
        return None
    if perm not in user_perms:
        return JsonResponse(
            {"success": False, "message": "You don't have permission to perform this action"},
            status=403,
        )
    return None


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
@idempotent('orders.create')
def orders(request):
    if request.method == "GET":
        page = safe_page(request)
        per_page = safe_per_page(request, 20)
        statuses = request.GET.get('statuses')
        payment_status = request.GET.get('payment_status')
        category_ids = request.GET.get('category_ids')
        user_id = request.GET.get('user_id')
        cashier_id = request.GET.get('cashier_id')
        order_type = request.GET.get('order_type')
        date_from = request.GET.get('date_from')
        date_to = request.GET.get('date_to')
        order_by = request.GET.get('order_by', '-created_at')
        include_deleted = request.GET.get('include_deleted', '').lower() == 'true'
        # Default true (back-compat: item 5 inline items); ?include_items=false
        # drops items[] from the list payload for lighter list-view fetches (item 14).
        include_items = request.GET.get('include_items', 'true').lower() != 'false'
        product_ids = request.GET.get('product_ids')
        tod_from = request.GET.get('tod_from')
        tod_to = request.GET.get('tod_to')

        result, status_code = AdminOrderService.get_all_orders(
            page=page, per_page=per_page, statuses=statuses,
            payment_status=payment_status, category_ids=category_ids,
            product_ids=product_ids, user_id=user_id, cashier_id=cashier_id,
            order_type=order_type, date_from=date_from, date_to=date_to,
            order_by=order_by, include_deleted=include_deleted,
            include_items=include_items, tod_from=tod_from, tod_to=tod_to,
        )
        return JsonResponse(result, status=status_code)

    denied = _check_permission(request, 'order.create')
    if denied:
        return denied

    data, error = create_order_request(request)
    if error:
        return json_response(error)

    result, status_code = AdminOrderService.create_order(
        user_id=data['user_id'],
        items=data['items'],
        order_type=data.get('order_type', 'HALL'),
        phone_number=data.get('phone_number'),
        description=data.get('description'),
        cashier_id=data.get('cashier_id'),
        delivery_person_id=data.get('delivery_person_id'),
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "PUT", "PATCH", "DELETE"])
@admin_required
def order_detail(request, order_id):
    if request.method == "GET":
        include_deleted = request.GET.get('include_deleted', '').lower() == 'true'
        result, status_code = AdminOrderService.get_order_by_id(order_id, include_deleted)
        return JsonResponse(result, status=status_code)

    if request.method == "DELETE":
        denied = _check_permission(request, 'order.delete')
        if denied:
            return denied
        hard = request.GET.get('hard', '').lower() == 'true'
        result, status_code = AdminOrderService.delete_order(order_id, hard)
        return JsonResponse(result, status=status_code)

    denied = _check_permission(request, 'order.update')
    if denied:
        return denied
    data, error = update_order_request(request)
    if error:
        return json_response(error)
    result, status_code = AdminOrderService.update_order(order_id, **data)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@admin_required
@permission_required('order.update')
def add_item(request, order_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    product_id = data.get('product_id')
    quantity = coerce_quantity(data.get('quantity', 1))

    if not product_id:
        return json_response(({
            "success": False, "message": "Missing product_id",
            "errors": {"product_id": "product_id is required"},
        }, 422))

    if quantity is None:
        return json_response(({
            "success": False, "message": "Invalid quantity",
            "errors": {"quantity": "Must be a positive integer"},
        }, 422))

    result, status_code = AdminOrderService.add_item_to_order(order_id, product_id, quantity)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["PATCH"])
@admin_required
@permission_required('order.update')
def update_item(request, order_id, item_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    quantity = coerce_quantity(data.get('quantity'))
    if quantity is None:
        return json_response(({
            "success": False, "message": "Invalid quantity",
            "errors": {"quantity": "Must be a positive integer"},
        }, 422))

    result, status_code = AdminOrderService.update_order_item(order_id, item_id, quantity)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["DELETE"])
@admin_required
@permission_required('order.update')
def remove_item(request, order_id, item_id):
    result, status_code = AdminOrderService.remove_item_from_order(order_id, item_id)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["PATCH"])
@admin_required
@permission_required('order.update')
@idempotent('orders.status')
def update_status(request, order_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    status = data.get('status')
    if not status:
        return json_response(({
            "success": False, "message": "Missing status",
            "errors": {"status": "status is required"},
        }, 422))

    result, status_code = AdminOrderService.update_order_status(
        order_id, status, cashier_id=request.user.id,
        reason=(data.get('reason') or '')[:255],
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@admin_required
@permission_required('order.update')
@idempotent('orders.pay')
def pay_order(request, order_id):
    """POST /orders/<id>/pay  {payment_method} | {payments:[{method,amount},...]}

    Either a single tender, or an explicit split (which is how a MIXED sale is
    recorded — MIXED is never a valid bare `payment_method`). Both write the
    OrderPayment tender lines, so the sale is visible to shift settlement.
    """
    payment_method, payments = 'CASH', None
    if request.body:
        from base.helpers.request import parse_json_body
        body, _ = parse_json_body(request)
        if body:
            payment_method = body.get('payment_method', 'CASH')
            payments = body.get('payments')
    result, status_code = AdminOrderService.mark_as_paid(
        order_id, payment_method=payment_method, payments=payments,
        cashier_id=request.user.id)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@admin_required
@permission_required('order.update')
@idempotent('orders.unpay')
def unpay_order(request, order_id):
    result, status_code = AdminOrderService.mark_as_unpaid(
        order_id, cashier_id=request.user.id,
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@admin_required
@permission_required('order.update')
def mark_ready(request, order_id):
    result, status_code = AdminOrderService.mark_order_ready(order_id)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@admin_required
@permission_required('order.update')
def mark_item_ready(request, order_id, item_id):
    result, status_code = AdminOrderService.mark_item_ready(order_id, item_id)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@admin_required
@permission_required('order.update')
def unmark_item_ready(request, order_id, item_id):
    result, status_code = AdminOrderService.unmark_item_ready(order_id, item_id)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@admin_required
@permission_required('order.update')
@idempotent('orders.cancel')
def cancel_order(request, order_id):
    reason = ''
    if request.body:
        body, _ = parse_json_body(request)
        if body:
            reason = (body.get('reason') or '')[:255]
    result, status_code = AdminOrderService.update_order_status(
        order_id, 'CANCELED', cashier_id=request.user.id, reason=reason,
    )
    if result.get('success'):
        audit(
            request,
            AuditLog.Action.ORDER_CANCEL,
            target_type='Order',
            target_id=order_id,
        )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@admin_required
@permission_required('order.update')
def restore_order(request, order_id):
    result, status_code = AdminOrderService.restore_order(order_id)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@admin_required
@permission_required('order.stats')
def order_stats(request):
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    cashier_id = request.GET.get('cashier_id')
    result, status_code = AdminOrderService.get_order_stats(
        date_from, date_to, cashier_id,
        product_ids=request.GET.get('product_ids'),
        tod_from=request.GET.get('tod_from'), tod_to=request.GET.get('tod_to'))
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@admin_required
@permission_required('order.stats')
def daily_stats(request):
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    cashier_id = request.GET.get('cashier_id')
    result, status_code = AdminOrderService.get_daily_stats(
        date_from, date_to, cashier_id,
        tod_from=request.GET.get('tod_from'), tod_to=request.GET.get('tod_to'))
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@admin_required
@permission_required('order.stats')
def monthly_stats(request):
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    result, status_code = AdminOrderService.get_monthly_stats(date_from, date_to)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@admin_required
@permission_required('order.stats')
def yearly_stats(request):
    result, status_code = AdminOrderService.get_yearly_stats()
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@admin_required
@permission_required('order.stats')
def cashier_stats(request):
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    result, status_code = AdminOrderService.get_cashier_stats(date_from, date_to)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@admin_required
@permission_required('order.stats')
def status_stats(request):
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    result, status_code = AdminOrderService.get_status_stats(date_from, date_to)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@admin_required
@permission_required('order.stats')
def order_type_stats(request):
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    result, status_code = AdminOrderService.get_order_type_stats(date_from, date_to)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@admin_required
@permission_required('order.stats')
def top_products(request):
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    limit = safe_int(request, 'limit', 20, minimum=1, maximum=100)
    result, status_code = AdminOrderService.get_top_products(date_from, date_to, limit)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@admin_required
@permission_required('order.stats')
def least_sold_products(request):
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    limit = safe_int(request, 'limit', 20, minimum=1, maximum=100)
    result, status_code = AdminOrderService.get_least_sold_products(date_from, date_to, limit)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@admin_required
@permission_required('order.stats')
def category_stats(request):
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    result, status_code = AdminOrderService.get_category_stats(date_from, date_to)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@admin_required
@permission_required('order.stats')
def hourly_stats(request):
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    result, status_code = AdminOrderService.get_hourly_stats(
        date_from, date_to,
        tod_from=request.GET.get('tod_from'), tod_to=request.GET.get('tod_to'))
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@admin_required
@permission_required('order.stats')
def dashboard_stats(request):
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    result, status_code = AdminOrderService.get_dashboard_stats(
        date_from, date_to,
        tod_from=request.GET.get('tod_from'), tod_to=request.GET.get('tod_to'))
    return JsonResponse(result, status=status_code)
