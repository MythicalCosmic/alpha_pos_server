from datetime import datetime

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from base.helpers.request import parse_json_body, safe_page, safe_per_page
from base.helpers.response import json_response
from base.security.permissions import manager_required, pos_staff_required
from base.security.audit import audit
from base.models import AuditLog
from admins.services.shift_service import ShiftTemplateService, ShiftService


def _truthy(value):
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')


@csrf_exempt
@require_http_methods(["GET", "POST"])
@manager_required
def shift_templates(request):
    if request.method == "GET":
        result, status_code = ShiftTemplateService.list()
        return JsonResponse(result, status=status_code)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    start_time_str = data.get('start_time')
    end_time_str = data.get('end_time')
    start_time = datetime.strptime(start_time_str, '%H:%M').time() if start_time_str else None
    end_time = datetime.strptime(end_time_str, '%H:%M').time() if end_time_str else None

    result, status_code = ShiftTemplateService.create(
        name=data.get('name'),
        start_time=start_time,
        end_time=end_time,
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@manager_required
def shift_template_detail(request, template_id):
    if request.method == "GET":
        result, status_code = ShiftTemplateService.get(template_id)
        return JsonResponse(result, status=status_code)

    if request.method == "DELETE":
        result, status_code = ShiftTemplateService.delete(template_id)
        return JsonResponse(result, status=status_code)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = ShiftTemplateService.update(
        template_id,
        name=data.get('name'),
        start_time=data.get('start_time'),
        end_time=data.get('end_time'),
        is_active=data.get('is_active'),
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@manager_required
def shifts(request):
    page = safe_page(request)
    per_page = safe_per_page(request, 20)
    # cashier_id is the v3 Shifts-page filter name; user_id kept as an alias.
    user_id = request.GET.get('cashier_id') or request.GET.get('user_id')
    status = request.GET.get('statuses') or request.GET.get('status')
    date_from = request.GET.get('date_from') or request.GET.get('from')
    date_to = request.GET.get('date_to') or request.GET.get('to')
    live_only = _truthy(request.GET.get('live_only'))
    closed_only = _truthy(request.GET.get('closed_only'))
    state = (request.GET.get('state') or '').strip().lower()
    if state == 'live':
        live_only, closed_only = True, False
    elif state == 'closed':
        live_only, closed_only = False, True

    order_by = request.GET.get('order_by')
    if not order_by:
        sort_by = (request.GET.get('sort_by') or 'start_time').strip()
        sort_order = (request.GET.get('sort_order') or 'desc').strip().lower()
        order_by = f'-{sort_by}' if sort_order == 'desc' else sort_by

    result, status_code = ShiftService.list(
        page=page,
        per_page=per_page,
        user_id=user_id,
        status=status,
        date_from=date_from,
        date_to=date_to,
        datetime_from=request.GET.get('datetime_from'),
        datetime_to=request.GET.get('datetime_to'),
        from_at=request.GET.get('from_at'),
        to_at=request.GET.get('to_at'),
        tod_from=request.GET.get('tod_from'),
        tod_to=request.GET.get('tod_to'),
        live_only=live_only,
        closed_only=closed_only,
        order_by=order_by,
        actor=request.user,
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@pos_staff_required
def shift_detail(request, shift_id):
    # pos_staff so a cashier can see their own shift's stats, not just managers.
    result, status_code = ShiftService.get(shift_id, actor=request.user)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@pos_staff_required
def shift_start(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    user_id = data.get('user_id')
    if not user_id:
        return JsonResponse(
            {"success": False, "message": "user_id is required"},
            status=400,
        )

    result, status_code = ShiftService.start_shift(
        user_id=user_id,
        shift_template_id=data.get('shift_template_id'),
        actor=request.user,
        branch_id=data.get('branch_id'),
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@pos_staff_required
def shift_end(request, shift_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = ShiftService.end_shift(
        shift_id=shift_id,
        user_id=request.user.id,
        notes=data.get('notes', ''),
        actor=request.user,
        # {method: counted_amount} from the cashier's blind per-type count.
        counted=data.get('counted'),
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@manager_required
def shift_reconcile(request, shift_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    actual_cash = data.get('actual_cash')
    if actual_cash is None:
        return JsonResponse(
            {"success": False, "message": "actual_cash is required"},
            status=400,
        )

    result, status_code = ShiftService.reconcile(
        shift_id=shift_id,
        actual_cash=actual_cash,
        notes=data.get('notes', ''),
        reconciled_by_id=request.user.id,
        actor=request.user,
        # {method: confirmed_amount} the manager accepts; defaults per method to
        # the cashier's counted figure. Every confirmed tender posts to SAFE.
        confirmed=data.get('confirmed'),
    )
    if result.get('success'):
        payload = result.get('data', {})
        audit(
            request,
            AuditLog.Action.SHIFT_RECONCILE,
            target_type='Shift',
            target_id=shift_id,
            metadata={
                'expected_cash': payload.get('expected_cash'),
                'actual_cash': payload.get('actual_cash'),
                'difference': payload.get('difference'),
            },
        )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@manager_required
def active_shifts(request):
    result, status_code = ShiftService.get_active_shifts(actor=request.user)
    return JsonResponse(result, status=status_code)
