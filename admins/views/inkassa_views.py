from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from base.helpers.request import parse_json_body, validate_pagination
from base.helpers.response import json_response
from base.security.permissions import manager_required
from base.security.auth import login_required
from base.security.audit import audit
from base.security.idempotency import idempotent
from base.models import AuditLog
from admins.services.inkassa_service import AdminInkassaService


@csrf_exempt
@require_GET
@login_required
def inkassa_balance(request):
    result, status_code = AdminInkassaService.get_balance()
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@login_required
def inkassa_stats(request):
    result, status_code = AdminInkassaService.get_stats()
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@manager_required
def inkassa_history(request):
    page, per_page = validate_pagination(request)
    result, status_code = AdminInkassaService.get_history(page=page, per_page=per_page)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@manager_required
def inkassa_detail(request, inkassa_id):
    result, status_code = AdminInkassaService.get_detail(inkassa_id)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@manager_required
@idempotent('inkassa.perform')
def inkassa_perform(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = AdminInkassaService.perform(request.user, data)
    if result.get('success'):
        payload = result.get('data', {})
        audit(
            request,
            AuditLog.Action.INKASSA_PERFORM,
            target_type='CashRegister',
            metadata={
                'amount_removed': payload.get('amount_removed'),
                'balance_before': payload.get('balance_before'),
                'balance_after': payload.get('balance_after'),
                'inkassa_ids': [i.get('id') for i in payload.get('inkassas', [])],
            },
        )
    return JsonResponse(result, status=status_code)
