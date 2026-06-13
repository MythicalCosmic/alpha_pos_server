from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from base.helpers.request import validate_pagination
from base.security.permissions import admin_required
from admins.services.audit_service import AdminAuditService


@csrf_exempt
@require_GET
@admin_required
def audit_log(request):
    page, per_page = validate_pagination(request)

    actor_id = request.GET.get('actor_id')
    try:
        actor_id = int(actor_id) if actor_id else None
    except (ValueError, TypeError):
        actor_id = None

    target_id = request.GET.get('target_id')
    try:
        target_id = int(target_id) if target_id else None
    except (ValueError, TypeError):
        target_id = None

    result, status_code = AdminAuditService.list_logs(
        page=page,
        per_page=per_page,
        action=request.GET.get('action') or None,
        actor_id=actor_id,
        target_type=request.GET.get('target_type') or None,
        target_id=target_id,
        date_from=request.GET.get('date_from') or None,
        date_to=request.GET.get('date_to') or None,
    )
    return JsonResponse(result, status=status_code)
