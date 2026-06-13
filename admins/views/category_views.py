from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from base.helpers.request import parse_json_body, safe_page, safe_per_page
from base.helpers.response import json_response
from base.security.rate_limit import rate_limit
from base.security.permissions import manager_required, permission_required
from admins.services.category_service import AdminCategoryService
from admins.requests.category_requests import (
    create_category_request,
    update_status_request,
    reorder_request,
    bulk_ids_request,
)


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
@manager_required
def categories(request):
    if request.method == "GET":
        page = safe_page(request)
        per_page = safe_per_page(request, 20)
        search = request.GET.get('search')
        status = request.GET.get('status')
        order_by = request.GET.get('order_by', 'sort_order')
        include_deleted = request.GET.get('include_deleted', '').lower() == 'true'

        result, status_code = AdminCategoryService.get_all_categories(
            page=page,
            per_page=per_page,
            search=search,
            status=status,
            order_by=order_by,
            include_deleted=include_deleted,
        )
        return JsonResponse(result, status=status_code)

    denied = _check_permission(request, 'category.create')
    if denied:
        return denied

    data, error = create_category_request(request)
    if error:
        return json_response(error)

    result, status_code = AdminCategoryService.create_category(
        name=data['name'],
        description=data.get('description'),
        sort_order=data.get('sort_order', 0),
        status=data.get('status', 'ACTIVE'),
        colors=data.get('colors'),
        slug=data.get('slug'),
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "PUT", "PATCH", "DELETE"])
@manager_required
def category_detail(request, category_id):
    if request.method == "GET":
        include_deleted = request.GET.get('include_deleted', '').lower() == 'true'
        result, status_code = AdminCategoryService.get_category_by_id(
            category_id, include_deleted
        )
        return JsonResponse(result, status=status_code)

    if request.method == "DELETE":
        denied = _check_permission(request, 'category.delete')
        if denied:
            return denied
        hard_delete = request.GET.get('hard', '').lower() == 'true'
        result, status_code = AdminCategoryService.delete_category(category_id, hard_delete)
        return JsonResponse(result, status=status_code)

    denied = _check_permission(request, 'category.update')
    if denied:
        return denied
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status_code = AdminCategoryService.update_category(category_id, **data)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@manager_required
def active_categories(request):
    result, status_code = AdminCategoryService.get_active_categories()
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@manager_required
def deleted_categories(request):
    page = safe_page(request)
    per_page = safe_per_page(request, 20)
    result, status_code = AdminCategoryService.get_deleted_categories(page, per_page)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@manager_required
def category_stats(request):
    result, status_code = AdminCategoryService.get_category_stats()
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["PATCH"])
@manager_required
@permission_required('category.update')
def update_category_status(request, category_id):
    data, error = update_status_request(request)
    if error:
        return json_response(error)
    result, status_code = AdminCategoryService.update_category_status(
        category_id, data['status']
    )
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@manager_required
@permission_required('category.update')
def toggle_category_status(request, category_id):
    result, status_code = AdminCategoryService.toggle_category_status(category_id)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@manager_required
@permission_required('category.update')
def restore_category(request, category_id):
    result, status_code = AdminCategoryService.restore_category(category_id)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@rate_limit('admin_reorder', 20, 60)
@manager_required
@permission_required('category.update')
def reorder_categories(request):
    data, error = reorder_request(request)
    if error:
        return json_response(error)
    result, status_code = AdminCategoryService.reorder_categories(data['orders'])
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@rate_limit('admin_bulk_delete', 10, 60)
@manager_required
@permission_required('category.delete')
def bulk_delete_categories(request):
    data, error = bulk_ids_request(request)
    if error:
        return json_response(error)
    result, status_code = AdminCategoryService.bulk_delete(data['ids'])
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
@rate_limit('admin_bulk_restore', 10, 60)
@manager_required
@permission_required('category.update')
def bulk_restore_categories(request):
    data, error = bulk_ids_request(request)
    if error:
        return json_response(error)
    result, status_code = AdminCategoryService.bulk_restore(data['ids'])
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_GET
@manager_required
def category_by_slug(request, slug):
    result, status_code = AdminCategoryService.get_category_by_slug(slug)
    return JsonResponse(result, status=status_code)
