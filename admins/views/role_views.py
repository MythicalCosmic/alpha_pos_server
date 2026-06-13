"""Roles & permissions editor (Settings → Roles). Admin-only.

GET  /api/admins/permissions   -> { data: { permissions: [{key,label,group}] } }
GET  /api/admins/roles         -> { data: { roles: [{name, permissions:[keys]}] } }
PATCH /api/admins/roles/<name> -> body { permissions:[keys] } -> updated role
"""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from base.helpers.request import parse_json_body
from base.models import RolePermission, User
from base.security.permissions import admin_required
from base.security.permission_catalog import (
    catalog, VALID_KEYS, DEFAULT_ROLE_PERMISSIONS,
)

_ROLE_NAMES = [c[0] for c in User.RoleChoices.choices]


def _ensure_seeded():
    """Make sure every role has a RolePermission row (lazy seed of defaults)."""
    for name in _ROLE_NAMES:
        RolePermission.objects.get_or_create(
            role=name, defaults={'permissions': DEFAULT_ROLE_PERMISSIONS.get(name, [])},
        )


def _role_payload(row):
    return {'name': row.role, 'permissions': row.permissions or []}


@csrf_exempt
@require_http_methods(['GET'])
@admin_required
def list_permissions(request):
    return JsonResponse({'success': True, 'data': {'permissions': catalog()}})


@csrf_exempt
@require_http_methods(['GET'])
@admin_required
def list_roles(request):
    _ensure_seeded()
    rows = RolePermission.objects.all().order_by('role')
    return JsonResponse({'success': True, 'data': {'roles': [_role_payload(r) for r in rows]}})


@csrf_exempt
@require_http_methods(['GET', 'PATCH'])
@admin_required
def role_detail(request, name):
    name = (name or '').upper()
    if name not in _ROLE_NAMES:
        return JsonResponse({'success': False, 'message': 'Unknown role'}, status=404)
    _ensure_seeded()
    row, _ = RolePermission.objects.get_or_create(
        role=name, defaults={'permissions': DEFAULT_ROLE_PERMISSIONS.get(name, [])})

    if request.method == 'GET':
        return JsonResponse({'success': True, 'data': _role_payload(row)})

    # PATCH
    data, error = parse_json_body(request)
    if error:
        return JsonResponse(error[0], status=error[1])
    perms = data.get('permissions')
    if not isinstance(perms, list):
        return JsonResponse(
            {'success': False, 'message': 'permissions must be a list',
             'errors': {'permissions': 'list of keys required'}}, status=422)
    # Validate keys: allow the '*' wildcard + any catalog key. Reject unknowns
    # so the editor can't store typos that silently grant nothing.
    cleaned, unknown = [], []
    for p in perms:
        if p == '*' or p in VALID_KEYS:
            if p not in cleaned:
                cleaned.append(p)
        else:
            unknown.append(p)
    if unknown:
        return JsonResponse(
            {'success': False, 'message': f'Unknown permission keys: {unknown}',
             'errors': {'permissions': unknown}}, status=422)
    row.permissions = cleaned
    row.save(update_fields=['permissions', 'updated_at'])
    return JsonResponse({'success': True, 'data': _role_payload(row),
                         'message': 'Role updated'})
