from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from base.helpers.request import parse_json_body, validate_pagination
from base.helpers.response import json_response
from base.security.permissions import manager_required
from base.security.auth import login_required
from base.security.audit import audit
from base.models import AuditLog
from admins.services.inkassa_service import AdminInkassaService


@csrf_exempt
@require_GET
@login_required
def inkassa_balance(request):
    result, status_code = AdminInkassaService.get_balance(
        branch_id=(request.GET.get('branch_id') or '').strip() or None,
        actor=request.user,
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@login_required
def inkassa_stats(request):
    result, status_code = AdminInkassaService.get_stats(
        branch_id=(request.GET.get('branch_id') or '').strip() or None,
        actor=request.user,
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@manager_required
def inkassa_history(request):
    page, per_page = validate_pagination(request)
    result, status_code = AdminInkassaService.get_history(
        page=page, per_page=per_page,
        branch_id=(request.GET.get('branch_id') or '').strip() or None,
        actor=request.user,
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@manager_required
def inkassa_detail(request, inkassa_id):
    result, status_code = AdminInkassaService.get_detail(
        inkassa_id, actor=request.user,
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@manager_required
def inkassa_perform(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    batch_key = (
        request.META.get('HTTP_IDEMPOTENCY_KEY')
        or data.get('batch_id')
        or ''
    ).strip()
    if not batch_key or len(batch_key) > 128:
        return JsonResponse({
            'success': False,
            'message': 'Idempotency-Key header or batch_id is required',
            'error': {
                'batch_id': 'A stable batch id (1..128 characters) is required',
            },
        }, status=422)

    result, status_code = AdminInkassaService.perform(
        request.user,
        data,
        branch_id=(data.get('branch_id') or '').strip() or None,
        batch_key=batch_key,
    )
    if result.get('success') and not result.get('data', {}).get('replayed'):
        payload = result.get('data', {})
        audit(
            request,
            AuditLog.Action.INKASSA_PERFORM,
            target_type='CashRegister',
            metadata={
                'amount_removed': payload.get('amount_removed'),
                'balance_before': payload.get('balance_before'),
                'balance_after': payload.get('balance_after'),
                'branch_id': payload.get('branch_id'),
                'inkassa_ids': [i.get('id') for i in payload.get('inkassas', [])],
            },
        )
    return JsonResponse(result, status=status_code)
