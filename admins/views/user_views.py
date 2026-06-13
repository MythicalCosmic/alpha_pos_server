from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from base.helpers.request import parse_json_body, validate_pagination
from base.helpers.response import json_response
from base.security.permissions import manager_required
from base.security.audit import audit
from base.models import AuditLog
from admins.services.user_service import AdminUserService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@manager_required
def users(request):
    if request.method == "GET":
        page, per_page = validate_pagination(request)
        search = request.GET.get('search', '').strip()
        status = request.GET.get('status')
        role = request.GET.get('role')

        result, status_code = AdminUserService.list_users(
            page=page, per_page=per_page, search=search or None,
            status=status, role=role,
        )
        return JsonResponse(result, status=status_code)

    # POST — create user
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status_code = AdminUserService.create_user(
        first_name=data.get('first_name', ''),
        last_name=data.get('last_name', ''),
        role=data.get('role', 'CASHIER'),
        password=data.get('password'),
        email=data.get('email'),
        actor=request.user,
    )
    if result.get('success'):
        created_user = (result.get('data') or {}).get('user') or {}
        audit(
            request,
            AuditLog.Action.USER_CREATE,
            target_type='User',
            target_id=created_user.get('id'),
            # Skip email/password from the metadata — email is PII and the
            # audit row is sync-replicated; password is never logged anyway.
            metadata={'role': data.get('role', 'CASHIER')},
        )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "PUT", "PATCH", "DELETE"])
@manager_required
def user_detail(request, user_id):
    if request.method == "GET":
        result, status_code = AdminUserService.get_user(user_id)
        return JsonResponse(result, status=status_code)

    if request.method in ("PUT", "PATCH"):
        data, error = parse_json_body(request)
        if error:
            return json_response(error)

        result, status_code = AdminUserService.update_user(user_id, actor=request.user, **data)
        # Role escalation, account reactivation, and admin-driven password
        # resets all flow through update_user; without an audit row they leave
        # no trail and compromised admin credentials become undetectable.
        if result.get('success'):
            sensitive_keys = {'role', 'status', 'password', 'permissions', 'email'}
            changed = sorted(sensitive_keys & set(data.keys()))
            if changed:
                metadata = {'fields_changed': changed}
                # Capture the new role/status so the trail is useful for
                # privilege-escalation review; never log the password itself.
                if 'role' in data:
                    metadata['new_role'] = data['role']
                if 'status' in data:
                    metadata['new_status'] = data['status']
                if 'password' in data:
                    metadata['password_changed'] = True
                audit(
                    request,
                    AuditLog.Action.USER_UPDATE,
                    target_type='User',
                    target_id=user_id,
                    metadata=metadata,
                )
        return JsonResponse(result, status=status_code)

    if request.method == "DELETE":
        result, status_code = AdminUserService.delete_user(user_id)
        if result.get('success'):
            audit(
                request,
                AuditLog.Action.USER_DELETE,
                target_type='User',
                target_id=user_id,
            )
        return JsonResponse(result, status=status_code)
